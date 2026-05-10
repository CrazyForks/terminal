use anyhow::Result;
use browser_use_protocol::{HistoryRow, SessionMeta, SessionStatus, WorkbenchState};
use ratatui::backend::TestBackend;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Margin, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Clear, List, ListItem, Paragraph, Wrap};
use ratatui::{Frame, Terminal};
use std::time::Instant;

use crate::settings::MODEL_CHOICES;
use crate::theme::*;

use super::{App, Overlay};

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

pub(crate) fn render(frame: &mut Frame<'_>, app: &mut App) {
    let area = frame.area();
    let state = app.workbench_state().unwrap_or_else(|_| WorkbenchState {
        setup_complete: false,
        current_session: None,
        task: None,
        result: None,
        failure: Some("Could not load state.".to_string()),
        activity: Vec::new(),
        browser: Default::default(),
        history: Vec::new(),
    });

    let is_first_run =
        !app.setup_complete && state.history.is_empty() && state.current_session.is_none();
    if is_first_run && app.overlay == Overlay::None {
        render_setup(frame, area, app, true);
    } else if is_first_run
        && matches!(
            app.overlay,
            Overlay::Account | Overlay::Model | Overlay::BrowserChoice | Overlay::SetupComplete
        )
    {
        // Setup steps are full-screen product states, not modals over a workbench.
    } else {
        render_workbench(frame, area, app, &state);
    }

    match app.overlay {
        Overlay::None => {}
        Overlay::Setup => render_setup(frame, centered_rect(78, 20, area), app, false),
        Overlay::Account => render_account_overlay(frame, centered_rect(78, 18, area), app),
        Overlay::Model => render_model_overlay(frame, centered_rect(92, 22, area), app),
        Overlay::Browser => render_browser_overlay(frame, centered_rect(84, 18, area), app, &state),
        Overlay::BrowserChoice => {
            render_browser_choice_overlay(frame, centered_rect(84, 18, area), app)
        }
        Overlay::SetupComplete => render_setup_complete(frame, centered_rect(78, 16, area), app),
        Overlay::History => render_history_overlay(frame, centered_rect(94, 20, area), app, &state),
        Overlay::Actions => render_actions_overlay(frame, centered_rect(72, 16, area), app),
        Overlay::Help => render_help_overlay(frame, centered_rect(78, 14, area)),
        Overlay::Developer => {
            render_developer_overlay(frame, centered_rect(96, 24, area), app, &state)
        }
    }
}

fn render_workbench(frame: &mut Frame<'_>, area: Rect, app: &App, state: &WorkbenchState) {
    let block = Block::bordered()
        .title(workbench_title(app, state, area.width))
        .style(Style::default().fg(text()).bg(background()));
    frame.render_widget(block, area);

    let outer = area.inner(Margin {
        vertical: 1,
        horizontal: 2,
    });
    let composer_h = app.composer_height();
    let footer_h = 1u16;
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(8),
            Constraint::Length(composer_h),
            Constraint::Length(footer_h),
        ])
        .split(outer);

    let content = if let Some(session) = state.current_session.as_ref() {
        if session.status.is_active() {
            running_lines(state)
        } else if session.status == SessionStatus::Cancelled {
            cancelled_lines()
        } else if let Some(error) = state.failure.as_ref() {
            failure_lines(error)
        } else {
            result_lines(state)
        }
    } else {
        ready_lines(app, state)
    };
    frame.render_widget(
        Paragraph::new(content)
            .style(Style::default().fg(text()))
            .wrap(Wrap { trim: false }),
        chunks[0],
    );
    render_composer(frame, chunks[1], app, state.current_session.as_ref());
    render_footer(frame, chunks[2], app, state);
}

fn ready_lines(app: &App, state: &WorkbenchState) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(Span::styled("What should the browser do?", bold())),
        Line::from(""),
        Line::from(""),
        Line::from(Span::styled("Recent", muted())),
        Line::from(""),
    ];
    if state.history.is_empty() {
        lines.push(Line::from(Span::styled("  No previous work yet.", dim())));
    } else {
        for row in state.history.iter().take(3) {
            lines.push(history_line(row, 74));
        }
    }
    lines.push(Line::from(""));
    if let Some(notice) = app.status_notice.as_ref() {
        lines.push(Line::from(Span::styled(
            notice.clone(),
            status_style("failed"),
        )));
        lines.push(Line::from(""));
    }
    let auth_status = if app.auth_notice().ok().flatten().is_some() {
        "needs sign in"
    } else {
        "ready"
    };
    lines.push(Line::from(vec![
        Span::styled("Ready  ", muted()),
        Span::styled(auth_status, text_style()),
        Span::raw("      "),
        Span::styled("browser connected", text_style()),
    ]));
    lines
}

