use std::fmt;
use std::io::{self, Write};
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use browser_use_protocol::{project_workbench, SessionStatus, WorkbenchState};
use browser_use_store::Store;
use clap::{Parser, ValueEnum};
use crossterm::event::{
    self, DisableBracketedPaste, EnableBracketedPaste, Event as TermEvent, KeyCode, KeyEvent,
    KeyEventKind, KeyModifiers, KeyboardEnhancementFlags, PopKeyboardEnhancementFlags,
    PushKeyboardEnhancementFlags,
};
use crossterm::execute;
use crossterm::terminal::{disable_raw_mode, enable_raw_mode};
use crossterm::Command;
use ratatui::backend::CrosstermBackend;
use ratatui::widgets::{Paragraph, Widget};
use ratatui::{Terminal, TerminalOptions, Viewport};

mod composer;
mod palette;
mod render;
mod runtime;
mod settings;
mod theme;

use composer::Composer;
use palette::{Palette, PaletteAction};
use render::{lines_plain_text, native_scrollback_lines, render, render_dump};
use runtime::run_agent_thread;
use settings::{
    provider_model_for_display, AgentBackend, ACCOUNT_CHOICES, BROWSER_CHOICES, MODEL_CHOICES,
};

#[derive(Debug, Parser)]
#[command(name = "but", bin_name = "but")]
struct Args {
    #[arg(long, default_value = ".browser-use-terminal")]
    state_dir: PathBuf,
    #[arg(long, default_value = "GPT-5.5")]
    model: String,
    #[arg(long, default_value = "Codex login")]
    account: String,
    #[arg(long, default_value = "Browser Use cloud")]
    browser: String,
    #[arg(long)]
    dump_screen: bool,
    #[arg(long, default_value_t = 120)]
    width: u16,
    #[arg(long, default_value_t = 28)]
    height: u16,
    #[arg(long)]
    select_latest: bool,
    #[arg(long)]
    seed_demo: Option<String>,
    #[arg(long, value_enum)]
    overlay: Option<ScreenArg>,
    #[arg(long, value_enum, default_value = "codex", hide = true)]
    agent: AgentBackend,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Surface {
    Main,
    Setup,
    Account,
    ApiKey,
    Telemetry,
    Model,
    Browser,
    BrowserSelect,
    History,
    Actions,
    Developer,
}

#[derive(Clone, Copy, Debug, ValueEnum)]
enum ScreenArg {
    Setup,
    Account,
    Telemetry,
    Model,
    Browser,
    History,
    Actions,
    Developer,
}

impl From<ScreenArg> for Surface {
    fn from(value: ScreenArg) -> Self {
        match value {
            ScreenArg::Setup => Self::Setup,
            ScreenArg::Account => Self::Account,
            ScreenArg::Telemetry => Self::Telemetry,
            ScreenArg::Model => Self::Model,
            ScreenArg::Browser => Self::Browser,
            ScreenArg::History => Self::History,
            ScreenArg::Actions => Self::Actions,
            ScreenArg::Developer => Self::Developer,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ProductState {
    SetupNeeded,
    Ready,
    Running,
    Result,
    Failed,
    Cancelled,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum AppCommand {
    StartTask(String),
    SendFollowup { session_id: String, text: String },
    RetryTask(String),
    OpenBrowser,
    ReconnectBrowser,
    NewTask,
    OpenHistory,
    SelectHistory(String),
    ChangeModel,
    SignIn,
    ConfigureTelemetry,
    ChangeBrowser,
    SaveAccount(String),
    SaveModel(usize),
    SaveBrowser(usize),
    SaveAuth(String),
    SaveTelemetry(String),
}

struct App {
    store: Store,
    args: Args,
    selected_session_id: Option<String>,
    composer: Composer,
    surface: Surface,
    palette: Palette,
    selected_row: usize,
    setup_complete: bool,
    account: String,
    model: String,
    model_configured: bool,
    provider_model: String,
    browser: String,
    api_key_account: Option<String>,
    pending_model_after_auth: Option<usize>,
    browser_notice: Option<String>,
    status_notice: Option<String>,
    agent_backend: AgentBackend,
    quit_hint_until: Option<Instant>,
    last_native_snapshot: Option<String>,
}

impl App {
    fn new(args: Args) -> Result<Self> {
        let store = Store::open(&args.state_dir)?;
        seed_demo_if_requested(&store, args.seed_demo.as_deref())?;
        let selected_session_id = if args.select_latest {
            store
                .list_sessions()?
                .first()
                .map(|session| session.id.clone())
        } else {
            None
        };
        let surface = args.overlay.map(Into::into).unwrap_or(Surface::Main);
        let setup_complete = store.get_setting("setup.complete")?.as_deref() == Some("1");
        let account = store
            .get_setting("account")?
            .unwrap_or_else(|| args.account.clone());
        let stored_model = store.get_setting("model")?;
        let had_stored_model = stored_model.is_some();
        let model_configured = had_stored_model || setup_complete;
        let model = stored_model.unwrap_or_else(|| args.model.clone());
        let provider_model = store
            .get_setting("provider.model")?
            .unwrap_or_else(|| provider_model_for_display(&model).to_string());
        let browser = store
            .get_setting("browser")?
            .unwrap_or_else(|| args.browser.clone());
        let agent_backend = store
            .get_setting("agent.backend")?
            .and_then(|value| AgentBackend::from_setting(&value))
            .unwrap_or(args.agent);
        let selected_row = if surface == Surface::Main
            && !setup_complete
            && !had_stored_model
            && selected_session_id.is_none()
            && store.list_sessions()?.is_empty()
        {
            1
        } else {
            0
        };
        Ok(Self {
            store,
            args,
            selected_session_id,
            composer: Composer::default(),
            surface,
            palette: Palette::default(),
            selected_row,
            setup_complete,
            account,
            model,
            model_configured,
            provider_model,
            browser,
            api_key_account: None,
            pending_model_after_auth: None,
            browser_notice: None,
            status_notice: None,
            agent_backend,
            quit_hint_until: None,
            last_native_snapshot: None,
        })
    }

    fn workbench_state(&self) -> Result<WorkbenchState> {
        let sessions = self.store.list_sessions()?;
        let current_id = self.selected_session_id.as_deref();
        let current_events = current_id
            .map(|id| self.store.events_for_session(id))
            .transpose()?
            .unwrap_or_default();
        let all_events = sessions
            .iter()
            .map(|session| {
                self.store
                    .events_for_session(&session.id)
                    .map(|events| (session.id.clone(), events))
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(project_workbench(
            &sessions,
            &current_events,
            &all_events,
            current_id,
            self.browser.clone(),
        ))
    }

    fn open_surface(&mut self, surface: Surface) {
        self.surface = surface;
        self.selected_row = 0;
        if surface != Surface::Browser {
            self.browser_notice = None;
        }
        if surface != Surface::Actions {
            self.palette.clear();
        }
    }

    fn close_surface(&mut self) {
        self.surface = Surface::Main;
        self.selected_row = 0;
        self.browser_notice = None;
        self.palette.clear();
    }

    fn submit(&mut self) -> Result<()> {
        let text = self.composer.take_trimmed();
        if text.is_empty() {
            if let Some(session) = self
                .selected_session_id
                .as_deref()
                .and_then(|id| self.store.load_session(id).ok().flatten())
            {
                if session.status == SessionStatus::Failed {
                    self.execute_failed_selection(session.id)?;
                } else if session.status == SessionStatus::Cancelled {
                    self.execute_cancelled_selection()?;
                }
            }
            return Ok(());
        }
        if text == "/" {
            self.open_surface(Surface::Actions);
            return Ok(());
        }
        if let Some(session) = self
            .selected_session_id
            .as_deref()
            .and_then(|id| self.store.load_session(id).ok().flatten())
        {
            self.dispatch(AppCommand::SendFollowup {
                session_id: session.id,
                text,
            })?;
            return Ok(());
        }
        self.dispatch(AppCommand::StartTask(text))?;
        Ok(())
    }

    fn ensure_agent_ready(&mut self) -> Result<bool> {
        if let Some(notice) = self.auth_notice()? {
            self.status_notice = Some(notice);
            self.open_surface(Surface::Account);
            return Ok(false);
        }
        self.status_notice = None;
        Ok(true)
    }

    fn dispatch(&mut self, command: AppCommand) -> Result<()> {
        match command {
            AppCommand::StartTask(text) => {
                if !self.ensure_agent_ready()? {
                    return Ok(());
                }
                let session = self.store.create_session(None, std::env::current_dir()?)?;
                self.store.append_event(
                    &session.id,
                    "session.input",
                    serde_json::json!({ "text": text }),
                )?;
                self.store.append_event(
                    &session.id,
                    "browser.page",
                    serde_json::json!({
                        "url": "about:blank",
                        "title": "Browser ready",
                        "status": "connected",
                    }),
                )?;
                self.selected_session_id = Some(session.id.clone());
                self.last_native_snapshot = None;
                self.start_agent_for_session(session.id)?;
            }
            AppCommand::SendFollowup { session_id, text } => {
                let active = self
                    .store
                    .load_session(&session_id)?
                    .is_some_and(|session| session.status.is_active());
                if !active && !self.ensure_agent_ready()? {
                    return Ok(());
                }
                self.store.append_event(
                    &session_id,
                    "session.followup",
                    serde_json::json!({ "text": text }),
                )?;
                self.last_native_snapshot = None;
                if !active {
                    self.start_agent_for_session(session_id)?;
                }
            }
            AppCommand::RetryTask(session_id) => {
                if !self.ensure_agent_ready()? {
                    return Ok(());
                }
                self.start_agent_for_session(session_id)?;
            }
            AppCommand::OpenBrowser => self.request_open_browser()?,
            AppCommand::ReconnectBrowser => self.request_reconnect_browser()?,
            AppCommand::NewTask => {
                self.selected_session_id = None;
                self.last_native_snapshot = None;
                self.close_surface();
            }
            AppCommand::OpenHistory => self.open_surface(Surface::History),
            AppCommand::SelectHistory(session_id) => {
                self.selected_session_id = Some(session_id);
                self.last_native_snapshot = None;
                self.close_surface();
            }
            AppCommand::ChangeModel => self.open_surface(Surface::Model),
            AppCommand::SignIn => self.open_surface(Surface::Account),
            AppCommand::ConfigureTelemetry => self.start_telemetry_entry(),
            AppCommand::ChangeBrowser => self.open_surface(Surface::BrowserSelect),
            AppCommand::SaveAccount(account) => self.save_account(account)?,
            AppCommand::SaveModel(index) => self.save_model(index)?,
            AppCommand::SaveBrowser(index) => self.save_browser(index)?,
            AppCommand::SaveAuth(secret) => self.save_auth(secret)?,
            AppCommand::SaveTelemetry(secret) => self.save_telemetry(secret)?,
        }
        Ok(())
    }

    fn start_agent_for_session(&self, session_id: String) -> Result<()> {
        if matches!(self.agent_backend, AgentBackend::None) {
            return Ok(());
        }
        let state_dir = self.args.state_dir.clone();
        let backend = self.agent_backend;
        let model = self.provider_model.clone();
        let browser = self.browser.clone();
        thread::Builder::new()
            .name(format!("browser-use-agent-{session_id}"))
            .spawn(move || {
                if let Err(error) = run_agent_thread(state_dir, session_id, backend, model, browser)
                {
                    eprintln!("agent thread failed: {error:#}");
                }
            })
            .context("spawn agent thread")?;
        Ok(())
    }

    fn complete_demo_result(&mut self) -> Result<()> {
        let Some(id) = self.selected_session_id.clone() else {
            return Ok(());
        };
        self.store.append_event(
            &id,
            "session.done",
            serde_json::json!({"result": "Demo result from the Rust event store.\n\nThe browser task state is now rendered from SQLite."}),
        )?;
        Ok(())
    }

    fn cancel_current_task(&mut self) -> Result<bool> {
        let Some(id) = self.selected_session_id.clone() else {
            return Ok(false);
        };
        let Some(session) = self.store.load_session(&id)? else {
            return Ok(false);
        };
        if !session.status.is_active() {
            return Ok(false);
        }
        self.store.request_cancel(&id, "stopped from terminal")?;
        Ok(true)
    }

    fn handle_key(&mut self, key: KeyEvent) -> Result<bool> {
        if !matches!(key.kind, KeyEventKind::Press | KeyEventKind::Repeat) {
            return Ok(false);
        }
        match key {
            KeyEvent {
                code: KeyCode::Char('q'),
                modifiers: KeyModifiers::CONTROL,
                ..
            } => return Ok(true),
            KeyEvent {
                code: KeyCode::Char('c'),
                modifiers: KeyModifiers::CONTROL,
                ..
            } => {
                if !self.composer.is_empty() {
                    self.composer.clear();
                } else if self.cancel_current_task()? {
                    self.quit_hint_until = None;
                } else if self
                    .quit_hint_until
                    .is_some_and(|until| Instant::now() <= until)
                {
                    return Ok(true);
                } else {
                    self.quit_hint_until = Some(Instant::now() + Duration::from_millis(1500));
                }
            }
            KeyEvent {
                code: KeyCode::Esc, ..
            } => self.close_surface(),
            KeyEvent {
                code: KeyCode::Tab, ..
            } => self.open_surface(Surface::History),
            KeyEvent {
                code: KeyCode::F(1),
                ..
            } => {}
            KeyEvent {
                code: KeyCode::F(2),
                ..
            } => self.open_surface(Surface::Browser),
            KeyEvent {
                code: KeyCode::Char('e'),
                modifiers: KeyModifiers::CONTROL,
                ..
            } if self.composer.is_empty() => self.open_surface(Surface::Developer),
            KeyEvent {
                code: KeyCode::Char('/'),
                modifiers: KeyModifiers::NONE,
                ..
            } if self.composer.is_empty() => self.open_surface(Surface::Actions),
            KeyEvent {
                code: KeyCode::Char('r'),
                modifiers: KeyModifiers::NONE,
                ..
            } if self.surface == Surface::History => self.resume_selected_history()?,
            KeyEvent {
                code: KeyCode::Up, ..
            } if self.surface == Surface::Main && self.composer.handle_key(key) => {}
            KeyEvent {
                code: KeyCode::Down,
                ..
            } if self.surface == Surface::Main && self.composer.handle_key(key) => {}
            KeyEvent {
                code: KeyCode::Up, ..
            } if self.surface != Surface::Main
                || self.is_first_run_setup_visible()?
                || self.main_selection_count()? > 0 =>
            {
                self.move_selection(-1)?
            }
            KeyEvent {
                code: KeyCode::Down,
                ..
            } if self.surface != Surface::Main
                || self.is_first_run_setup_visible()?
                || self.main_selection_count()? > 0 =>
            {
                self.move_selection(1)?
            }
            KeyEvent {
                code: KeyCode::Up, ..
            } if self.surface == Surface::Main => {}
            KeyEvent {
                code: KeyCode::Down,
                ..
            } if self.surface == Surface::Main => {}
            KeyEvent {
                code: KeyCode::Enter,
                modifiers: KeyModifiers::NONE,
                ..
            } if self.is_first_run_setup_visible()? => self.execute_first_run_setup_selection()?,
            KeyEvent {
                code: KeyCode::Enter,
                modifiers: KeyModifiers::NONE,
                ..
            } if self.surface != Surface::Main => self.execute_surface_selection()?,
            KeyEvent {
                code: KeyCode::Enter,
                modifiers: KeyModifiers::NONE,
                ..
            } => self.submit()?,
            _ if self.surface == Surface::Actions && self.palette.handle_filter_key(key) => {
                self.selected_row = 0;
            }
            _ if matches!(self.surface, Surface::ApiKey | Surface::Telemetry)
                && self.handle_api_key_key(key) => {}
            _ if self.surface == Surface::Main && self.composer.handle_key(key) => {}
            KeyEvent {
                code: KeyCode::Char('d'),
                modifiers: KeyModifiers::CONTROL,
                ..
            } => self.complete_demo_result()?,
            _ => {}
        }
        Ok(false)
    }

    fn handle_paste(&mut self, text: &str) {
        match self.surface {
            Surface::Main | Surface::ApiKey | Surface::Telemetry => {
                self.composer.insert_paste(text);
            }
            Surface::Actions => {
                self.palette.push_filter_str(text);
                self.selected_row = 0;
            }
            _ => {}
        }
    }

    fn is_first_run_setup_visible(&self) -> Result<bool> {
        Ok(!self.setup_complete
            && self.surface == Surface::Main
            && self.selected_session_id.is_none()
            && self.composer.is_empty()
            && self.store.list_sessions()?.is_empty())
    }

    fn execute_surface_selection(&mut self) -> Result<()> {
        match self.surface {
            Surface::Actions => {
                if let Some(action) = self.palette.selected_action(self.selected_row) {
                    self.execute_palette_action(action)?;
                }
            }
            Surface::History => {
                let sessions = self.store.list_sessions()?;
                if let Some(session) =
                    sessions.get(self.selected_row.min(sessions.len().saturating_sub(1)))
                {
                    self.dispatch(AppCommand::SelectHistory(session.id.clone()))?;
                }
            }
            Surface::Setup => match self
                .selected_row
                .min(self.setup_row_count().saturating_sub(1))
            {
                0 => self.dispatch(AppCommand::SignIn)?,
                1 => self.dispatch(AppCommand::ChangeModel)?,
                2 => self.dispatch(AppCommand::ChangeBrowser)?,
                _ => {
                    self.setup_complete = true;
                    self.store.set_setting("setup.complete", "1")?;
                    self.persist_runtime_settings()?;
                    self.close_surface();
                }
            },
            Surface::Account => {
                let account = ACCOUNT_CHOICES
                    .get(
                        self.selected_row
                            .min(ACCOUNT_CHOICES.len().saturating_sub(1)),
                    )
                    .unwrap_or(&ACCOUNT_CHOICES[0])
                    .to_string();
                self.dispatch(AppCommand::SaveAccount(account))?;
            }
            Surface::ApiKey => {
                let secret = self.composer.take_trimmed();
                self.dispatch(AppCommand::SaveAuth(secret))?;
            }
            Surface::Telemetry => {
                let secret = self.composer.take_trimmed();
                self.dispatch(AppCommand::SaveTelemetry(secret))?;
            }
            Surface::Model => {
                self.dispatch(AppCommand::SaveModel(self.selected_row))?;
            }
            Surface::Browser => match self.selected_row.min(2) {
                0 => self.dispatch(AppCommand::OpenBrowser)?,
                1 => self.dispatch(AppCommand::ReconnectBrowser)?,
                _ => self.dispatch(AppCommand::ChangeBrowser)?,
            },
            Surface::BrowserSelect => {
                self.dispatch(AppCommand::SaveBrowser(self.selected_row))?;
            }
            Surface::Developer => match self.selected_row.min(1) {
                0 => self.dispatch(AppCommand::ConfigureTelemetry)?,
                _ => self.close_surface(),
            },
            Surface::Main => {
                self.close_surface();
            }
        }
        Ok(())
    }

    fn execute_first_run_setup_selection(&mut self) -> Result<()> {
        match self
            .selected_row
            .min(self.setup_row_count().saturating_sub(1))
        {
            0 => self.dispatch(AppCommand::SignIn)?,
            1 => self.dispatch(AppCommand::ChangeModel)?,
            2 => self.dispatch(AppCommand::ChangeBrowser)?,
            _ => {
                self.setup_complete = true;
                self.store.set_setting("setup.complete", "1")?;
                self.persist_runtime_settings()?;
                self.close_surface();
            }
        }
        Ok(())
    }

    fn resume_selected_history(&mut self) -> Result<()> {
        let sessions = self.store.list_sessions()?;
        if let Some(session) = sessions.get(self.selected_row.min(sessions.len().saturating_sub(1)))
        {
            self.dispatch(AppCommand::SelectHistory(session.id.clone()))?;
        }
        Ok(())
    }

    fn execute_failed_selection(&mut self, session_id: String) -> Result<()> {
        let state = self.workbench_state()?;
        let error = state.failure.as_deref().unwrap_or_default();
        match self.selected_row.min(3) {
            0 if error.to_ascii_lowercase().contains("browser") => {
                self.open_surface(Surface::Browser)
            }
            0 if self.auth_notice()?.is_some() => self.open_surface(Surface::Account),
            0 => self.dispatch(AppCommand::RetryTask(session_id))?,
            1 if error.to_ascii_lowercase().contains("browser") => {
                self.open_surface(Surface::BrowserSelect)
            }
            1 => self.open_surface(Surface::Model),
            2 => self.dispatch(AppCommand::RetryTask(session_id))?,
            _ => self.dispatch(AppCommand::NewTask)?,
        }
        Ok(())
    }

    fn execute_cancelled_selection(&mut self) -> Result<()> {
        match self.selected_row.min(2) {
            0 => {}
            1 => self.dispatch(AppCommand::NewTask)?,
            _ => self.dispatch(AppCommand::OpenHistory)?,
        }
        Ok(())
    }

    fn execute_palette_action(&mut self, action: PaletteAction) -> Result<()> {
        match action {
            PaletteAction::NewTask => self.dispatch(AppCommand::NewTask)?,
            PaletteAction::OpenBrowser => self.dispatch(AppCommand::OpenBrowser)?,
            PaletteAction::ReconnectBrowser => self.dispatch(AppCommand::ReconnectBrowser)?,
            PaletteAction::PreviousWork => self.dispatch(AppCommand::OpenHistory)?,
            PaletteAction::ChooseModel => self.dispatch(AppCommand::ChangeModel)?,
            PaletteAction::SignIn => self.dispatch(AppCommand::SignIn)?,
            PaletteAction::ConfigureLaminar => self.dispatch(AppCommand::ConfigureTelemetry)?,
        }
        Ok(())
    }

    fn save_account(&mut self, account: String) -> Result<()> {
        self.account = account.clone();
        if self.account == "Codex login" {
            self.persist_runtime_settings()?;
            self.status_notice = Some("Codex login selected.".to_string());
            self.open_surface(Surface::Model);
            return Ok(());
        }
        self.start_auth_entry(account);
        Ok(())
    }

    fn save_model(&mut self, index: usize) -> Result<()> {
        let choice = MODEL_CHOICES
            .get(index.min(MODEL_CHOICES.len().saturating_sub(1)))
            .unwrap_or(&MODEL_CHOICES[0]);
        self.model = choice.display.to_string();
        self.account = choice.account.to_string();
        self.provider_model = choice.provider_model.to_string();
        self.agent_backend = choice.backend;
        self.model_configured = true;
        self.persist_runtime_settings()?;
        if !self.account_ready(&self.account)? {
            self.pending_model_after_auth = Some(index);
            self.start_auth_entry(self.account.clone());
            return Ok(());
        }
        self.status_notice = Some(format!("Model set to {}.", self.model));
        if !self.setup_complete {
            self.open_surface(Surface::BrowserSelect);
        } else {
            self.close_surface();
        }
        Ok(())
    }

    fn save_browser(&mut self, index: usize) -> Result<()> {
        let choice = BROWSER_CHOICES
            .get(index.min(BROWSER_CHOICES.len().saturating_sub(1)))
            .unwrap_or(&BROWSER_CHOICES[0]);
        self.browser = (*choice).to_string();
        self.persist_runtime_settings()?;
        self.status_notice = Some(format!("Browser set to {}.", self.browser));
        if !self.setup_complete && self.model_configured && self.account_ready(&self.account)? {
            self.setup_complete = true;
            self.store.set_setting("setup.complete", "1")?;
            self.close_surface();
        } else if !self.setup_complete {
            self.open_surface(Surface::Setup);
        } else {
            self.close_surface();
        }
        Ok(())
    }

    fn save_auth(&mut self, secret: String) -> Result<()> {
        let Some(account) = self.api_key_account.clone() else {
            self.open_surface(Surface::Account);
            return Ok(());
        };
        if secret.trim().is_empty() {
            self.status_notice = Some(format!("{} is required.", auth_secret_label(&account)));
            self.open_surface(Surface::ApiKey);
            return Ok(());
        }
        self.store
            .set_setting(auth_setting_key(&account), secret.trim())?;
        self.account = account.clone();
        self.persist_runtime_settings()?;
        self.api_key_account = None;
        self.status_notice = Some(format!("Saved {}.", auth_secret_label(&account)));
        if let Some(index) = self.pending_model_after_auth.take() {
            self.selected_row = index;
            self.open_surface(Surface::Model);
        } else {
            self.open_surface(Surface::Model);
        }
        Ok(())
    }

    fn start_auth_entry(&mut self, account: String) {
        self.api_key_account = Some(account);
        self.composer.clear();
        self.open_surface(Surface::ApiKey);
    }

    fn start_telemetry_entry(&mut self) {
        self.composer.clear();
        self.open_surface(Surface::Telemetry);
    }

    fn save_telemetry(&mut self, secret: String) -> Result<()> {
        if secret.trim().is_empty() {
            self.status_notice = Some("Laminar API key is required.".to_string());
            self.open_surface(Surface::Telemetry);
            return Ok(());
        }
        self.store
            .set_setting(LAMINAR_API_KEY_SETTING, secret.trim())?;
        self.status_notice = Some("Saved Laminar API key.".to_string());
        self.open_surface(Surface::Developer);
        Ok(())
    }

    fn handle_api_key_key(&mut self, key: KeyEvent) -> bool {
        self.composer.handle_key(key)
    }

    fn setup_row_count(&self) -> usize {
        if self.model_configured {
            4
        } else {
            3
        }
    }

    fn request_open_browser(&mut self) -> Result<()> {
        let Some(session_id) = self.selected_session_id.clone() else {
            self.browser_notice = Some("No current browser task yet.".to_string());
            return Ok(());
        };
        let state = self.workbench_state()?;
        let target = state
            .browser
            .live_url
            .as_deref()
            .or(state.browser.url.as_deref())
            .unwrap_or("about:blank");
        self.store.append_event(
            &session_id,
            "browser.open_requested",
            serde_json::json!({ "target": target }),
        )?;
        self.browser_notice = Some(match open_external_url(target) {
            Ok(()) => format!("Opened {target}"),
            Err(error) => format!("Could not open {target}: {error}"),
        });
        Ok(())
    }

    fn request_reconnect_browser(&mut self) -> Result<()> {
        let Some(session_id) = self.selected_session_id.clone() else {
            self.browser_notice = Some("No current browser task yet.".to_string());
            return Ok(());
        };
        self.store.append_event(
            &session_id,
            "browser.reconnect_requested",
            serde_json::json!({ "browser": self.browser }),
        )?;
        self.browser_notice = Some("Reconnect requested.".to_string());
        Ok(())
    }

    fn persist_runtime_settings(&self) -> Result<()> {
        self.store.set_setting("account", &self.account)?;
        self.store.set_setting("model", &self.model)?;
        self.store
            .set_setting("provider.model", &self.provider_model)?;
        self.store.set_setting("browser", &self.browser)?;
        self.store
            .set_setting("agent.backend", self.agent_backend.as_setting())?;
        Ok(())
    }

    fn selectable_row_count(&self) -> Result<usize> {
        Ok(match self.surface {
            Surface::Main => {
                if self.is_first_run_setup_visible()? {
                    self.setup_row_count()
                } else {
                    self.main_selection_count()?
                }
            }
            Surface::Setup => self.setup_row_count(),
            Surface::Account => ACCOUNT_CHOICES.len(),
            Surface::ApiKey => 0,
            Surface::Telemetry => 0,
            Surface::Model => MODEL_CHOICES.len(),
            Surface::Browser => 3,
            Surface::BrowserSelect => BROWSER_CHOICES.len(),
            Surface::History => self.store.list_sessions()?.len(),
            Surface::Actions => self.palette.items().len(),
            Surface::Developer => 1,
        })
    }

    fn main_selection_count(&self) -> Result<usize> {
        let state = self.workbench_state()?;
        Ok(match self.product_state(&state) {
            ProductState::Failed => 4,
            ProductState::Cancelled => 3,
            _ => 0,
        })
    }

    fn move_selection(&mut self, delta: isize) -> Result<()> {
        let count = self.selectable_row_count()?;
        if count == 0 {
            self.selected_row = 0;
            return Ok(());
        }
        let max = count.saturating_sub(1) as isize;
        self.selected_row = (self.selected_row as isize + delta).clamp(0, max) as usize;
        Ok(())
    }

    fn composer_height(&self) -> u16 {
        self.composer.height()
    }

    fn live_viewport_height(&self) -> u16 {
        self.args.height.clamp(8, 14)
    }

    fn native_scrollback_is_active(&self) -> bool {
        self.surface == Surface::Main && self.last_native_snapshot.is_some()
    }

    #[cfg(test)]
    fn set_input(&mut self, value: String) {
        self.composer.set_input(value);
    }

    #[cfg(test)]
    fn set_input_cursor(&mut self, cursor: usize) {
        self.composer.set_cursor(cursor);
    }

    fn product_state(&self, state: &WorkbenchState) -> ProductState {
        if !self.setup_complete && state.history.is_empty() && state.current_session.is_none() {
            return ProductState::SetupNeeded;
        }
        let Some(session) = state.current_session.as_ref() else {
            return ProductState::Ready;
        };
        if session.status.is_active() {
            ProductState::Running
        } else if session.status == SessionStatus::Cancelled {
            ProductState::Cancelled
        } else if state.failure.is_some() {
            ProductState::Failed
        } else {
            ProductState::Result
        }
    }

    fn should_print_and_exit(&self) -> Result<bool> {
        if self.surface != Surface::Main || self.is_first_run_setup_visible()? {
            return Ok(false);
        }
        let state = self.workbench_state()?;
        Ok(matches!(
            self.product_state(&state),
            ProductState::Result | ProductState::Failed | ProductState::Cancelled
        ))
    }

    fn account_ready(&self, account: &str) -> Result<bool> {
        Ok(match account {
            "OpenAI API key" => self.has_stored_or_env(
                "auth.openai.api_key",
                &["LLM_BROWSER_OPENAI_API_KEY", "OPENAI_API_KEY"],
            )?,
            "OpenRouter API key" => self.has_stored_or_env(
                "auth.openrouter.api_key",
                &["LLM_BROWSER_OPENAI_COMPAT_API_KEY", "OPENROUTER_API_KEY"],
            )?,
            "Anthropic API key" => self.has_stored_or_env(
                "auth.anthropic.api_key",
                &["LLM_BROWSER_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"],
            )?,
            "Claude Code login" => self.has_stored_or_env(
                "auth.claude_code.auth_token",
                &[
                    "LLM_BROWSER_CLAUDE_CODE_OAUTH_TOKEN",
                    "CLAUDE_CODE_OAUTH_TOKEN",
                    "ANTHROPIC_AUTH_TOKEN",
                ],
            )?,
            "Codex login" => true,
            _ => false,
        })
    }

    fn auth_notice(&self) -> Result<Option<String>> {
        let notice = match self.agent_backend {
            AgentBackend::Openai
                if !self.has_stored_or_env(
                    "auth.openai.api_key",
                    &["LLM_BROWSER_OPENAI_API_KEY", "OPENAI_API_KEY"],
                )? =>
            {
                Some("OpenAI API key is missing. Sign in here before retrying.")
            }
            AgentBackend::Openrouter
                if !self.has_stored_or_env(
                    "auth.openrouter.api_key",
                    &["LLM_BROWSER_OPENAI_COMPAT_API_KEY", "OPENROUTER_API_KEY"],
                )? =>
            {
                Some("OpenRouter API key is missing. Sign in here before retrying.")
            }
            AgentBackend::Anthropic
                if self.account == "Claude Code login"
                    && !self.has_stored_or_env(
                        "auth.claude_code.auth_token",
                        &[
                            "LLM_BROWSER_CLAUDE_CODE_OAUTH_TOKEN",
                            "CLAUDE_CODE_OAUTH_TOKEN",
                            "ANTHROPIC_AUTH_TOKEN",
                        ],
                    )? =>
            {
                Some("Claude Code OAuth token is missing. Paste it here before retrying.")
            }
            AgentBackend::Anthropic
                if self.account != "Claude Code login"
                    && !self.has_stored_or_env(
                        "auth.anthropic.api_key",
                        &["LLM_BROWSER_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"],
                    )? =>
            {
                Some("Anthropic API key is missing. Sign in here before retrying.")
            }
            _ => None,
        };
        Ok(notice.map(str::to_string))
    }

    fn has_stored_or_env(&self, setting_key: &str, env_names: &[&str]) -> Result<bool> {
        if self
            .store
            .get_setting(setting_key)?
            .is_some_and(|value| !value.trim().is_empty())
        {
            return Ok(true);
        }
        Ok(env_names
            .iter()
            .any(|name| std::env::var(name).is_ok_and(|value| !value.trim().is_empty())))
    }

    fn laminar_status(&self) -> Result<String> {
        if self
            .store
            .get_setting(LAMINAR_API_KEY_SETTING)?
            .is_some_and(|value| !value.trim().is_empty())
        {
            return Ok("connected via TUI config".to_string());
        }
        if std::env::var("LMNR_PROJECT_API_KEY").is_ok_and(|value| !value.trim().is_empty()) {
            return Ok("connected via LMNR_PROJECT_API_KEY".to_string());
        }
        Ok("not connected".to_string())
    }
}

const LAMINAR_API_KEY_SETTING: &str = "telemetry.laminar.api_key";

fn auth_setting_key(account: &str) -> &'static str {
    match account {
        "OpenAI API key" => "auth.openai.api_key",
        "OpenRouter API key" => "auth.openrouter.api_key",
        "Anthropic API key" => "auth.anthropic.api_key",
        "Claude Code login" => "auth.claude_code.auth_token",
        _ => "auth.codex.placeholder",
    }
}

fn auth_secret_label(account: &str) -> &'static str {
    match account {
        "OpenAI API key" => "OpenAI API key",
        "OpenRouter API key" => "OpenRouter API key",
        "Anthropic API key" => "Anthropic API key",
        "Claude Code login" => "Claude Code OAuth token",
        _ => "credential",
    }
}

#[cfg(not(test))]
fn open_external_url(target: &str) -> Result<()> {
    let target = target.trim();
    if target.is_empty() {
        anyhow::bail!("browser target is empty");
    }
    open::that_detached(target).with_context(|| format!("launch external browser for {target}"))
}

#[cfg(test)]
fn open_external_url(target: &str) -> Result<()> {
    if target.trim().is_empty() {
        anyhow::bail!("browser target is empty");
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ResetKeyboardEnhancementFlags;

impl Command for ResetKeyboardEnhancementFlags {
    fn write_ansi(&self, f: &mut impl fmt::Write) -> fmt::Result {
        f.write_str("\x1b[<u")
    }

    #[cfg(windows)]
    fn execute_winapi(&self) -> io::Result<()> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "keyboard enhancement reset is not implemented for legacy Windows terminals",
        ))
    }

    #[cfg(windows)]
    fn is_ansi_code_supported(&self) -> bool {
        false
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct DisableModifyOtherKeys;

impl Command for DisableModifyOtherKeys {
    fn write_ansi(&self, f: &mut impl fmt::Write) -> fmt::Result {
        f.write_str("\x1b[>4;0m")
    }

    #[cfg(windows)]
    fn execute_winapi(&self) -> io::Result<()> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "modifyOtherKeys reset is not implemented for legacy Windows terminals",
        ))
    }

    #[cfg(windows)]
    fn is_ansi_code_supported(&self) -> bool {
        false
    }
}

fn main() -> Result<()> {
    let args = Args::parse();
    if args.dump_screen {
        let mut app = App::new(args)?;
        let text = render_dump(&mut app)?;
        print!("{text}");
        return Ok(());
    }
    let mut app = App::new(args)?;
    if app.should_print_and_exit()? {
        print_native_transcript(&mut app)?;
        return Ok(());
    }
    run_terminal(app)
}

fn print_native_transcript(app: &mut App) -> Result<()> {
    let width = crossterm::terminal::size()
        .map(|(width, _)| width)
        .unwrap_or(app.args.width);
    let lines = native_scrollback_lines(app, width)?;
    print!("{}", lines_plain_text(&lines));
    io::stdout().flush()?;
    Ok(())
}

fn run_terminal(mut app: App) -> Result<()> {
    let live_height = app.live_viewport_height();
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(
        stdout,
        EnableBracketedPaste,
        PushKeyboardEnhancementFlags(
            KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES
                | KeyboardEnhancementFlags::REPORT_EVENT_TYPES
                | KeyboardEnhancementFlags::REPORT_ALTERNATE_KEYS
        )
    )?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::with_options(
        backend,
        TerminalOptions {
            viewport: Viewport::Inline(live_height),
        },
    )?;
    let result = (|| -> Result<()> {
        loop {
            draw_terminal_frame(&mut terminal, &mut app)?;
            if event::poll(Duration::from_millis(100))? {
                match event::read()? {
                    TermEvent::Key(key) => {
                        if app.handle_key(key)? {
                            break Ok(());
                        }
                    }
                    TermEvent::Paste(text) => {
                        app.handle_paste(&text);
                    }
                    TermEvent::Resize(_, _) => {}
                    _ => {}
                }
            }
        }
    })();
    let restore_result = restore_terminal(terminal.backend_mut());
    let cursor_result = terminal.show_cursor();
    restore_result?;
    cursor_result?;
    result?;
    Ok(())
}

fn draw_terminal_frame(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &mut App,
) -> Result<()> {
    maybe_emit_native_transcript(terminal, app)?;
    terminal.draw(|frame| render(frame, app))?;
    Ok(())
}

fn maybe_emit_native_transcript(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: &mut App,
) -> Result<()> {
    let size = terminal.size()?;
    let state = app.workbench_state()?;
    if app.surface != Surface::Main
        || app.is_first_run_setup_visible()?
        || !matches!(
            app.product_state(&state),
            ProductState::Result | ProductState::Failed | ProductState::Cancelled
        )
    {
        return Ok(());
    }
    let lines = native_scrollback_lines(app, size.width)?;
    let snapshot = lines_plain_text(&lines);
    if app.last_native_snapshot.as_deref() == Some(snapshot.as_str()) {
        return Ok(());
    }
    let height = lines.len().try_into().unwrap_or(u16::MAX).max(1);
    terminal.insert_before(height, |buf| {
        Paragraph::new(lines).render(buf.area, buf);
    })?;
    app.last_native_snapshot = Some(snapshot);
    Ok(())
}

fn restore_terminal(mut target: impl io::Write) -> Result<()> {
    disable_raw_mode()?;
    execute!(
        target,
        PopKeyboardEnhancementFlags,
        ResetKeyboardEnhancementFlags,
        DisableModifyOtherKeys,
        DisableBracketedPaste,
    )?;
    Ok(())
}

fn seed_demo_if_requested(store: &Store, mode: Option<&str>) -> Result<()> {
    let Some(mode) = mode else {
        return Ok(());
    };
    if !store.list_sessions()?.is_empty() {
        return Ok(());
    }
    let session = store.create_session(None, std::env::current_dir()?)?;
    store.append_event(
        &session.id,
        "session.input",
        serde_json::json!({"text": "Find the top 5 Hacker News posts"}),
    )?;
    store.append_event(
        &session.id,
        "browser.page",
        serde_json::json!({
            "url": "https://news.ycombinator.com",
            "title": "Hacker News",
            "tabs": 1,
            "viewport": {"w": 1440, "h": 900},
        }),
    )?;
    store.append_event(
        &session.id,
        "browser.live_url",
        serde_json::json!({"live_url": "https://live.browser-use.com/?wss=example"}),
    )?;
    if mode == "done" || mode == "followup" {
        store.append_event(
            &session.id,
            "session.done",
            serde_json::json!({"result": "Top 5 Hacker News posts\n\n1. Example story\n2. Another story\n3. Browser agents in practice"}),
        )?;
        if mode == "followup" {
            store.append_event(
                &session.id,
                "session.followup",
                serde_json::json!({"text": "Which one should I read first?"}),
            )?;
            store.append_event(
                &session.id,
                "session.done",
                serde_json::json!({"result": "Read Example story first. It has the strongest discussion and enough context to decide whether to open the others."}),
            )?;
        }
    } else if mode == "long" {
        let result = (1..=60)
            .map(|idx| format!("- scroll check line {idx}"))
            .collect::<Vec<_>>()
            .join("\n");
        store.append_event(
            &session.id,
            "session.done",
            serde_json::json!({ "result": result }),
        )?;
    } else if mode == "failed" {
        store.append_event(
            &session.id,
            "session.failed",
            serde_json::json!({"error": "OpenRouter API key is missing"}),
        )?;
    } else if mode == "cancelled" || mode == "stopped" {
        store.request_cancel(&session.id, "stopped from terminal")?;
    }
    Ok(())
}

#[cfg(test)]
mod redesign_tests {
    use super::*;

    fn args(temp: &tempfile::TempDir) -> Args {
        Args {
            state_dir: temp.path().to_path_buf(),
            model: "GPT-5.5".to_string(),
            account: "Codex login".to_string(),
            browser: "Browser Use cloud".to_string(),
            dump_screen: true,
            width: 100,
            height: 28,
            select_latest: false,
            seed_demo: None,
            overlay: None,
            agent: AgentBackend::None,
        }
    }

    fn ready_app(temp: &tempfile::TempDir) -> Result<App> {
        let mut app = App::new(args(temp))?;
        app.setup_complete = true;
        app.model_configured = true;
        app.store.set_setting("setup.complete", "1")?;
        Ok(app)
    }

    #[test]
    fn first_run_setup_is_activation_not_completion_modal() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let mut app = App::new(args(&temp))?;
        let screen = render_dump(&mut app)?;
        assert!(screen.contains("browser-use setup"));
        assert!(screen.contains("[needs] Model"));
        assert!(screen.contains("> Choose model"));
        assert!(!screen.contains("complete modal"));

        assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
        assert_eq!(app.surface, Surface::Model);
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
        assert_eq!(app.surface, Surface::BrowserSelect);
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
        assert_eq!(app.surface, Surface::Main);
        assert!(app.setup_complete);
        let screen = render_dump(&mut app)?;
        assert!(screen.contains("What should the browser do?"));
        assert!(screen.contains("Browser Use cloud"));
        Ok(())
    }

    #[test]
    fn account_flow_collects_api_key_inline() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let mut app = App::new(args(&temp))?;
        app.open_surface(Surface::Account);
        app.selected_row = 4;
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
        assert_eq!(app.surface, Surface::ApiKey);
        for ch in "sk-or-v1-test".chars() {
            assert!(!app.handle_key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE))?);
        }
        let screen = render_dump(&mut app)?;
        assert!(screen.contains("OpenRouter API key"));
        assert!(screen.contains("sk-or-v1"));
        assert!(!screen.contains("sk-or-v1-test"));
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
        assert_eq!(
            app.store.get_setting("auth.openrouter.api_key")?.as_deref(),
            Some("sk-or-v1-test")
        );
        assert_eq!(app.surface, Surface::Model);
        Ok(())
    }

    #[test]
    fn model_selection_routes_to_required_sign_in() -> Result<()> {
        let saved = std::env::var("OPENROUTER_API_KEY").ok();
        std::env::remove_var("OPENROUTER_API_KEY");
        let result = (|| -> Result<()> {
            let temp = tempfile::tempdir()?;
            let mut app = App::new(args(&temp))?;
            app.open_surface(Surface::Model);
            app.selected_row = 7;
            assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
            assert_eq!(app.model, "GLM-5.1");
            assert_eq!(app.account, "OpenRouter API key");
            assert_eq!(app.surface, Surface::ApiKey);
            Ok(())
        })();
        if let Some(value) = saved {
            std::env::set_var("OPENROUTER_API_KEY", value);
        }
        result
    }

    #[test]
    fn result_screen_is_transcript_first_and_markdown_is_clean() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let mut app = ready_app(&temp)?;
        let session = app.store.create_session(None, std::env::current_dir()?)?;
        app.store.append_event(
            &session.id,
            "session.input",
            serde_json::json!({"text": "inspect cart"}),
        )?;
        app.store.append_event(
            &session.id,
            "browser.state",
            serde_json::json!({"url": "https://example.com/cart", "title": "Cart", "tabs": 1, "viewport": {"w": 1440, "h": 900}}),
        )?;
        app.store.append_event(
            &session.id,
            "session.done",
            serde_json::json!({"result": "Your cart has **14 items**.\n\n- [Example item](https://example.com/item) with `coupon.json`\n- /tmp/cart.json"}),
        )?;
        app.selected_session_id = Some(session.id);
        let screen = render_dump(&mut app)?;
        assert!(screen.contains("> inspect cart"));
        assert!(screen.contains("+- browser"));
        assert!(screen.contains("+- result"));
        assert!(screen.contains("+- source"));
        assert!(screen.contains("Your cart has 14 items."));
        assert!(screen.contains("Example item (https://example.com/item)"));
        assert!(screen.contains("/tmp/cart.json"));
        assert!(!screen.contains("**14 items**"));
        assert!(!screen.contains("`coupon.json`"));
        assert!(!screen.contains("┌"));
        Ok(())
    }

    #[test]
    fn helper_completion_renders_as_result_not_activity_blob() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let mut app = ready_app(&temp)?;
        let session = app.store.create_session(None, std::env::current_dir()?)?;
        app.store.append_event(
            &session.id,
            "session.input",
            serde_json::json!({"text": "what is in this repo?"}),
        )?;
        app.store.append_event(
            &session.id,
            "agent.spawned",
            serde_json::json!({"child_session_id": "child", "nickname": "repo-explorer"}),
        )?;
        app.store.append_event(
            &session.id,
            "session.followup",
            serde_json::json!({"text": "whats happening"}),
        )?;
        app.store.append_event(
            &session.id,
            "agent.completed",
            serde_json::json!({
                "child_session_id": "child",
                "payload": {
                    "result": "Repository summary:\n\n- **Purpose:** Rust-first terminal workbench\n- `crates/browser-use-tui` owns the UI"
                },
            }),
        )?;
        app.selected_session_id = Some(session.id);
        let screen = render_dump(&mut app)?;
        assert!(screen.contains("> whats happening"));
        assert!(screen.contains("+- result"));
        assert!(screen.contains("Purpose: Rust-first terminal workbench"));
        assert!(screen.contains("crates/browser-use-tui"));
        assert!(screen.contains("helper finished"));
        assert!(!screen.contains("helper finished: Repository summary"));
        assert!(!screen.contains("**Purpose:**"));
        Ok(())
    }

    #[test]
    fn command_palette_filters_and_exposes_only_product_actions() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let mut app = ready_app(&temp)?;
        app.open_surface(Surface::Actions);
        let screen = render_dump(&mut app)?;
        assert!(screen.contains("Reconnect browser"));
        assert!(!screen.contains("Setup"));
        assert!(!screen.contains("Developer"));
        for ch in "model".chars() {
            assert!(!app.handle_key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE))?);
        }
        let screen = render_dump(&mut app)?;
        assert!(screen.contains("Choose model"));
        assert!(!screen.contains("Open browser"));
        Ok(())
    }

    #[test]
    fn browser_panel_actions_record_explicit_events() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let mut app = ready_app(&temp)?;
        let session = app.store.create_session(None, std::env::current_dir()?)?;
        app.store.append_event(
            &session.id,
            "session.input",
            serde_json::json!({"text": "inspect"}),
        )?;
        app.store.append_event(
            &session.id,
            "browser.live_url",
            serde_json::json!({"live_url": "https://live.browser-use.com/?wss=example"}),
        )?;
        app.selected_session_id = Some(session.id.clone());
        app.open_surface(Surface::Browser);
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
        app.selected_row = 1;
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
        let events = app.store.events_for_session(&session.id)?;
        assert!(events
            .iter()
            .any(|event| event.event_type == "browser.open_requested"));
        assert!(events
            .iter()
            .any(|event| event.event_type == "browser.reconnect_requested"));
        Ok(())
    }

    #[test]
    fn laminar_key_can_be_saved_from_developer_surface() -> Result<()> {
        let saved = std::env::var("LMNR_PROJECT_API_KEY").ok();
        std::env::remove_var("LMNR_PROJECT_API_KEY");
        let result = (|| -> Result<()> {
            let temp = tempfile::tempdir()?;
            let mut app = ready_app(&temp)?;
            app.open_surface(Surface::Developer);
            let screen = render_dump(&mut app)?;
            assert!(screen.contains("not connected"));
            assert!(screen.contains("Configure Laminar"));

            assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
            assert_eq!(app.surface, Surface::Telemetry);
            app.handle_paste("lmnr_test_key");
            let screen = render_dump(&mut app)?;
            assert!(screen.contains("Laminar API key"));
            assert!(screen.contains("lmnr_tes"));
            assert!(!screen.contains("lmnr_test_key"));

            assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
            assert_eq!(
                app.store.get_setting(LAMINAR_API_KEY_SETTING)?.as_deref(),
                Some("lmnr_test_key")
            );
            assert_eq!(app.surface, Surface::Developer);
            let screen = render_dump(&mut app)?;
            assert!(screen.contains("connected via TUI config"));
            Ok(())
        })();
        if let Some(value) = saved {
            std::env::set_var("LMNR_PROJECT_API_KEY", value);
        }
        result
    }

    #[test]
    fn composer_keeps_codex_like_multiline_behavior() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let mut app = ready_app(&temp)?;
        app.set_input("hello browser world".to_string());
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::ALT))?);
        assert_eq!(app.composer.input(), "hello browser ");
        assert_eq!(app.composer.cursor(), app.composer.input_len());
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL))?);
        assert_eq!(app.composer.input(), "");

        app.set_input("first line\nprefix suffix".to_string());
        app.set_input_cursor("first line\nprefix ".chars().count());
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::SUPER))?);
        assert_eq!(app.composer.input(), "first line");

        app.set_input("a".to_string());
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT))?);
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::NONE))?);
        assert_eq!(app.composer.input(), "a\nb");
        assert_eq!(app.composer_height(), 4);
        let rendered_input = lines_plain_text(&app.composer.render_lines(10, "placeholder"));
        assert!(rendered_input.contains("> a"));
        assert!(rendered_input.contains("  b"));
        assert!(!rendered_input.contains('|'));

        app.handle_paste(" pasted\ntext");
        assert_eq!(app.composer.input(), "a\nb pasted\ntext");
        let rendered_paste = lines_plain_text(&app.composer.render_lines(10, "placeholder"));
        assert!(rendered_paste.contains("  b pasted"));
        assert!(!rendered_paste.contains('|'));

        app.set_input("first\nsecond".to_string());
        app.set_input_cursor(app.composer.input_len());
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE))?);
        assert_eq!(app.composer.cursor(), "first".chars().count());
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE))?);
        assert_eq!(app.composer.cursor(), app.composer.input_len());
        Ok(())
    }

    #[test]
    fn long_results_use_terminal_scrollback_not_internal_scroll() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app_args = Args {
            height: 12,
            width: 80,
            ..args(&temp)
        };
        let mut app = App::new(app_args)?;
        app.setup_complete = true;
        app.model_configured = true;
        app.store.set_setting("setup.complete", "1")?;
        let session = app.store.create_session(None, std::env::current_dir()?)?;
        app.store.append_event(
            &session.id,
            "session.input",
            serde_json::json!({"text": "summarize a long page"}),
        )?;
        let result = (1..=40)
            .map(|idx| format!("- line {idx}"))
            .collect::<Vec<_>>()
            .join("\n");
        app.store.append_event(
            &session.id,
            "session.done",
            serde_json::json!({ "result": result }),
        )?;
        app.selected_session_id = Some(session.id);
        let lines = native_scrollback_lines(&mut app, 80)?;
        let text = format!("{lines:?}");
        assert!(lines.len() > app.args.height as usize);
        assert!(text.contains("line 1"));
        assert!(text.contains("line 40"));
        assert!(app.last_native_snapshot.is_none());
        Ok(())
    }

    #[test]
    fn activity_rendering_does_not_cap_or_compact_steps() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let mut app = ready_app(&temp)?;
        let session = app.store.create_session(None, std::env::current_dir()?)?;
        app.store.append_event(
            &session.id,
            "session.input",
            serde_json::json!({"text": "exercise all activity rows"}),
        )?;
        for idx in 1..=14 {
            app.store.append_event(
                &session.id,
                "tool.started",
                serde_json::json!({"name": "python", "call_id": format!("call-{idx}")}),
            )?;
        }
        app.store.append_event(
            &session.id,
            "model.delta",
            serde_json::json!({"text": "result token"}),
        )?;
        app.selected_session_id = Some(session.id);
        let lines = native_scrollback_lines(&mut app, 120)?;
        let text = lines_plain_text(&lines);
        assert!(!text.contains("earlier steps"));
        assert!(!text.contains("writing result ("));
        assert!(!text.contains("writing result"));
        assert_eq!(text.matches("using browser").count(), 14);
        Ok(())
    }

    #[test]
    fn followups_render_as_transcript_turns() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let mut app = ready_app(&temp)?;
        let session = app.store.create_session(None, std::env::current_dir()?)?;
        app.store.append_event(
            &session.id,
            "session.input",
            serde_json::json!({"text": "inspect repository"}),
        )?;
        app.store.append_event(
            &session.id,
            "session.done",
            serde_json::json!({"result": "It is a Rust TUI."}),
        )?;
        app.store.append_event(
            &session.id,
            "session.followup",
            serde_json::json!({"text": "which files matter most?"}),
        )?;
        app.store.append_event(
            &session.id,
            "session.done",
            serde_json::json!({"result": "Cargo.toml and crates/browser-use-tui/src/main.rs."}),
        )?;
        app.selected_session_id = Some(session.id);
        let screen = render_dump(&mut app)?;
        assert!(screen.contains("> inspect repository"));
        assert!(screen.contains("inspect repository"));
        assert!(screen.contains("> which files matter most?"));
        assert!(screen.contains("which files matter most?"));
        assert!(screen.contains("Cargo.toml"));
        Ok(())
    }

    #[test]
    fn followup_retry_cancel_and_developer_surface_work() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app_args = Args {
            select_latest: true,
            seed_demo: Some("done".to_string()),
            agent: AgentBackend::Fake,
            ..args(&temp)
        };
        let mut app = App::new(app_args)?;
        app.setup_complete = true;
        app.store.set_setting("setup.complete", "1")?;
        let session_id = app.selected_session_id.clone().context("seed session")?;
        app.set_input("shorter".to_string());
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))?);
        let events = app.store.events_for_session(&session_id)?;
        assert!(events
            .iter()
            .any(|event| event.event_type == "session.followup"));

        let running = app.store.create_session(None, std::env::current_dir()?)?;
        app.store.append_event(
            &running.id,
            "session.input",
            serde_json::json!({"text": "run"}),
        )?;
        app.selected_session_id = Some(running.id.clone());
        assert!(!app.handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL))?);
        assert_eq!(
            app.store
                .load_session(&running.id)?
                .map(|session| session.status),
            Some(SessionStatus::Cancelled)
        );

        app.open_surface(Surface::Developer);
        let screen = render_dump(&mut app)?;
        assert!(screen.contains("developer"));
        assert!(screen.contains("Events"));
        Ok(())
    }
}
