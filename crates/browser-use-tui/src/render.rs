use anyhow::Result;
use browser_use_protocol::{
    EventRecord, HistoryRow, SessionMeta, TelemetrySummary, TranscriptTurn, WorkbenchState,
};
use pulldown_cmark::{CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use ratatui::backend::TestBackend;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Margin, Position, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Clear, Paragraph, Wrap};
use ratatui::{Frame, Terminal};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::composer::composer_rule;
use crate::settings::{ACCOUNT_CHOICES, BROWSER_CHOICES, MODEL_CHOICES};
use crate::theme::*;

use super::{App, ProductState, Surface};

pub(crate) fn render_dump(app: &mut App) -> Result<String> {
    let backend = TestBackend::new(app.args.width, app.args.height);
    let mut terminal = Terminal::new(backend)?;
    terminal.draw(|frame| render(frame, app))?;
    Ok(buffer_to_string(terminal.backend().buffer()))
}

fn buffer_to_string(buffer: &ratatui::buffer::Buffer) -> String {
    let area = buffer.area;
    let mut out = String::new();
    for y in area.y..area.y.saturating_add(area.height) {
        let mut line = String::new();
        for x in area.x..area.x.saturating_add(area.width) {
            line.push_str(buffer[(x, y)].symbol());
        }
        out.push_str(line.trim_end());
        out.push('\n');
    }
    out
}

pub(crate) fn native_scrollback_lines(app: &mut App, width: u16) -> Result<Vec<Line<'static>>> {
    let state = app.workbench_state()?;
    let product_state = app.product_state(&state);
    let body_width = width.saturating_sub(4).max(1);
    let surface =
        if app.is_first_run_setup_visible().unwrap_or(false) && app.surface == Surface::Main {
            Surface::Setup
        } else {
            app.surface
        };
    let mut lines = Vec::new();
    lines.extend(header_lines(
        app,
        &state,
        surface_title(surface),
        body_width,
    ));
    if surface == Surface::Main {
        lines.extend(match product_state {
            ProductState::SetupNeeded => setup_lines(app),
            ProductState::Ready => ready_lines(app, &state),
            ProductState::Running => running_history_lines(&state, body_width),
            ProductState::Result => result_lines(&state, body_width),
            ProductState::Failed => failure_lines(&state, app, body_width),
            ProductState::Cancelled => cancelled_lines(&state, body_width),
        });
    } else {
        lines.extend(surface_lines(surface, app, &state));
    }
    lines.push(Line::from(""));
    Ok(lines)
}

pub(crate) fn lines_plain_text(lines: &[Line<'static>]) -> String {
    let mut out = String::new();
    for line in lines {
        for span in &line.spans {
            out.push_str(&span.content);
        }
        out.push('\n');
    }
    out
}

pub(crate) fn native_scrollback_event_lines(
    events: &[EventRecord],
    state: &WorkbenchState,
    width: u16,
    last_group: &mut Option<String>,
) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    for event in events {
        match event.event_type.as_str() {
            "session.input" | "session.followup" => {
                let Some(prompt) = event
                    .payload
                    .get("text")
                    .and_then(serde_json::Value::as_str)
                    .map(str::trim)
                    .filter(|text| !text.is_empty())
                else {
                    continue;
                };
                last_group.take();
                push_gap_if_needed(&mut lines);
                append_prompt_section(&mut lines, prompt);
            }
            "session.done" => {
                last_group.take();
                if let Some(result) = event
                    .payload
                    .get("result")
                    .and_then(serde_json::Value::as_str)
                    .filter(|text| !text.trim().is_empty())
                {
                    push_gap_if_needed(&mut lines);
                    append_result_block(&mut lines, result, state, width);
                }
            }
            "session.failed" => {
                last_group.take();
                let error = event
                    .payload
                    .get("error")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("The task failed.");
                push_gap_if_needed(&mut lines);
                append_ascii_lines_block(
                    &mut lines,
                    "error",
                    vec![Line::from(Span::styled(
                        friendly_error_message(error),
                        muted(),
                    ))],
                    Some("saved"),
                );
            }
            "session.cancelled" => {
                last_group.take();
                push_gap_if_needed(&mut lines);
                append_ascii_lines_block(
                    &mut lines,
                    "stopped",
                    vec![Line::from(Span::styled(
                        "Progress is saved in history.",
                        muted(),
                    ))],
                    Some("saved"),
                );
            }
            "agent.completed" => {
                if let Some(result) = event
                    .payload
                    .get("payload")
                    .and_then(|payload| payload.get("result"))
                    .and_then(serde_json::Value::as_str)
                    .filter(|text| !text.trim().is_empty())
                {
                    last_group.take();
                    push_gap_if_needed(&mut lines);
                    append_result_block(&mut lines, result, state, width);
                } else {
                    append_grouped_event_line(
                        &mut lines,
                        last_group,
                        "explored",
                        "helper finished",
                    );
                }
            }
            "agent.failed" => {
                last_group.take();
                let error = event
                    .payload
                    .get("payload")
                    .and_then(|payload| payload.get("error"))
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("The agent could not start.");
                push_gap_if_needed(&mut lines);
                append_ascii_lines_block(
                    &mut lines,
                    "error",
                    vec![Line::from(Span::styled(error.to_string(), muted()))],
                    Some("saved"),
                );
            }
            "agent.cancelled" => {
                append_grouped_event_line(&mut lines, last_group, "explored", "helper stopped");
            }
            "browser.connected" => {
                append_grouped_event_line(&mut lines, last_group, "browser", "browser connected");
            }
            "browser.reconnected" => {
                append_grouped_event_line(&mut lines, last_group, "browser", "browser reconnected");
            }
            "browser.target_changed" => {
                append_grouped_event_line(
                    &mut lines,
                    last_group,
                    "browser",
                    "browser target changed",
                );
            }
            "browser.disconnected" => {
                append_grouped_event_line(
                    &mut lines,
                    last_group,
                    "browser",
                    "browser disconnected",
                );
            }
            "browser.live_url" => {
                append_grouped_event_line(
                    &mut lines,
                    last_group,
                    "browser",
                    "connected live browser",
                );
            }
            "browser.page" | "browser.state" => {
                if let Some(url) = event.payload.get("url").and_then(serde_json::Value::as_str) {
                    append_grouped_event_line(
                        &mut lines,
                        last_group,
                        "browser",
                        &format!("opened {}", compact_url_for_render(url)),
                    );
                }
            }
            "command.started" => {
                let text = event
                    .payload
                    .get("cmd")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("command");
                append_grouped_event_line(&mut lines, last_group, "ran", text);
            }
            "command.finished" => {
                if event
                    .payload
                    .get("success")
                    .and_then(serde_json::Value::as_bool)
                    .is_some_and(|success| !success)
                {
                    let code = event
                        .payload
                        .get("exit_code")
                        .and_then(serde_json::Value::as_i64)
                        .map(|code| code.to_string())
                        .unwrap_or_else(|| "unknown".to_string());
                    append_grouped_event_line(
                        &mut lines,
                        last_group,
                        "ran",
                        &format!("command failed with exit {code}"),
                    );
                }
            }
            "file.read" => {
                if let Some(path) = event
                    .payload
                    .get("path")
                    .and_then(serde_json::Value::as_str)
                {
                    append_grouped_event_line(
                        &mut lines,
                        last_group,
                        "explored",
                        &format!("read {}", compact_path_for_render(path)),
                    );
                }
            }
            "file.search" => {
                let query = event
                    .payload
                    .get("query")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("files");
                append_grouped_event_line(
                    &mut lines,
                    last_group,
                    "explored",
                    &format!("searched {query:?}"),
                );
            }
            "file.list" => {
                append_grouped_event_line(&mut lines, last_group, "explored", "listed files");
            }
            "patch.file_changed" => {
                let kind = event
                    .payload
                    .get("kind")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("changed");
                let path = event
                    .payload
                    .get("path")
                    .and_then(serde_json::Value::as_str)
                    .map(compact_path_for_render)
                    .unwrap_or_else(|| "file".to_string());
                append_grouped_event_line(
                    &mut lines,
                    last_group,
                    "changed",
                    &format!("{kind} {path}"),
                );
            }
            "agent.spawned" => {
                append_grouped_event_line(
                    &mut lines,
                    last_group,
                    "explored",
                    &agent_started_text_for_render(&event.payload),
                );
            }
            _ => {}
        }
    }
    lines
}