fn running_lines(state: &WorkbenchState) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from("")];
    let activity = if state.activity.is_empty() {
        vec!["starting browser task".to_string()]
    } else {
        state
            .activity
            .iter()
            .rev()
            .take(5)
            .cloned()
            .collect::<Vec<_>>()
    };
    for item in activity.into_iter().rev() {
        lines.push(Line::from(vec![
            Span::styled("* ", accent()),
            Span::styled(item, text_style()),
        ]));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("Browser", bold())));
    lines.push(kv_line(
        "page",
        state.browser.url.as_deref().unwrap_or("connecting"),
    ));
    lines.push(kv_line(
        "open",
        state
            .browser
            .live_url
            .as_deref()
            .map(|_| "live browser")
            .unwrap_or("not available yet"),
    ));
    lines
}

fn result_lines(state: &WorkbenchState) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled("Result", bold())), Line::from("")];
    if let Some(result) = state.result.as_ref() {
        lines.extend(markdown_result_lines(result).into_iter().take(18));
    } else {
        lines.push(Line::from(Span::styled("No result yet.", dim())));
    }
    if let Some(source) = state
        .browser
        .url
        .as_ref()
        .or(state.browser.live_url.as_ref())
    {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled("Source", bold())));
        lines.push(Line::from(Span::styled(source.clone(), link())));
    }
    lines
}

fn failure_lines(error: &str) -> Vec<Line<'static>> {
    let message = friendly_error_message(error);
    vec![
        Line::from(Span::styled("The agent could not finish the task.", bold())),
        Line::from(""),
        Line::from(Span::styled(message, muted())),
        Line::from(""),
        Line::from("> Retry"),
        Line::from("  Sign in"),
        Line::from("  Choose model"),
        Line::from("  Change browser"),
        Line::from(""),
        Line::from(Span::styled("Work preserved in history.", muted())),
    ]
}

fn cancelled_lines() -> Vec<Line<'static>> {
    vec![
        Line::from(Span::styled("The task was stopped.", bold())),
        Line::from(""),
        Line::from(Span::styled("Work preserved in history.", muted())),
        Line::from(""),
        Line::from("> Start a follow-up"),
        Line::from("  Previous work"),
        Line::from("  Setup"),
    ]
}

fn workbench_title(app: &App, state: &WorkbenchState, width: u16) -> String {
    let max_title = width.saturating_sub(4) as usize;
    if let Some(session) = state.current_session.as_ref() {
        let status = session.status.as_str();
        let max_task = max_title.saturating_sub(status.len() + 4).max(12);
        let task = truncate(state.task.as_deref().unwrap_or("browser task"), max_task);
        truncate(&format!(" {task}  {status} "), max_title)
    } else {
        let prefix = " browser-use";
        let details = format!("{}  {}", app.browser, app.model);
        let details = truncate(&details, max_title.saturating_sub(prefix.len() + 2).max(12));
        let spaces = max_title.saturating_sub(prefix.len() + details.len());
        format!("{prefix}{}{}", " ".repeat(spaces), details)
    }
}