pub(crate) fn render(frame: &mut Frame<'_>, app: &mut App) {
    let area = app_surface(frame.area());
    let state = app.workbench_state().unwrap_or_else(|_| WorkbenchState {
        setup_complete: false,
        current_session: None,
        task: None,
        result: None,
        failure: Some("Could not load state.".to_string()),
        activity: Vec::new(),
        transcript: Vec::new(),
        browser: Default::default(),
        telemetry: Default::default(),
        history: Vec::new(),
    });
    let product_state = app.product_state(&state);

    if app.is_first_run_setup_visible().unwrap_or(false) && app.surface == Surface::Main {
        render_surface(frame, area, app, &state, Surface::Setup);
        return;
    }

    match app.surface {
        Surface::Main => render_main(frame, area, app, &state, product_state),
        surface => render_surface(frame, area, app, &state, surface),
    }
}

fn app_surface(area: Rect) -> Rect {
    area.inner(Margin {
        vertical: 0,
        horizontal: 2,
    })
}

fn render_main(
    frame: &mut Frame<'_>,
    area: Rect,
    app: &mut App,
    state: &WorkbenchState,
    product_state: ProductState,
) {
    let composer_h = app.composer_height();
    if app.native_scrollback_is_active() {
        let live_lines = native_live_lines(app, state, product_state);
        let compact_composer_h = composer_h.saturating_sub(1).max(1);
        if live_lines.is_empty() {
            let chunks = Layout::default()
                .direction(Direction::Vertical)
                .constraints([
                    Constraint::Length(compact_composer_h),
                    Constraint::Length(1),
                    Constraint::Min(0),
                ])
                .split(area);
            render_composer_input(frame, chunks[0], app, state.current_session.as_ref());
            render_footer(frame, chunks[1], app, state, product_state);
            return;
        }
        let chunks = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(live_lines.len().min(4) as u16),
                Constraint::Length(compact_composer_h),
                Constraint::Length(1),
                Constraint::Min(0),
            ])
            .split(area);
        frame.render_widget(
            Paragraph::new(live_lines)
                .style(Style::default().fg(text()))
                .wrap(Wrap { trim: false }),
            chunks[0],
        );
        render_composer_input(frame, chunks[1], app, state.current_session.as_ref());
        render_footer(frame, chunks[2], app, state, product_state);
        return;
    }

    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2),
            Constraint::Min(6),
            Constraint::Length(composer_h + 1),
            Constraint::Length(1),
        ])
        .split(area);

    render_header(frame, chunks[0], app, state, "browser-use");
    let body = match product_state {
        ProductState::SetupNeeded => setup_lines(app),
        ProductState::Ready => ready_lines(app, state),
        ProductState::Running => running_lines(state, chunks[1].width),
        ProductState::Result => result_lines(state, chunks[1].width),
        ProductState::Failed => failure_lines(state, app, chunks[1].width),
        ProductState::Cancelled => cancelled_lines(state, chunks[1].width),
    };
    frame.render_widget(
        Paragraph::new(body)
            .style(Style::default().fg(text()))
            .wrap(Wrap { trim: false }),
        chunks[1],
    );
    render_composer(frame, chunks[2], app, state.current_session.as_ref());
    render_footer(frame, chunks[3], app, state, product_state);
}

fn native_live_lines(
    app: &App,
    state: &WorkbenchState,
    product_state: ProductState,
) -> Vec<Line<'static>> {
    match product_state {
        ProductState::Running => {
            let mut lines = Vec::new();
            append_ascii_text_block(
                &mut lines,
                "working",
                &[format!("{} running browser task", spinner_frame())],
                Some("live"),
            );
            lines
        }
        ProductState::Failed => {
            let error = state.failure.as_deref().unwrap_or("The task failed.");
            let (primary, secondary) = failure_actions(error);
            let mut lines = Vec::new();
            append_ascii_lines_block(
                &mut lines,
                "next",
                vec![
                    selected(primary, 0, app.selected_row),
                    selected(secondary, 1, app.selected_row),
                    selected("Retry", 2, app.selected_row),
                    selected("New task", 3, app.selected_row),
                ],
                None,
            );
            lines
        }
        ProductState::Cancelled => {
            let mut lines = Vec::new();
            append_ascii_lines_block(
                &mut lines,
                "next",
                vec![
                    selected("Continue with a follow-up", 0, app.selected_row),
                    selected("Start a new task", 1, app.selected_row),
                    selected("Previous work", 2, app.selected_row),
                ],
                None,
            );
            lines
        }
        _ => Vec::new(),
    }
}

fn render_surface(
    frame: &mut Frame<'_>,
    area: Rect,
    app: &App,
    state: &WorkbenchState,
    surface: Surface,
) {
    frame.render_widget(Clear, frame.area());
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2),
            Constraint::Min(6),
            Constraint::Length(1),
        ])
        .split(area);
    render_header(frame, chunks[0], app, state, surface_title(surface));
    let lines = surface_lines(surface, app, state);
    frame.render_widget(
        Paragraph::new(lines)
            .style(Style::default().fg(text()))
            .wrap(Wrap { trim: false }),
        chunks[1],
    );
    frame.render_widget(
        Paragraph::new(surface_footer(surface))
            .style(muted())
            .alignment(Alignment::Right),
        chunks[2],
    );
}

fn surface_title(surface: Surface) -> &'static str {
    match surface {
        Surface::Setup => "browser-use setup",
        Surface::Account => "browser-use setup / sign in",
        Surface::ApiKey => "browser-use setup / sign in",
        Surface::Telemetry => "browser-use / Laminar",
        Surface::Model => "browser-use setup / model",
        Surface::Browser => "browser-use / browser",
        Surface::BrowserSelect => "browser-use setup / browser",
        Surface::History => "browser-use / previous work",
        Surface::Actions => "Actions",
        Surface::Developer => "browser-use / developer",
        Surface::Main => "browser-use",
    }
}

fn surface_footer(surface: Surface) -> &'static str {
    match surface {
        Surface::ApiKey => "enter save   esc cancel",
        Surface::Telemetry => "enter save   esc cancel",
        Surface::Actions => "type to filter   enter select   esc close",
        Surface::History => "enter open   r resume   / actions   esc back",
        Surface::Setup => "enter continue   esc quit",
        Surface::Browser => "enter select   esc back",
        Surface::Developer => "esc close",
        _ => "enter select   esc back",
    }
}

fn surface_lines(surface: Surface, app: &App, state: &WorkbenchState) -> Vec<Line<'static>> {
    match surface {
        Surface::Setup => setup_lines(app),
        Surface::Account => account_lines(app),
        Surface::ApiKey => api_key_lines(app),
        Surface::Telemetry => telemetry_key_lines(app),
        Surface::Model => model_lines(app),
        Surface::Browser => browser_panel_lines(app, state),
        Surface::BrowserSelect => browser_select_lines(app),
        Surface::History => history_lines(app, state),
        Surface::Actions => action_lines(app),
        Surface::Developer => developer_lines(app, state),
        Surface::Main => Vec::new(),
    }
}

fn render_header(
    frame: &mut Frame<'_>,
    area: Rect,
    app: &App,
    state: &WorkbenchState,
    title: &str,
) {
    let lines = header_lines(app, state, title, area.width);
    frame.render_widget(Paragraph::new(lines), area);
}

fn header_lines(app: &App, state: &WorkbenchState, title: &str, width: u16) -> Vec<Line<'static>> {
    let width = width as usize;
    if width == 0 {
        return Vec::new();
    }
    let browser = browser_header_label(app, state);
    let right = format!("{browser}   {}", app.model);
    let max_left = width.saturating_sub(right.chars().count() + 2).max(10);
    let left = truncate(title, max_left);
    let right = truncate(&right, width.saturating_sub(left.chars().count() + 2));
    let spaces = width.saturating_sub(left.chars().count() + right.chars().count());
    vec![
        Line::from(vec![
            Span::styled(left, bold()),
            Span::raw(" ".repeat(spaces)),
            Span::styled(right, muted()),
        ]),
        Line::from(composer_rule(width as u16)),
    ]
}

fn browser_header_label(app: &App, state: &WorkbenchState) -> String {
    let status = if state.browser.status == "not connected" {
        if app.browser == "Browser Use cloud" {
            "ready"
        } else {
            "connected"
        }
    } else {
        state.browser.status.as_str()
    };
    format!("{} {status}", app.browser)
}

fn render_composer(
    frame: &mut Frame<'_>,
    area: Rect,
    app: &App,
    current_session: Option<&SessionMeta>,
) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Min(1)])
        .split(area);
    frame.render_widget(Paragraph::new(composer_rule(chunks[0].width)), chunks[0]);
    render_composer_input(frame, chunks[1], app, current_session);
}

fn render_composer_input(
    frame: &mut Frame<'_>,
    area: Rect,
    app: &App,
    current_session: Option<&SessionMeta>,
) {
    let placeholder = if current_session.is_some_and(|session| session.status.is_active()) {
        "Type to steer the agent..."
    } else if current_session.is_some() {
        "Ask a follow-up..."
    } else {
        "Tell the browser what to do..."
    };
    let max_lines = area.height.max(1) as usize;
    frame.render_widget(
        Paragraph::new(app.composer.render_lines(max_lines, placeholder))
            .style(Style::default().fg(text()))
            .wrap(Wrap { trim: false }),
        area,
    );
    if area.width > 0 && area.height > 0 {
        let (cursor_x, cursor_y) = app.composer.cursor_position(max_lines);
        frame.set_cursor_position(Position {
            x: area
                .x
                .saturating_add(cursor_x.min(area.width.saturating_sub(1))),
            y: area
                .y
                .saturating_add(cursor_y.min(area.height.saturating_sub(1))),
        });
    }
}

fn render_footer(
    frame: &mut Frame<'_>,
    area: Rect,
    app: &App,
    state: &WorkbenchState,
    product_state: ProductState,
) {
    let label = if app
        .quit_hint_until
        .is_some_and(|until| std::time::Instant::now() <= until)
    {
        "ctrl+c again to quit"
    } else {
        match product_state {
            ProductState::Running => {
                "enter steer   shift+enter newline   ctrl+c stop   f2 browser   / actions"
            }
            ProductState::Ready | ProductState::SetupNeeded => {
                "enter run   tab history   / actions"
            }
            ProductState::Failed | ProductState::Cancelled => "enter select   / actions",
            ProductState::Result => {
                if state.current_session.is_some() {
                    "enter follow-up   shift+enter newline   f2 browser   tab history   / actions"
                } else {
                    "enter run   tab history   / actions"
                }
            }
        }
    };
    frame.render_widget(
        Paragraph::new(label)
            .style(muted())
            .alignment(Alignment::Right),
        area,
    );
}

fn setup_lines(app: &App) -> Vec<Line<'static>> {
    let account_status = if app.account_ready(&app.account).unwrap_or(false) {
        "connected"
    } else {
        "No account connected"
    };
    let model_status = if app.model_configured {
        app.model.as_str()
    } else {
        "No model selected"
    };
    let mut lines = vec![
        Line::from(Span::styled("Set up the browser agent", bold())),
        Line::from(""),
        setup_status_line(
            if app.account_ready(&app.account).unwrap_or(false) {
                "ok"
            } else {
                "needs"
            },
            "Sign in",
            account_status,
        ),
        setup_status_line(
            if app.model_configured { "ok" } else { "needs" },
            "Model",
            model_status,
        ),
        setup_status_line("ok", "Browser", &format!("{} available", app.browser)),
        Line::from(""),
    ];
    if let Some(notice) = app.status_notice.as_ref() {
        lines.push(Line::from(Span::styled(
            notice.clone(),
            status_style("failed"),
        )));
        lines.push(Line::from(""));
    }
    lines.push(selected("Sign in", 0, app.selected_row));
    lines.push(selected("Choose model", 1, app.selected_row));
    lines.push(selected("Change browser", 2, app.selected_row));
    if app.model_configured && app.account_ready(&app.account).unwrap_or(false) {
        lines.push(selected("Continue", 3, app.selected_row));
    }
    lines
}

fn account_lines(app: &App) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(Span::styled("Choose an account", bold())),
        Line::from(""),
    ];
    if let Some(notice) = app.status_notice.as_ref() {
        lines.push(Line::from(Span::styled(
            notice.clone(),
            status_style("failed"),
        )));
        lines.push(Line::from(""));
    }
    for (idx, account) in ACCOUNT_CHOICES.iter().enumerate() {
        let status = if *account == "Codex login" || app.account_ready(account).unwrap_or(false) {
            "connected"
        } else if account.contains("API key") {
            "needs key"
        } else {
            "needs sign in"
        };
        lines.push(selected(
            &format!("{account:<24} {status}"),
            idx,
            app.selected_row,
        ));
    }
    lines
}