fn render_composer(
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
    let text = if app.input.is_empty() {
        vec![Line::from(vec![
            Span::styled("> ", dim()),
            Span::styled("▌ ", accent()),
            Span::styled(placeholder, dim()),
        ])]
    } else {
        let max_lines = area.height.saturating_sub(2).max(1) as usize;
        visible_composer_lines(
            composer_input_lines(&app.input, app.input_cursor),
            max_lines,
        )
    };
    frame.render_widget(
        Paragraph::new(text)
            .block(Block::bordered().style(Style::default().bg(composer_bg())))
            .style(Style::default().bg(composer_bg()))
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn render_footer(frame: &mut Frame<'_>, area: Rect, app: &App, state: &WorkbenchState) {
    let label = if app
        .quit_hint_until
        .is_some_and(|until| Instant::now() <= until)
    {
        "ctrl+c again to quit"
    } else if state
        .current_session
        .as_ref()
        .is_some_and(|session| session.status.is_active())
    {
        "enter steer     ctrl+c stop     f2 browser     / actions"
    } else if state.current_session.is_some() {
        "enter follow-up     f2 browser     tab history     / actions"
    } else {
        "enter run     tab history     / actions     f1 keys"
    };
    frame.render_widget(
        Paragraph::new(label)
            .style(muted())
            .alignment(Alignment::Right),
        area,
    );
}

fn render_setup(frame: &mut Frame<'_>, area: Rect, app: &App, first_run: bool) {
    if !first_run {
        frame.render_widget(Clear, area);
    }
    let inner = if first_run {
        modal(frame, centered_rect(80, 18, area), "browser-use")
    } else {
        modal(frame, area, "Setup")
    };
    let mut lines = vec![
        if first_run {
            Line::from(Span::styled("Set up the browser agent", bold()))
        } else {
            Line::from(Span::styled("The browser agent needs attention.", bold()))
        },
        Line::from(""),
    ];
    if let Some(notice) = app.status_notice.as_ref() {
        lines.push(Line::from(Span::styled(
            notice.clone(),
            status_style("failed"),
        )));
        lines.push(Line::from(""));
    }
    if first_run {
        lines.extend([
            selected(
                &format!(
                    "Sign in                  {}",
                    app.auth_status_for_account(&app.account)
                ),
                0,
                app.selected_row,
            ),
            Line::from(""),
            selected(
                &format!(
                    "Choose model             {}",
                    if app.model_configured {
                        app.model.as_str()
                    } else {
                        "No model selected"
                    }
                ),
                1,
                app.selected_row,
            ),
            Line::from(""),
            selected(
                &format!("Choose browser           {}", app.browser),
                2,
                app.selected_row,
            ),
            Line::from(""),
            Line::from(Span::styled(
                "enter select     tab history     / actions",
                muted(),
            )),
        ]);
    } else {
        lines.extend([
            setup_status_line("ok", "Browser", &format!("{} found", app.browser)),
            Line::from(""),
            setup_status_line("ok", "Sign in", &app.account),
            Line::from(""),
            setup_status_line("ok", "Model", &app.model),
            Line::from(""),
            selected("Sign in", 0, app.selected_row),
            selected("Choose model", 1, app.selected_row),
            selected("Change browser", 2, app.selected_row),
            Line::from(""),
            Line::from(Span::styled("enter fix     esc back", muted())),
        ]);
    }
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

fn render_setup_complete(frame: &mut Frame<'_>, area: Rect, app: &App) {
    frame.render_widget(Clear, area);
    let inner = modal(frame, area, "Ready");
    let auth_state = app
        .auth_notice()
        .ok()
        .flatten()
        .unwrap_or_else(|| format!("Signed in with {}", app.account));
    let lines = vec![
        setup_status_line("ok", "Sign in", &auth_state),
        setup_status_line("ok", "Model", &app.model),
        setup_status_line("ok", "Browser", &app.browser),
        Line::from(""),
        Line::from("> Start using browser-use"),
        Line::from(""),
        Line::from(Span::styled("enter continue", muted())),
    ];
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

fn render_account_overlay(frame: &mut Frame<'_>, area: Rect, app: &App) {
    frame.render_widget(Clear, area);
    let inner = modal(frame, area, "Sign in");
    let mut lines = vec![
        Line::from("Choose how the agent should connect to a model."),
        Line::from(""),
    ];
    if let Some(notice) = app.status_notice.as_ref() {
        lines.push(Line::from(Span::styled(
            notice.clone(),
            status_style("failed"),
        )));
        lines.push(Line::from(""));
    }
    for (idx, account) in super::settings::ACCOUNT_CHOICES.iter().enumerate() {
        lines.push(selected(
            &format!("{account:<24} {}", app.auth_status_for_account(account)),
            idx,
            app.selected_row,
        ));
    }
    lines.extend([
        Line::from(""),
        Line::from(Span::styled("enter select     esc back", muted())),
    ]);
    frame.render_widget(Paragraph::new(lines), inner);
}

fn render_model_overlay(frame: &mut Frame<'_>, area: Rect, app: &App) {
    frame.render_widget(Clear, area);
    let inner = modal(frame, area, "Choose model");
    let mut lines = vec![
        Line::from(Span::styled("Recommended", bold())),
        Line::from(""),
    ];
    for (idx, choice) in MODEL_CHOICES.iter().enumerate() {
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
        Line::from(""),
        Line::from(Span::styled("enter select     esc back", muted())),
    ]);
    frame.render_widget(Paragraph::new(lines), inner);
}

fn render_browser_overlay(frame: &mut Frame<'_>, area: Rect, app: &App, state: &WorkbenchState) {
    frame.render_widget(Clear, area);
    let inner = modal(frame, area, "Browser");
    let mut lines = vec![
        Line::from(Span::styled("Current", bold())),
        kv_line("backend", &app.browser),
        kv_line("title", state.browser.title.as_deref().unwrap_or("unknown")),
        kv_line(
            "page",
            state.browser.url.as_deref().unwrap_or("no page yet"),
        ),
        kv_line("status", &state.browser.status),
        kv_line(
            "live",
            state.browser.live_url.as_deref().unwrap_or("not available"),
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
        selected("Open browser", 0, app.selected_row),
        selected("Reconnect", 1, app.selected_row),
        selected("Change browser", 2, app.selected_row),
        Line::from(""),
        Line::from(Span::styled("enter select     esc close", muted())),
    ];
    if let Some(notice) = app.browser_notice.as_ref() {
        lines.insert(lines.len().saturating_sub(1), Line::from(""));
        lines.insert(
            lines.len().saturating_sub(1),
            Line::from(Span::styled(notice.clone(), muted())),
        );
    }
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

fn render_browser_choice_overlay(frame: &mut Frame<'_>, area: Rect, app: &App) {
    frame.render_widget(Clear, area);
    let inner = modal(frame, area, "Choose browser");
    let lines = vec![
        selected(
            "Local Chrome                 visible browser on this machine",
            0,
            app.selected_row,
        ),
        selected(
            "Browser Use cloud            remote browser with live view",
            1,
            app.selected_row,
        ),
        selected(
            "Headless Chromium            background browser",
            2,
            app.selected_row,
        ),
        Line::from(""),
        Line::from(Span::styled("Current", muted())),
        Line::from(format!("  {} available", app.browser)),
        Line::from(""),
        Line::from(Span::styled("enter select     esc back", muted())),
    ];
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

fn render_history_overlay(frame: &mut Frame<'_>, area: Rect, app: &App, state: &WorkbenchState) {
    frame.render_widget(Clear, area);
    let inner = modal(frame, area, "Previous work");
    let mut lines = if state.history.is_empty() {
        vec![Line::from(Span::styled("No previous work yet.", dim()))]
    } else {
        state
            .history
            .iter()
            .enumerate()
            .map(|(idx, row)| {
                let marker = if idx == app.selected_row { "> " } else { "  " };
                history_overlay_line(row, marker, inner.width.saturating_sub(4) as usize)
            })
            .collect()
    };
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "enter open     r resume     esc close",
        muted(),
    )));
    frame.render_widget(Paragraph::new(lines), inner);
}

fn render_actions_overlay(frame: &mut Frame<'_>, area: Rect, app: &App) {
    frame.render_widget(Clear, area);
    let inner = modal(frame, area, "Actions");
    let items = [
        "New task",
        "Open browser",
        "Previous work",
        "Setup",
        "Choose model",
        "Sign in",
    ];
    let rows = items
        .iter()
        .enumerate()
        .map(|(idx, item)| ListItem::new(selected(item, idx, app.selected_row)))
        .collect::<Vec<_>>();
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(4), Constraint::Length(1)])
        .split(inner);
    frame.render_widget(List::new(rows), chunks[0]);
    frame.render_widget(
        Paragraph::new("enter select     esc close").style(muted()),
        chunks[1],
    );
}

fn render_help_overlay(frame: &mut Frame<'_>, area: Rect) {
    frame.render_widget(Clear, area);
    let inner = modal(frame, area, "Keyboard");
    let rows = vec![
        ("enter", "run, follow up, confirm"),
        ("tab", "previous work"),
        ("f2", "browser"),
        ("/", "actions"),
        ("ctrl+c", "clear input, stop task, or quit"),
        ("esc", "close overlay"),
    ];
    frame.render_widget(
        Paragraph::new(
            rows.into_iter()
                .map(|(k, v)| kv_line(k, v))
                .collect::<Vec<_>>(),
        ),
        inner,
    );
}

fn render_developer_overlay(frame: &mut Frame<'_>, area: Rect, app: &App, state: &WorkbenchState) {
    frame.render_widget(Clear, area);
    let inner = modal(frame, area, "Developer");
    let mut lines = vec![Line::from(Span::styled("Events", bold())), Line::from("")];
    let Some(session) = state.current_session.as_ref() else {
        lines.push(Line::from(Span::styled("No task selected.", dim())));
        frame.render_widget(Paragraph::new(lines), inner);
        return;
    };
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
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("esc close", muted())));
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
}