fn api_key_lines(app: &App) -> Vec<Line<'static>> {
    let account = app.api_key_account.as_deref().unwrap_or("selected account");
    let mut lines = vec![
        Line::from(Span::styled(auth_secret_label(account), bold())),
        Line::from(""),
        Line::from(format!("  {}", masked_secret(app.composer.input()))),
        Line::from(""),
        Line::from(Span::styled(
            "  This key is stored locally in browser-use state.",
            muted(),
        )),
        Line::from(""),
    ];
    if let Some(notice) = app.status_notice.as_ref() {
        lines.push(Line::from(Span::styled(
            notice.clone(),
            status_style("failed"),
        )));
        lines.push(Line::from(""));
    }
    lines.push(Line::from("> Save key"));
    lines.push(Line::from("  Cancel"));
    lines
}

fn telemetry_key_lines(app: &App) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(Span::styled("Laminar API key", bold())),
        Line::from(""),
        Line::from(format!("  {}", masked_secret(app.composer.input()))),
        Line::from(""),
        Line::from(Span::styled(
            "  Stored locally and used by future agent runs.",
            muted(),
        )),
        Line::from(""),
    ];
    if let Some(notice) = app.status_notice.as_ref() {
        lines.push(Line::from(Span::styled(
            notice.clone(),
            status_style("failed"),
        )));
        lines.push(Line::from(""));
    }
    lines.push(Line::from("> Save key"));
    lines.push(Line::from("  Cancel"));
    lines
}

fn model_lines(app: &App) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(Span::styled("Recommended", bold())),
        Line::from(""),
    ];
    for (idx, choice) in MODEL_CHOICES.iter().enumerate() {
        if idx == 3 {
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled("API keys", bold())));
            lines.push(Line::from(""));
        }
        lines.push(selected(choice.row, idx, app.selected_row));
    }
    lines.extend([
        Line::from(""),
        Line::from(Span::styled("Current", muted())),
        Line::from(if app.model_configured {
            format!("  {} via {}", app.model, app.account)
        } else {
            "  none".to_string()
        }),
    ]);
    lines
}

fn browser_select_lines(app: &App) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(Span::styled("Choose browser", bold())),
        Line::from(""),
    ];
    let descriptions = [
        "remote browser with live view",
        "visible browser on this machine",
        "background browser",
    ];
    for (idx, browser) in BROWSER_CHOICES.iter().enumerate() {
        lines.push(selected(
            &format!("{browser:<24} {}", descriptions[idx]),
            idx,
            app.selected_row,
        ));
    }
    lines.extend([
        Line::from(""),
        Line::from(Span::styled("Current", muted())),
        Line::from(format!("  {}", app.browser)),
    ]);
    lines
}

fn ready_lines(app: &App, state: &WorkbenchState) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(Span::styled("What should the browser do?", bold())),
        Line::from(""),
    ];
    if let Some(notice) = app.status_notice.as_ref() {
        lines.push(Line::from(Span::styled(
            notice.clone(),
            status_style("failed"),
        )));
        lines.push(Line::from(""));
    }
    lines.push(Line::from(Span::styled("Recent", muted())));
    lines.push(Line::from(""));
    if state.history.is_empty() {
        lines.push(Line::from(Span::styled("  No previous work yet.", dim())));
    } else {
        for row in state.history.iter().take(4) {
            lines.push(history_line(row, 86));
        }
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("Ready", muted())));
    lines.push(kv_line("account", &app.account));
    lines.push(kv_line("browser", &browser_ready_label(app, state)));
    lines
}

fn running_lines(state: &WorkbenchState, width: u16) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    append_ascii_text_block(
        &mut lines,
        "working",
        &[format!("{} running browser task", spinner_frame())],
        Some("live"),
    );
    lines.push(Line::from(""));
    if !append_transcript_turns(&mut lines, state, width, true) {
        append_task_section(&mut lines, state);
        lines.push(Line::from(""));
        append_activity_section(&mut lines, state, true);
    }
    lines
}

fn running_history_lines(state: &WorkbenchState, width: u16) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    if !append_transcript_turns(&mut lines, state, width, true) {
        append_task_section(&mut lines, state);
        lines.push(Line::from(""));
        append_activity_section(&mut lines, state, true);
    }
    lines
}

fn result_lines(state: &WorkbenchState, width: u16) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    if !append_transcript_turns(&mut lines, state, width, false) {
        append_task_section(&mut lines, state);
        lines.push(Line::from(""));
        append_activity_section(&mut lines, state, false);
        lines.push(Line::from(""));
        if let Some(result) = state.result.as_ref() {
            append_result_block(&mut lines, result, state, width);
        } else {
            append_ascii_text_block(
                &mut lines,
                "result",
                &["No result yet.".to_string()],
                Some("done"),
            );
        }
    }
    lines
}

fn failure_lines(state: &WorkbenchState, app: &App, width: u16) -> Vec<Line<'static>> {
    let error = state.failure.as_deref().unwrap_or("The task failed.");
    let message = friendly_error_message(error);
    let (primary, secondary) = failure_actions(error);
    let mut lines = Vec::new();
    if append_transcript_turns(&mut lines, state, width, false) {
        lines.push(Line::from(""));
    } else {
        append_task_section(&mut lines, state);
        lines.push(Line::from(""));
    }
    append_ascii_lines_block(
        &mut lines,
        "error",
        vec![
            Line::from(Span::styled("The agent could not start.", bold())),
            Line::from(Span::styled(message, muted())),
        ],
        Some("saved"),
    );
    lines.push(Line::from(""));
    append_ascii_lines_block(
        &mut lines,
        "next",
        vec![
            selected(primary, 0, app.selected_row),
            selected(secondary, 1, app.selected_row),
            selected("Retry", 2, app.selected_row),
            selected("New task", 3, app.selected_row),
        ],
        None,
    );
    lines
}

fn cancelled_lines(state: &WorkbenchState, width: u16) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    if append_transcript_turns(&mut lines, state, width, false) {
        lines.push(Line::from(""));
    } else {
        append_task_section(&mut lines, state);
        lines.push(Line::from(""));
    }
    append_ascii_lines_block(
        &mut lines,
        "stopped",
        vec![Line::from(Span::styled(
            "Progress is saved in history.",
            muted(),
        ))],
        Some("saved"),
    );
    lines.push(Line::from(""));
    append_ascii_lines_block(
        &mut lines,
        "next",
        vec![
            Line::from("> Continue with a follow-up"),
            Line::from("  Start a new task"),
            Line::from("  Previous work"),
        ],
        None,
    );
    lines
}

fn browser_panel_lines(app: &App, state: &WorkbenchState) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(Span::styled("Current browser", bold())),
        Line::from(""),
        kv_line("backend", &app.browser),
        kv_line("status", &state.browser.status),
        kv_line("title", state.browser.title.as_deref().unwrap_or("unknown")),
        kv_line(
            "page",
            state.browser.url.as_deref().unwrap_or("no page yet"),
        ),
        kv_line(
            "live view",
            state
                .browser
                .live_url
                .as_deref()
                .map(|_| "available")
                .unwrap_or("not available"),
        ),
        kv_line(
            "tabs",
            &state
                .browser
                .tabs
                .map(|tabs| format!("{tabs} open"))
                .unwrap_or_else(|| "unknown".to_string()),
        ),
        kv_line(
            "viewport",
            state.browser.viewport.as_deref().unwrap_or("unknown"),
        ),
        Line::from(""),
        selected("Open live browser", 0, app.selected_row),
        selected("Reconnect", 1, app.selected_row),
        selected("Change browser", 2, app.selected_row),
    ];
    if let Some(notice) = app.browser_notice.as_ref() {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(notice.clone(), muted())));
    }
    lines
}

fn history_lines(app: &App, state: &WorkbenchState) -> Vec<Line<'static>> {
    if state.history.is_empty() {
        return vec![Line::from(Span::styled("No previous work yet.", dim()))];
    }
    state
        .history
        .iter()
        .enumerate()
        .map(|(idx, row)| history_overlay_line(row, idx, app.selected_row, 88))
        .collect()
}

fn action_lines(app: &App) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled("Actions", bold())), Line::from("")];
    if !app.palette.filter().is_empty() {
        lines.push(Line::from(vec![
            Span::styled("filter  ", muted()),
            Span::styled(app.palette.filter().to_string(), text_style()),
        ]));
        lines.push(Line::from(""));
    }
    let items = app.palette.items();
    if items.is_empty() {
        lines.push(Line::from(Span::styled("No matching actions.", dim())));
    } else {
        for (idx, item) in items.iter().enumerate() {
            lines.push(selected(item.label, idx, app.selected_row));
        }
    }
    lines
}

fn developer_lines(app: &App, state: &WorkbenchState) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(Span::styled("Laminar", bold())),
        Line::from(""),
        kv_line(
            "status",
            &app.laminar_status()
                .unwrap_or_else(|_| "settings unavailable".to_string()),
        ),
    ];
    if let Some(notice) = app.status_notice.as_ref() {
        lines.push(Line::from(Span::styled(notice.clone(), muted())));
    }
    lines.push(selected("Configure Laminar", 0, app.selected_row));
    lines.extend([
        Line::from(""),
        Line::from(Span::styled("Current task", bold())),
        Line::from(""),
    ]);
    let Some(session) = state.current_session.as_ref() else {
        lines.push(Line::from(Span::styled("No task selected.", dim())));
        return lines;
    };
    append_telemetry_detail_lines(&mut lines, &state.telemetry);
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("Events", bold())));
    lines.push(Line::from(""));
    match app.store.events_for_session(&session.id) {
        Ok(events) => {
            for event in events.iter().rev().take(12).rev() {
                let payload = truncate(&event.payload.to_string(), 44);
                lines.push(Line::from(vec![
                    Span::styled(format!("{:>4}  ", event.seq), muted()),
                    Span::styled(
                        format!("{:<24}", truncate(&event.event_type, 24)),
                        text_style(),
                    ),
                    Span::styled(payload, dim()),
                ]));
            }
        }
        Err(err) => lines.push(Line::from(Span::styled(err.to_string(), dim()))),
    }
    lines
}

fn append_task_section(lines: &mut Vec<Line<'static>>, state: &WorkbenchState) {
    append_prompt_section(
        lines,
        &state
            .task
            .clone()
            .unwrap_or_else(|| "browser task".to_string()),
    );
}

fn append_transcript_turns(
    lines: &mut Vec<Line<'static>>,
    state: &WorkbenchState,
    width: u16,
    running: bool,
) -> bool {
    if state.transcript.is_empty() {
        return false;
    }
    for (idx, turn) in state.transcript.iter().enumerate() {
        if idx > 0 {
            lines.push(Line::from(""));
        }
        let is_pending_running = running
            && idx + 1 == state.transcript.len()
            && turn.result.is_none()
            && turn.failure.is_none();
        append_prompt_section(lines, &turn.prompt);
        append_turn_activity(lines, turn);
        if is_pending_running {
            continue;
        }
        if let Some(failure) = turn.failure.as_ref() {
            lines.push(Line::from(""));
            append_ascii_lines_block(
                lines,
                "error",
                vec![Line::from(Span::styled(
                    friendly_error_message(failure),
                    muted(),
                ))],
                Some("saved"),
            );
        } else if let Some(result) = turn.result.as_ref() {
            lines.push(Line::from(""));
            if idx + 1 == state.transcript.len() {
                append_result_block(lines, result, state, width);
            } else {
                append_markdown_block(lines, "result", result, width, Some("done"));
            }
        }
    }
    true
}

fn append_prompt_section(lines: &mut Vec<Line<'static>>, prompt: &str) {
    lines.push(Line::from(vec![
        Span::styled("> ", accent()),
        Span::styled(prompt.to_string(), text_style()),
    ]));
}

fn append_turn_activity(lines: &mut Vec<Line<'static>>, turn: &TranscriptTurn) {
    if turn.activity.is_empty() {
        return;
    }
    lines.push(Line::from(""));
    append_activity_blocks(lines, &turn.activity);
}

fn append_activity_section(lines: &mut Vec<Line<'static>>, state: &WorkbenchState, running: bool) {
    if state.activity.is_empty() {
        let fallback = if running {
            "starting browser task"
        } else {
            "no recorded steps"
        };
        append_ascii_text_block(lines, "activity", &[fallback.to_string()], Some("pending"));
        return;
    }
    append_activity_blocks(lines, &state.activity);
}

fn append_activity_blocks(lines: &mut Vec<Line<'static>>, activity: &[String]) {
    let mut browser = Vec::new();
    let mut explored = Vec::new();
    let mut ran = Vec::new();
    let mut changed = Vec::new();
    let mut other = Vec::new();

    for item in activity {
        let formatted = format_activity_item(item);
        if is_browser_activity(item) {
            browser.push(formatted);
        } else if is_command_activity(item) {
            ran.push(formatted);
        } else if is_change_activity(item) {
            changed.push(formatted);
        } else if is_explore_activity(item) {
            explored.push(formatted);
        } else {
            other.push(formatted);
        }
    }

    let mut wrote = false;
    for (title, items) in [
        ("browser", browser),
        ("explored", explored),
        ("ran", ran),
        ("changed", changed),
        ("activity", other),
    ] {
        if items.is_empty() {
            continue;
        }
        if wrote {
            lines.push(Line::from(""));
        }
        append_ascii_text_block(lines, title, &items, Some("done"));
        wrote = true;
    }
}