fn setup_status_line(prefix: &str, label: &str, value: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("[{prefix}] "), accent()),
        Span::styled(format!("{label:<14}"), bold()),
        Span::styled(value.to_string(), muted()),
    ])
}

fn kv_line(label: &str, value: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{label:<10}"), muted()),
        Span::styled(value.to_string(), text_style()),
    ])
}

fn history_line(row: &HistoryRow, width: usize) -> Line<'static> {
    let task_width = width.saturating_sub(20).max(12);
    Line::from(vec![
        Span::styled("> ", dim()),
        Span::styled(
            format!("{:<task_width$}", truncate(&row.task, task_width)),
            text_style(),
        ),
        Span::styled(
            format!("{:<10}", row.status.as_str()),
            status_style(row.status.as_str()),
        ),
        Span::styled("recent", muted()),
    ])
}

fn history_overlay_line(row: &HistoryRow, marker: &str, width: usize) -> Line<'static> {
    let task_width = width.saturating_sub(20).max(12);
    Line::from(vec![
        Span::styled(marker.to_string(), dim()),
        Span::styled(
            format!("{:<task_width$}", truncate(&row.task, task_width)),
            text_style(),
        ),
        Span::styled(
            format!("{:<10}", row.status.as_str()),
            status_style(row.status.as_str()),
        ),
        Span::styled("recent", muted()),
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

fn composer_input_lines(input: &str, cursor: usize) -> Vec<Line<'static>> {
    let chars = input.chars().collect::<Vec<_>>();
    let cursor = cursor.min(chars.len());
    let mut out = Vec::new();
    let mut global = 0usize;

    for (idx, source_line) in input.split('\n').enumerate() {
        let line_len = source_line.chars().count();
        let prefix = if idx == 0 { "> " } else { "  " };
        if cursor >= global && cursor <= global + line_len {
            let local = cursor - global;
            let before = source_line.chars().take(local).collect::<String>();
            let after = source_line.chars().skip(local).collect::<String>();
            out.push(Line::from(vec![
                Span::styled(prefix, accent()),
                Span::styled(before, bold()),
                Span::styled("▌", accent()),
                Span::styled(after, bold()),
            ]));
        } else {
            out.push(Line::from(vec![
                Span::styled(prefix, accent()),
                Span::styled(source_line.to_string(), bold()),
            ]));
        }
        global += line_len + 1;
    }

    if out.is_empty() {
        out.push(Line::from(vec![
            Span::styled("> ", accent()),
            Span::styled("▌", accent()),
        ]));
    }

    out
}

fn visible_composer_lines(mut lines: Vec<Line<'static>>, max_lines: usize) -> Vec<Line<'static>> {
    if lines.len() <= max_lines {
        return lines;
    }
    let start = lines.len().saturating_sub(max_lines);
    lines.drain(0..start);
    lines
}

fn markdown_result_lines(markdown: &str) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    let mut in_code = false;

    for source in markdown.lines() {
        let trimmed = source.trim_start();
        if trimmed.starts_with("```") {
            in_code = !in_code;
            lines.push(Line::from(Span::styled("code", muted())));
            continue;
        }
        if in_code {
            lines.push(Line::from(Span::styled(source.to_string(), muted())));
            continue;
        }
        if trimmed.is_empty() {
            lines.push(Line::from(""));
            continue;
        }
        if let Some(text) = trimmed.strip_prefix("### ") {
            lines.push(Line::from(Span::styled(text.to_string(), bold())));
            continue;
        }
        if let Some(text) = trimmed.strip_prefix("## ") {
            lines.push(Line::from(Span::styled(text.to_string(), bold())));
            continue;
        }
        if let Some(text) = trimmed.strip_prefix("# ") {
            lines.push(Line::from(Span::styled(text.to_string(), bold())));
            continue;
        }
        if let Some(text) = trimmed
            .strip_prefix("- ")
            .or_else(|| trimmed.strip_prefix("* "))
        {
            let mut spans = vec![Span::styled("• ", accent())];
            spans.extend(inline_markdown_spans(text));
            lines.push(Line::from(spans));
            continue;
        }
        if let Some((marker, text)) = numbered_markdown_item(trimmed) {
            let mut spans = vec![Span::styled(format!("{marker} "), accent())];
            spans.extend(inline_markdown_spans(text));
            lines.push(Line::from(spans));
            continue;
        }
        lines.push(Line::from(inline_markdown_spans(source)));
    }

    if lines.is_empty() {
        lines.push(Line::from(""));
    }
    lines
}

fn numbered_markdown_item(value: &str) -> Option<(&str, &str)> {
    let (marker, text) = value.split_once(' ')?;
    if marker.ends_with('.')
        && marker[..marker.len().saturating_sub(1)]
            .parse::<usize>()
            .is_ok()
    {
        Some((marker, text))
    } else {
        None
    }
}

fn inline_markdown_spans(value: &str) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    let mut rest = value;

    while let Some(start) = rest.find('[') {
        push_inline_text(&mut spans, &rest[..start]);
        let after_open = &rest[start + 1..];
        let Some(label_end) = after_open.find("](") else {
            rest = &rest[start..];
            break;
        };
        let after_label = &after_open[label_end + 2..];
        let Some(url_end) = after_label.find(')') else {
            rest = &rest[start..];
            break;
        };
        let label = &after_open[..label_end];
        let url = &after_label[..url_end];
        spans.push(Span::styled(label.to_string(), link()));
        spans.push(Span::raw(" ("));
        spans.push(Span::styled(url.to_string(), link()));
        spans.push(Span::raw(")"));
        rest = &after_label[url_end + 1..];
    }

    push_inline_text(&mut spans, rest);
    spans
}