fn append_result_block(
    lines: &mut Vec<Line<'static>>,
    result: &str,
    state: &WorkbenchState,
    width: u16,
) {
    append_markdown_block(lines, "result", result, width, None);
    if let Some(source) = state
        .browser
        .url
        .as_ref()
        .or(state.browser.live_url.as_ref())
    {
        append_ascii_tail(
            lines,
            "source",
            vec![Line::from(Span::styled(source.clone(), link()))],
        );
    } else {
        append_ascii_tail(lines, "done", Vec::new());
    }
}

fn append_markdown_block(
    lines: &mut Vec<Line<'static>>,
    title: &str,
    markdown: &str,
    width: u16,
    footer: Option<&str>,
) {
    let body_width = width.saturating_sub(8).max(24);
    let body = markdown_result_lines(markdown, body_width)
        .into_iter()
        .map(trim_default_markdown_indent)
        .collect();
    append_ascii_lines_block(lines, title, body, footer);
}

fn append_ascii_text_block(
    lines: &mut Vec<Line<'static>>,
    title: &str,
    items: &[String],
    footer: Option<&str>,
) {
    let body = items
        .iter()
        .map(|item| Line::from(Span::styled(item.clone(), text_style())))
        .collect();
    append_ascii_lines_block(lines, title, body, footer);
}

fn append_ascii_lines_block(
    lines: &mut Vec<Line<'static>>,
    title: &str,
    body: Vec<Line<'static>>,
    footer: Option<&str>,
) {
    lines.push(Line::from(vec![
        Span::styled("  +- ", dim()),
        Span::styled(title.to_string(), bold()),
    ]));
    if body.is_empty() {
        lines.push(Line::from(vec![
            Span::styled("  |  ", dim()),
            Span::styled("no details", dim()),
        ]));
    } else {
        for line in body {
            lines.push(prefix_block_line("  |  ", line));
        }
    }
    if let Some(footer) = footer {
        append_ascii_tail(lines, footer, Vec::new());
    }
}

fn append_ascii_tail(lines: &mut Vec<Line<'static>>, label: &str, body: Vec<Line<'static>>) {
    lines.push(Line::from(vec![
        Span::styled("  +- ", dim()),
        Span::styled(label.to_string(), muted()),
    ]));
    for line in body {
        lines.push(prefix_block_line("     ", line));
    }
}

fn push_gap_if_needed(lines: &mut Vec<Line<'static>>) {
    if !lines.is_empty() {
        lines.push(Line::from(""));
    }
}

fn append_grouped_event_line(
    lines: &mut Vec<Line<'static>>,
    last_group: &mut Option<String>,
    group: &str,
    item: &str,
) {
    if last_group.as_deref() != Some(group) {
        push_gap_if_needed(lines);
        lines.push(Line::from(vec![
            Span::styled("  +- ", dim()),
            Span::styled(group.to_string(), bold()),
        ]));
        *last_group = Some(group.to_string());
    }
    lines.push(prefix_block_line(
        "  |  ",
        Line::from(Span::styled(item.to_string(), text_style())),
    ));
}

fn prefix_block_line(prefix: &'static str, line: Line<'static>) -> Line<'static> {
    let mut spans = vec![Span::styled(prefix, dim())];
    spans.extend(line.spans);
    Line::from(spans)
}

fn trim_default_markdown_indent(mut line: Line<'static>) -> Line<'static> {
    if let Some(first) = line.spans.first_mut() {
        if let Some(rest) = first.content.strip_prefix("  ") {
            first.content = rest.to_string().into();
        }
    }
    line
}

fn format_activity_item(item: &str) -> String {
    item.strip_prefix("browsing ")
        .map(|url| format!("opened {url}"))
        .or_else(|| item.strip_prefix("ran ").map(|cmd| cmd.to_string()))
        .or_else(|| {
            item.strip_prefix("read ")
                .map(|path| format!("read {path}"))
        })
        .or_else(|| {
            item.strip_prefix("searched ")
                .map(|query| format!("searched {query}"))
        })
        .or_else(|| {
            item.strip_prefix("modified ")
                .map(|path| format!("modified {path}"))
        })
        .or_else(|| {
            item.strip_prefix("added ")
                .map(|path| format!("added {path}"))
        })
        .or_else(|| {
            item.strip_prefix("deleted ")
                .map(|path| format!("deleted {path}"))
        })
        .unwrap_or_else(|| item.to_string())
}

fn compact_url_for_render(url: &str) -> String {
    url.trim()
        .strip_prefix("https://")
        .or_else(|| url.trim().strip_prefix("http://"))
        .unwrap_or_else(|| url.trim())
        .trim_end_matches('/')
        .to_string()
}

fn compact_path_for_render(path: &str) -> String {
    let trimmed = path.trim();
    trimmed
        .rsplit_once('/')
        .map(|(_, tail)| tail)
        .filter(|tail| !tail.is_empty())
        .unwrap_or(trimmed)
        .to_string()
}

fn agent_started_text_for_render(payload: &serde_json::Value) -> String {
    let label = payload
        .get("nickname")
        .and_then(serde_json::Value::as_str)
        .or_else(|| payload.get("role").and_then(serde_json::Value::as_str))
        .unwrap_or("helper");
    format!("started {label} helper")
}

fn is_browser_activity(item: &str) -> bool {
    item.starts_with("browsing ")
        || item.starts_with("browser ")
        || item == "connected live browser"
}

fn is_command_activity(item: &str) -> bool {
    item.starts_with("ran ") || item.starts_with("command failed")
}

fn is_change_activity(item: &str) -> bool {
    item.starts_with("modified ") || item.starts_with("added ") || item.starts_with("deleted ")
}

fn is_explore_activity(item: &str) -> bool {
    item.starts_with("read ")
        || item.starts_with("searched ")
        || item == "listed files"
        || item.starts_with("started ")
        || item.starts_with("helper ")
}

fn spinner_frame() -> &'static str {
    const FRAMES: [&str; 4] = ["-", "\\", "|", "/"];
    let tick = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() / 160)
        .unwrap_or(0);
    FRAMES[tick as usize % FRAMES.len()]
}

fn setup_status_line(prefix: &str, label: &str, value: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("  [{prefix}] "), accent()),
        Span::styled(format!("{label:<12}"), bold()),
        Span::styled(value.to_string(), muted()),
    ])
}

fn kv_line(label: &str, value: &str) -> Line<'static> {
    Line::from(vec![
        Span::raw("  "),
        Span::styled(format!("{label:<10}"), muted()),
        Span::styled(value.to_string(), text_style()),
    ])
}

fn history_line(row: &HistoryRow, width: usize) -> Line<'static> {
    let task_width = width.saturating_sub(22).max(12);
    Line::from(vec![
        Span::raw("  "),
        Span::styled(
            format!("{:<task_width$}", truncate(&row.task, task_width)),
            text_style(),
        ),
        Span::styled(
            format!("{:<10}", row.status.as_str()),
            status_style(row.status.as_str()),
        ),
        Span::styled(relative_time(row.updated_ms), muted()),
    ])
}

fn history_overlay_line(
    row: &HistoryRow,
    idx: usize,
    selected_row: usize,
    width: usize,
) -> Line<'static> {
    let task_width = width.saturating_sub(22).max(12);
    Line::from(vec![
        Span::styled(
            if idx == selected_row { "> " } else { "  " },
            if idx == selected_row { accent() } else { dim() },
        ),
        Span::styled(
            format!("{:<task_width$}", truncate(&row.task, task_width)),
            text_style(),
        ),
        Span::styled(
            format!("{:<10}", row.status.as_str()),
            status_style(row.status.as_str()),
        ),
        Span::styled(relative_time(row.updated_ms), muted()),
    ])
}

fn selected(text: &str, idx: usize, selected: usize) -> Line<'static> {
    Line::from(vec![
        Span::styled(
            if idx == selected { "> " } else { "  " },
            if idx == selected { accent() } else { dim() },
        ),
        Span::styled(
            text.to_string(),
            if idx == selected {
                bold()
            } else {
                text_style()
            },
        ),
    ])
}

fn markdown_result_lines(markdown: &str, width: u16) -> Vec<Line<'static>> {
    let mut options = Options::empty();
    options.insert(Options::ENABLE_STRIKETHROUGH);
    let parser = Parser::new_ext(markdown, options);
    let mut writer = MarkdownWriter::default();
    for event in parser {
        writer.handle_event(event);
    }
    wrap_markdown_lines(writer.finish(), width as usize)
}

#[derive(Clone, Debug)]
struct ListState {
    next: Option<u64>,
}

#[derive(Default)]
struct MarkdownWriter {
    lines: Vec<Line<'static>>,
    current: Vec<Span<'static>>,
    style_stack: Vec<Style>,
    list_stack: Vec<ListState>,
    link_stack: Vec<String>,
    pending_prefix: Option<String>,
    in_code_block: bool,
}

impl MarkdownWriter {
    fn handle_event(&mut self, event: Event<'_>) {
        match event {
            Event::Start(tag) => self.start_tag(tag),
            Event::End(tag) => self.end_tag(tag),
            Event::Text(text) => self.push_text(&text),
            Event::Code(code) => {
                self.ensure_prefix();
                self.current.push(Span::styled(code.to_string(), muted()));
            }
            Event::SoftBreak | Event::HardBreak => self.flush_current(),
            Event::Rule => {
                self.flush_current();
                self.push_non_duplicate_blank();
                self.lines.push(Line::from(Span::styled("---", muted())));
                self.push_non_duplicate_blank();
            }
            Event::Html(html) | Event::InlineHtml(html) => self.push_text(&html),
            Event::FootnoteReference(text) => self.push_text(&format!("[{text}]")),
            Event::TaskListMarker(checked) => {
                self.ensure_prefix();
                self.current
                    .push(Span::styled(if checked { "[x] " } else { "[ ] " }, muted()));
            }
        }
    }

    fn start_tag(&mut self, tag: Tag<'_>) {
        match tag {
            Tag::Paragraph => {}
            Tag::Heading { level, .. } => {
                self.flush_current();
                self.style_stack.push(heading_style(level));
            }
            Tag::BlockQuote => {
                self.flush_current();
                self.pending_prefix = Some("> ".to_string());
            }
            Tag::CodeBlock(kind) => {
                self.flush_current();
                let label = match kind {
                    CodeBlockKind::Fenced(language) if !language.is_empty() => {
                        format!("code {language}")
                    }
                    _ => "code".to_string(),
                };
                self.lines.push(Line::from(Span::styled(label, muted())));
                self.in_code_block = true;
            }
            Tag::List(start) => {
                self.flush_current();
                self.list_stack.push(ListState { next: start });
            }
            Tag::Item => {
                self.flush_current();
                self.pending_prefix = Some(self.next_list_marker());
            }
            Tag::Emphasis => self.style_stack.push(muted()),
            Tag::Strong => self.style_stack.push(bold()),
            Tag::Strikethrough => self.style_stack.push(muted()),
            Tag::Link { dest_url, .. } => {
                self.link_stack.push(dest_url.to_string());
                self.style_stack.push(link());
            }
            Tag::Image {
                title, dest_url, ..
            } => {
                self.ensure_prefix();
                let label = if title.is_empty() {
                    dest_url.to_string()
                } else {
                    title.to_string()
                };
                self.current
                    .push(Span::styled(format!("[image: {label}]"), muted()));
            }
            Tag::FootnoteDefinition(_)
            | Tag::HtmlBlock
            | Tag::Table(_)
            | Tag::TableHead
            | Tag::TableRow
            | Tag::TableCell
            | Tag::MetadataBlock(_) => {}
        }
    }

    fn end_tag(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Paragraph | TagEnd::Heading(_) | TagEnd::BlockQuote => self.flush_current(),
            TagEnd::CodeBlock => {
                self.flush_current();
                self.in_code_block = false;
            }
            TagEnd::List(_) => {
                self.flush_current();
                self.list_stack.pop();
            }
            TagEnd::Item => self.flush_current(),
            TagEnd::Emphasis | TagEnd::Strong | TagEnd::Strikethrough => {
                self.style_stack.pop();
            }
            TagEnd::Link => {
                self.style_stack.pop();
                if let Some(dest) = self.link_stack.pop() {
                    self.current.push(Span::raw(" ("));
                    self.current.push(Span::styled(dest, link()));
                    self.current.push(Span::raw(")"));
                }
            }
            TagEnd::Image
            | TagEnd::FootnoteDefinition
            | TagEnd::HtmlBlock
            | TagEnd::Table
            | TagEnd::TableHead
            | TagEnd::TableRow
            | TagEnd::TableCell
            | TagEnd::MetadataBlock(_) => {}
        }
    }

    fn push_text(&mut self, text: &str) {
        for (idx, line) in text.lines().enumerate() {
            if idx > 0 {
                self.flush_current();
            }
            self.ensure_prefix();
            let style = if self.in_code_block {
                muted()
            } else {
                self.style_stack.last().copied().unwrap_or_else(text_style)
            };
            if looks_like_bare_link(line) || looks_like_path(line) {
                self.current.push(Span::styled(line.to_string(), link()));
            } else {
                self.current.push(Span::styled(line.to_string(), style));
            }
        }
    }

    fn ensure_prefix(&mut self) {
        if self.current.is_empty() {
            if let Some(prefix) = self.pending_prefix.take() {
                self.current.push(Span::styled(prefix, accent()));
            } else {
                self.current.push(Span::raw("  "));
            }
        }
    }

    fn flush_current(&mut self) {
        if self.current.is_empty() {
            return;
        }
        self.lines
            .push(Line::from(std::mem::take(&mut self.current)));
        self.pending_prefix = None;
    }

    fn push_non_duplicate_blank(&mut self) {
        if self.lines.last().is_some_and(|line| !line.spans.is_empty()) {
            self.lines.push(Line::from(""));
        }
    }

    fn next_list_marker(&mut self) -> String {
        let depth = self.list_stack.len().saturating_sub(1);
        let indent = format!("  {}", "  ".repeat(depth));
        let Some(list) = self.list_stack.last_mut() else {
            return format!("{indent}* ");
        };
        match &mut list.next {
            Some(next) => {
                let marker = format!("{indent}{next}. ");
                *next += 1;
                marker
            }
            None => format!("{indent}* "),
        }
    }

    fn finish(mut self) -> Vec<Line<'static>> {
        self.flush_current();
        if self.lines.is_empty() {
            self.lines.push(Line::from(""));
        }
        self.lines
    }
}