fn push_inline_text(spans: &mut Vec<Span<'static>>, value: &str) {
    let mut rest = value;
    while let Some(start) = rest.find('`') {
        if start > 0 {
            spans.extend(bare_link_spans(&rest[..start]));
        }
        let after_open = &rest[start + 1..];
        let Some(end) = after_open.find('`') else {
            spans.extend(bare_link_spans(&rest[start..]));
            return;
        };
        spans.push(Span::styled(after_open[..end].to_string(), muted()));
        rest = &after_open[end + 1..];
    }
    spans.extend(bare_link_spans(rest));
}

fn bare_link_spans(value: &str) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    let mut rest = value;
    loop {
        let http = rest.find("http://");
        let https = rest.find("https://");
        let Some(start) = [http, https].into_iter().flatten().min() else {
            if !rest.is_empty() {
                spans.push(Span::styled(rest.to_string(), text_style()));
            }
            break;
        };
        if start > 0 {
            spans.push(Span::styled(rest[..start].to_string(), text_style()));
        }
        let tail = &rest[start..];
        let end = tail.find(char::is_whitespace).unwrap_or_else(|| tail.len());
        spans.push(Span::styled(tail[..end].to_string(), link()));
        rest = &tail[end..];
    }
    spans
}

fn modal(frame: &mut Frame<'_>, area: Rect, title: &str) -> Rect {
    let block = Block::bordered()
        .title(title.to_string())
        .style(Style::default().fg(text()).bg(panel()));
    frame.render_widget(block, area);
    area.inner(Margin {
        vertical: 1,
        horizontal: 2,
    })
}

fn centered_rect(width: u16, height: u16, area: Rect) -> Rect {
    let width = width.min(area.width);
    let height = height.min(area.height);
    Rect {
        x: area.x + area.width.saturating_sub(width) / 2,
        y: area.y + area.height.saturating_sub(height) / 2,
        width,
        height,
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

fn friendly_error_message(value: &str) -> String {
    let lower = value.to_ascii_lowercase();
    if lower.contains("auth login openrouter") || lower.contains("openrouter_api_key") {
        return "OpenRouter API key is missing. Sign in before retrying.".to_string();
    }
    if lower.contains("auth login openai") || lower.contains("openai_api_key") {
        return "OpenAI API key is missing. Sign in before retrying.".to_string();
    }
    if lower.contains("auth login anthropic") || lower.contains("anthropic_api_key") {
        return "Anthropic API key is missing. Sign in before retrying.".to_string();
    }
    if lower.contains("claude setup-token") || lower.contains("claude_code_oauth_token") {
        return "Claude Code login needs an OAuth token before retrying.".to_string();
    }
    truncate(&first_line(value), 96)
}