fn wrap_markdown_lines(lines: Vec<Line<'static>>, width: usize) -> Vec<Line<'static>> {
    let width = width.max(24);
    let mut out = Vec::new();
    for line in lines {
        let text = line
            .spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect::<String>();
        if text.chars().count() <= width || text.trim().is_empty() {
            out.push(line);
            continue;
        }
        let continuation_indent = continuation_indent(&text);
        for (idx, part) in wrap_plain(&text, width, &continuation_indent)
            .into_iter()
            .enumerate()
        {
            let style = if idx == 0 {
                line.spans
                    .last()
                    .map(|span| span.style)
                    .unwrap_or_else(text_style)
            } else {
                text_style()
            };
            out.push(Line::from(Span::styled(part, style)));
        }
    }
    out
}

fn continuation_indent(text: &str) -> String {
    let leading = text.chars().take_while(|ch| ch.is_whitespace()).count();
    let trimmed = text.trim_start();
    if let Some(rest) = trimmed.strip_prefix("* ") {
        return format!("{}  ", " ".repeat(leading + trimmed.len() - rest.len() - 2));
    }
    let marker_len = trimmed.chars().take_while(|ch| ch.is_ascii_digit()).count();
    if marker_len > 0 && trimmed.chars().nth(marker_len) == Some('.') {
        return " ".repeat(leading + marker_len + 2);
    }
    " ".repeat(leading)
}

fn wrap_plain(text: &str, width: usize, continuation_indent: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut current = String::new();
    for word in text.split_whitespace() {
        let limit = if lines.is_empty() {
            width
        } else {
            width.saturating_sub(continuation_indent.chars().count())
        };
        let current_len = current.chars().count();
        let word_len = word.chars().count();
        if current_len > 0 && current_len + 1 + word_len > limit {
            lines.push(current);
            current = continuation_indent.to_string();
            current.push_str(word);
        } else {
            if !current.is_empty() {
                current.push(' ');
            } else if !lines.is_empty() {
                current.push_str(continuation_indent);
            }
            current.push_str(word);
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    lines
}

fn heading_style(level: HeadingLevel) -> Style {
    match level {
        HeadingLevel::H1 | HeadingLevel::H2 | HeadingLevel::H3 => bold(),
        HeadingLevel::H4 | HeadingLevel::H5 | HeadingLevel::H6 => text_style(),
    }
}

fn looks_like_bare_link(value: &str) -> bool {
    value.starts_with("http://") || value.starts_with("https://")
}

fn looks_like_path(value: &str) -> bool {
    value.starts_with('/') || value.starts_with("~/")
}

fn append_telemetry_detail_lines(lines: &mut Vec<Line<'static>>, telemetry: &TelemetrySummary) {
    if telemetry.trace_id.is_none() && telemetry.failure.is_none() {
        lines.push(Line::from(Span::styled(
            "No Laminar event for this task.",
            dim(),
        )));
        return;
    }
    if let Some(trace_id) = telemetry.trace_id.as_ref() {
        lines.push(kv_line("trace", trace_id));
    }
    if let Some(backend) = telemetry.backend.as_ref() {
        lines.push(kv_line("backend", backend));
    }
    if let Some(endpoint) = telemetry.endpoint.as_ref() {
        lines.push(kv_line("endpoint", endpoint));
    }
    if let Some(error) = telemetry.failure.as_ref() {
        lines.push(kv_line(
            "status",
            &format!("disabled: {}", truncate(&first_line(error), 120)),
        ));
    }
}

fn browser_ready_label(app: &App, state: &WorkbenchState) -> String {
    if state.browser.status == "not connected" {
        format!("{} ready", app.browser)
    } else {
        format!("{} {}", app.browser, state.browser.status)
    }
}

fn masked_secret(value: &str) -> String {
    if value.is_empty() {
        "paste key here".to_string()
    } else {
        let prefix = value.chars().take(8).collect::<String>();
        format!(
            "{prefix}{}",
            "*".repeat(value.chars().count().saturating_sub(8).max(8))
        )
    }
}

fn auth_secret_label(account: &str) -> &'static str {
    match account {
        "OpenAI API key" => "OpenAI API key",
        "OpenRouter API key" => "OpenRouter API key",
        "Anthropic API key" => "Anthropic API key",
        "Claude Code login" => "Claude Code OAuth token",
        _ => "Credential",
    }
}

fn friendly_error_message(value: &str) -> String {
    let lower = value.to_ascii_lowercase();
    if lower.contains("auth login openrouter") || lower.contains("openrouter_api_key") {
        return "OpenRouter API key is missing.".to_string();
    }
    if lower.contains("auth login openai") || lower.contains("openai_api_key") {
        return "OpenAI API key is missing.".to_string();
    }
    if lower.contains("auth login anthropic") || lower.contains("anthropic_api_key") {
        return "Anthropic API key is missing.".to_string();
    }
    if lower.contains("claude setup-token") || lower.contains("claude_code_oauth_token") {
        return "Claude Code OAuth token is missing.".to_string();
    }
    truncate(&first_line(value), 96)
}

fn failure_actions(error: &str) -> (&'static str, &'static str) {
    let lower = error.to_ascii_lowercase();
    if lower.contains("openrouter") {
        ("Sign in to OpenRouter", "Choose a different model")
    } else if lower.contains("openai") {
        ("Sign in to OpenAI", "Choose a different model")
    } else if lower.contains("anthropic") || lower.contains("claude") {
        ("Sign in", "Choose a different model")
    } else if lower.contains("browser") || lower.contains("chrome") {
        ("Open browser settings", "Choose a different browser")
    } else {
        ("Retry", "Choose a different model")
    }
}

fn relative_time(ms: i64) -> String {
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as i64)
        .unwrap_or(ms);
    let elapsed = now_ms.saturating_sub(ms);
    let seconds = elapsed / 1000;
    if seconds < 60 {
        return "recent".to_string();
    }
    let minutes = seconds / 60;
    if minutes < 60 {
        return format!("{minutes}m ago");
    }
    let hours = minutes / 60;
    if hours < 24 {
        return format!("{hours}h ago");
    }
    let days = hours / 24;
    if days == 1 {
        "yesterday".to_string()
    } else {
        format!("{days}d ago")
    }
}

fn truncate(value: &str, max: usize) -> String {
    if value.chars().count() <= max {
        return value.to_string();
    }
    if max <= 3 {
        return value.chars().take(max).collect();
    }
    let mut out = value.chars().take(max - 3).collect::<String>();
    out.push_str("...");
    out
}

fn first_line(value: &str) -> String {
    value.lines().next().unwrap_or(value).to_string()
}
