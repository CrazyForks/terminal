//! Rust-owned browser control plane for browser-use terminal.
//!
//! The LLM-facing split is intentional:
//! - `browser` controls connection/lifecycle/debug state.
//! - `browser_execute` runs persistent JavaScript jobs through this Rust-held
//!   CDP connection.

use std::collections::{HashMap, HashSet, VecDeque};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::Condvar;
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

use anyhow::{anyhow, bail, Context, Result};
use base64::{engine::general_purpose, Engine as _};
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tempfile::TempDir;
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{connect, Message, WebSocket};

const BU_API: &str = "https://api.browser-use.com/api/v3";
const LOG_LIMIT: usize = 250;
const BROWSER_EXECUTOR_JS: &str = include_str!("browser_executor.js");
const BROWSER_JOB_DEFAULT_OBSERVE_MS: u64 = 1_000;

#[derive(Debug)]
pub struct BrowserCommandOutput {
    pub content: Value,
    pub events: Vec<Value>,
}

#[derive(Debug, Default, Clone, Deserialize, Serialize)]
pub struct BrowserJobOutput {
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub next_observe_ms: Option<u64>,
    pub text: String,
    pub error: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub diagnosis: Option<BrowserIssueDiagnosis>,
    #[serde(default)]
    pub data: Value,
    #[serde(default)]
    pub outputs: Vec<Value>,
    #[serde(default)]
    pub summary: Vec<Value>,
    #[serde(default)]
    pub artifacts: Vec<Value>,
    #[serde(default)]
    pub images: Vec<Value>,
    #[serde(default)]
    pub browser_events: Vec<Value>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BrowserIssueDiagnosis {
    pub summary: String,
    pub what_happened: String,
    pub next_step: String,
    pub browser_usable: bool,
    pub page_usable: bool,
    pub error_kind: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum BrowserMode {
    None,
    Local,
    Managed,
    RemoteCdp,
    RemoteCloud,
}

impl BrowserMode {
    fn as_str(&self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Local => "local",
            Self::Managed => "managed",
            Self::RemoteCdp => "remote-cdp",
            Self::RemoteCloud => "remote-cloud",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum BrowserOwner {
    None,
    External,
    Rust,
}

impl BrowserOwner {
    fn as_str(&self) -> &'static str {
        match self {
            Self::None => "none",
            Self::External => "external",
            Self::Rust => "rust",
        }
    }
}

#[derive(Debug, Clone)]
struct Endpoint {
    kind: String,
    http_url: Option<String>,
    ws_url: String,
    candidate_id: Option<String>,
}

#[derive(Clone)]
struct CdpConnection {
    tx: mpsc::Sender<CdpBrokerCommand>,
    events: Arc<Mutex<VecDeque<Value>>>,
}

struct CdpBrokerCall {
    method: String,
    session_id: Option<String>,
    params: Value,
    response: mpsc::Sender<std::result::Result<Value, String>>,
}

enum CdpBrokerCommand {
    Call(CdpBrokerCall),
}

#[derive(Debug, Clone)]
struct ManagedLaunch {
    executable: String,
    profile: ManagedProfile,
    headless: bool,
    extra_args: Vec<String>,
}

#[derive(Debug, Clone)]
enum ManagedProfile {
    Temp,
    Path(PathBuf),
}

struct ManagedBrowser {
    child: Child,
    _profile_dir: Option<TempDir>,
    launch: ManagedLaunch,
}

#[derive(Debug, Clone, Serialize)]
struct LocalBrowserInstall {
    browser_name: String,
    browser_path: PathBuf,
    user_data_dir: PathBuf,
}

#[derive(Debug, Clone, Serialize)]
struct LocalBrowserProfile {
    id: String,
    browser_name: String,
    browser_path: PathBuf,
    user_data_dir: PathBuf,
    profile_dir: String,
    profile_name: String,
    profile_path: PathBuf,
    display_name: String,
}

struct BrowserSession {
    session_id: Option<String>,
    mode: BrowserMode,
    owner: BrowserOwner,
    endpoint: Option<Endpoint>,
    connection: Option<CdpConnection>,
    current_target_id: Option<String>,
    current_session_id: Option<String>,
    connection_generation: u64,
    managed: Option<ManagedBrowser>,
    remote_browser_id: Option<String>,
    live_url: Option<String>,
    browser_name: Option<String>,
    profile: Option<String>,
    last_error: Option<String>,
    last_error_kind: Option<String>,
    last_target_id: Option<String>,
    last_session_id: Option<String>,
    last_emitted_browser_payload: Option<Value>,
    logs: VecDeque<String>,
}

impl Default for BrowserSession {
    fn default() -> Self {
        Self {
            session_id: None,
            mode: BrowserMode::None,
            owner: BrowserOwner::None,
            endpoint: None,
            connection: None,
            current_target_id: None,
            current_session_id: None,
            connection_generation: 0,
            managed: None,
            remote_browser_id: None,
            live_url: None,
            browser_name: None,
            profile: None,
            last_error: None,
            last_error_kind: None,
            last_target_id: None,
            last_session_id: None,
            last_emitted_browser_payload: None,
            logs: VecDeque::new(),
        }
    }
}

static SESSIONS: OnceLock<Mutex<HashMap<String, BrowserSession>>> = OnceLock::new();
static BROWSER_JS_EXECUTORS: OnceLock<Mutex<HashMap<String, BrowserJsExecutor>>> = OnceLock::new();
static BROWSER_JOB_RUN_COUNTER: AtomicU64 = AtomicU64::new(1);

struct BrowserJsExecutor {
    child: Arc<Mutex<Child>>,
    stdin: Arc<Mutex<ChildStdin>>,
    state: Arc<BrowserExecutorSharedState>,
    reader: Option<thread::JoinHandle<()>>,
}

struct BrowserExecutorSharedState {
    inner: Mutex<BrowserExecutorState>,
    changed: Condvar,
}

#[derive(Default)]
struct BrowserExecutorState {
    jobs: HashMap<String, BrowserJobState>,
    failed: Option<String>,
}

#[derive(Default, Clone)]
struct BrowserJobState {
    id: String,
    session_id: String,
    artifact_dir: PathBuf,
    ok: bool,
    status: String,
    text: String,
    error: Option<String>,
    diagnosis: Option<BrowserIssueDiagnosis>,
    data: Value,
    outputs: Vec<Value>,
    summary: Vec<Value>,
    artifacts: Vec<Value>,
    images: Vec<Value>,
    browser_events: Vec<Value>,
    seq: u64,
    started_at_ms: u128,
    timeout_ms: u64,
}

fn browser_js_executors() -> &'static Mutex<HashMap<String, BrowserJsExecutor>> {
    BROWSER_JS_EXECUTORS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn active_browser_jobs_json(session_id: &str) -> Value {
    let executors = browser_js_executors()
        .lock()
        .expect("browser JS executor registry poisoned");
    let Some(executor) = executors.get(session_id) else {
        return Value::Array(Vec::new());
    };
    let Ok(state) = executor.state.inner.lock() else {
        return Value::Array(Vec::new());
    };
    Value::Array(
        state
            .jobs
            .values()
            .filter(|job| job.session_id == session_id && job.status == "running")
            .map(|job| {
                json!({
                    "run_id": job.id,
                    "status": job.status,
                    "started_at_ms": job.started_at_ms as u64,
                    "timeout_ms": job.timeout_ms,
                    "next_step": format!("browser_observe run_id={}", job.id),
                })
            })
            .collect(),
    )
}

pub fn execute_browser_js(
    session_id: &str,
    cwd: impl AsRef<Path>,
    artifact_dir: impl AsRef<Path>,
    code: &str,
    yield_time_ms: u64,
    timeout_ms: u64,
) -> Result<BrowserJobOutput> {
    if code.trim().is_empty() {
        bail!("browser_execute requires non-empty code");
    }
    fs::create_dir_all(artifact_dir.as_ref())
        .with_context(|| format!("create artifact dir {}", artifact_dir.as_ref().display()))?;
    ensure_browser_session_has_connection(session_id)?;
    let run_id = new_browser_job_run_id();
    let timeout_ms = timeout_ms.max(1);
    let yield_time_ms = yield_time_ms.clamp(1, 30_000);
    let executor = ensure_browser_js_executor(session_id, cwd.as_ref(), artifact_dir.as_ref())?;
    {
        let mut state = executor
            .state
            .inner
            .lock()
            .expect("browser JS executor state poisoned");
        state.jobs.insert(
            run_id.clone(),
            BrowserJobState {
                id: run_id.clone(),
                session_id: session_id.to_string(),
                artifact_dir: artifact_dir.as_ref().to_path_buf(),
                ok: true,
                status: "running".to_string(),
                started_at_ms: unix_time_ms(),
                timeout_ms,
                ..Default::default()
            },
        );
        executor.state.changed.notify_all();
    }
    send_browser_executor_message(
        &executor,
        json!({
            "type": "execute",
            "run_id": run_id,
            "code": code,
            "timeout_ms": timeout_ms,
        }),
    )?;
    wait_for_browser_job(
        &executor.state,
        &run_id,
        0,
        Duration::from_millis(yield_time_ms),
    )
}

pub fn observe_browser_job(
    session_id: &str,
    run_id: &str,
    yield_time_ms: u64,
) -> Result<BrowserJobOutput> {
    let executors = browser_js_executors()
        .lock()
        .expect("browser JS executor registry poisoned");
    let executor = executors
        .get(session_id)
        .ok_or_else(|| anyhow!("unknown browser job {run_id:?}"))?;
    let seq = {
        let state = executor
            .state
            .inner
            .lock()
            .expect("browser JS executor state poisoned");
        let job = state
            .jobs
            .get(run_id)
            .ok_or_else(|| anyhow!("unknown browser job {run_id:?}"))?;
        if job.session_id != session_id {
            bail!("browser job {run_id} belongs to a different session");
        }
        job.seq
    };
    wait_for_browser_job(
        &executor.state,
        run_id,
        seq,
        Duration::from_millis(yield_time_ms.clamp(1, 30_000)),
    )
}

pub fn cancel_browser_job(
    session_id: &str,
    run_id: &str,
    reason: &str,
) -> Result<BrowserJobOutput> {
    let executors = browser_js_executors()
        .lock()
        .expect("browser JS executor registry poisoned");
    let executor = executors
        .get(session_id)
        .ok_or_else(|| anyhow!("unknown browser job {run_id:?}"))?;
    send_browser_executor_message(
        executor,
        json!({
            "type": "cancel",
            "run_id": run_id,
            "reason": reason,
        }),
    )?;
    wait_for_browser_job(
        &executor.state,
        run_id,
        0,
        Duration::from_millis(BROWSER_JOB_DEFAULT_OBSERVE_MS),
    )
}

fn ensure_browser_js_executor(
    session_id: &str,
    cwd: &Path,
    artifact_dir: &Path,
) -> Result<BrowserJsExecutor> {
    let mut executors = browser_js_executors()
        .lock()
        .expect("browser JS executor registry poisoned");
    if let Some(executor) = executors.get(session_id) {
        return Ok(executor.clone_for_use());
    }
    if !command_exists("bun") {
        bail!("Bun is required for browser_execute but was not found on PATH");
    }
    fs::create_dir_all(artifact_dir)
        .with_context(|| format!("create artifact dir {}", artifact_dir.display()))?;
    let script_path = artifact_dir.join(".browser-executor.js");
    fs::write(&script_path, BROWSER_EXECUTOR_JS)
        .with_context(|| format!("write {}", script_path.display()))?;
    let mut child = Command::new("bun")
        .arg(&script_path)
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("spawn Bun browser executor")?;
    let stdin = Arc::new(Mutex::new(
        child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("Bun browser executor missing stdin"))?,
    ));
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow!("Bun browser executor missing stdout"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| anyhow!("Bun browser executor missing stderr"))?;
    let state = Arc::new(BrowserExecutorSharedState {
        inner: Mutex::new(BrowserExecutorState::default()),
        changed: Condvar::new(),
    });
    let reader_state = Arc::clone(&state);
    let reader_stdin = Arc::clone(&stdin);
    let reader_session_id = session_id.to_string();
    let reader = thread::Builder::new()
        .name(format!("browser-js-executor-{session_id}"))
        .spawn(move || {
            read_browser_executor_stdout(
                stdout,
                stderr,
                reader_session_id,
                reader_stdin,
                reader_state,
            )
        })
        .context("spawn browser executor reader")?;
    let executor = BrowserJsExecutor {
        child: Arc::new(Mutex::new(child)),
        stdin,
        state,
        reader: Some(reader),
    };
    let clone = executor.clone_for_use();
    executors.insert(session_id.to_string(), executor);
    Ok(clone)
}

impl BrowserJsExecutor {
    fn clone_for_use(&self) -> Self {
        Self {
            child: Arc::clone(&self.child),
            stdin: Arc::clone(&self.stdin),
            state: Arc::clone(&self.state),
            reader: None,
        }
    }
}

fn send_browser_executor_message(executor: &BrowserJsExecutor, value: Value) -> Result<()> {
    let mut stdin = executor
        .stdin
        .lock()
        .expect("browser JS executor stdin poisoned");
    serde_json::to_writer(&mut *stdin, &value)?;
    stdin.write_all(b"\n")?;
    stdin.flush()?;
    Ok(())
}

fn read_browser_executor_stdout(
    stdout: ChildStdout,
    stderr: ChildStderr,
    session_id: String,
    stdin: Arc<Mutex<ChildStdin>>,
    state: Arc<BrowserExecutorSharedState>,
) {
    let stderr_state = Arc::clone(&state);
    thread::spawn(move || {
        let mut stderr = BufReader::new(stderr);
        let mut line = String::new();
        loop {
            line.clear();
            match stderr.read_line(&mut line) {
                Ok(0) => break,
                Ok(_) => mark_browser_executor_failed(
                    &stderr_state,
                    format!("browser executor stderr: {}", line.trim_end()),
                ),
                Err(error) => {
                    mark_browser_executor_failed(
                        &stderr_state,
                        format!("read browser executor stderr: {error}"),
                    );
                    break;
                }
            }
        }
    });

    let mut stdout = BufReader::new(stdout);
    let mut line = String::new();
    loop {
        line.clear();
        match stdout.read_line(&mut line) {
            Ok(0) => {
                mark_browser_executor_failed(&state, "browser executor exited".to_string());
                break;
            }
            Ok(_) => {
                let trimmed = line.trim_end();
                if trimmed.is_empty() {
                    continue;
                }
                match serde_json::from_str::<Value>(trimmed) {
                    Ok(message) => {
                        handle_browser_executor_message(&session_id, &stdin, &state, message)
                    }
                    Err(error) => mark_browser_executor_failed(
                        &state,
                        format!("parse browser executor message: {error}: {trimmed}"),
                    ),
                }
            }
            Err(error) => {
                mark_browser_executor_failed(
                    &state,
                    format!("read browser executor stdout: {error}"),
                );
                break;
            }
        }
    }
}

fn handle_browser_executor_message(
    session_id: &str,
    stdin: &Arc<Mutex<ChildStdin>>,
    state: &Arc<BrowserExecutorSharedState>,
    message: Value,
) {
    match message.get("type").and_then(Value::as_str).unwrap_or("") {
        "cdp.call" => handle_browser_executor_cdp_call(session_id, stdin, message),
        "run.log" => update_browser_job(state, &message, |job, message| {
            if let Some(text) = message.get("text").and_then(Value::as_str) {
                if !job.text.is_empty() && !job.text.ends_with('\n') {
                    job.text.push('\n');
                }
                job.text.push_str(text);
            }
        }),
        "run.checkpoint" => update_browser_job(state, &message, |job, message| {
            job.summary.push(json!({
                "kind": "checkpoint",
                "label": message.get("label").cloned().unwrap_or(Value::Null),
                "data": message.get("data").cloned().unwrap_or(Value::Null),
            }));
        }),
        "run.output" => update_browser_job(state, &message, |job, message| {
            if let Some(output) = message.get("output").cloned() {
                job.outputs.push(output);
            }
        }),
        "run.image" => update_browser_job(state, &message, |job, message| {
            if let Some(image) = browser_job_image_artifact(job, message) {
                job.images.push(image.clone());
                job.artifacts.push(image);
            }
        }),
        "run.artifact" => update_browser_job(state, &message, |job, message| {
            if let Some(artifact) = message.get("artifact").cloned() {
                job.artifacts.push(artifact);
            }
        }),
        "run.result" => update_browser_job(state, &message, |job, message| {
            job.ok = true;
            job.status = "finished".to_string();
            job.data = message.get("result").cloned().unwrap_or(Value::Null);
        }),
        "run.error" => update_browser_job(state, &message, |job, message| {
            let error = message
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("browser_execute failed")
                .to_string();
            job.ok = false;
            job.status = "failed".to_string();
            job.error = Some(error.clone());
            job.diagnosis = Some(browser_job_failure_diagnosis(&job.session_id, &error));
        }),
        "run.cancelled" => update_browser_job(state, &message, |job, _| {
            job.ok = true;
            job.status = "cancelled".to_string();
            if job.text.trim().is_empty() {
                job.text = "browser job cancelled.".to_string();
            }
        }),
        "browser.set_current_target" => {
            let target_id = message.get("target_id").and_then(Value::as_str);
            let session = message.get("session_id").and_then(Value::as_str);
            if let (Some(target_id), Some(cdp_session_id)) = (target_id, session) {
                let _ = set_current_browser_target(session_id, target_id, cdp_session_id);
            }
        }
        _ => {}
    }
}

fn update_browser_job<F>(state: &Arc<BrowserExecutorSharedState>, message: &Value, f: F)
where
    F: FnOnce(&mut BrowserJobState, &Value),
{
    let Some(run_id) = message.get("run_id").and_then(Value::as_str) else {
        return;
    };
    let mut guard = state
        .inner
        .lock()
        .expect("browser JS executor state poisoned");
    if let Some(job) = guard.jobs.get_mut(run_id) {
        f(job, message);
        job.seq = job.seq.saturating_add(1);
        state.changed.notify_all();
    }
}

fn mark_browser_executor_failed(state: &Arc<BrowserExecutorSharedState>, error: String) {
    let mut guard = state
        .inner
        .lock()
        .expect("browser JS executor state poisoned");
    guard.failed = Some(error.clone());
    for job in guard
        .jobs
        .values_mut()
        .filter(|job| job.status == "running")
    {
        job.ok = false;
        job.status = "failed".to_string();
        job.error = Some(error.clone());
        job.diagnosis = Some(browser_job_failure_diagnosis(&job.session_id, &error));
        job.seq = job.seq.saturating_add(1);
    }
    state.changed.notify_all();
}

fn handle_browser_executor_cdp_call(
    session_id: &str,
    stdin: &Arc<Mutex<ChildStdin>>,
    message: Value,
) {
    let call_id = message
        .get("call_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let method = message
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let params = message.get("params").cloned().unwrap_or_else(|| json!({}));
    let requested_session_id = message
        .get("session_id")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let timeout_ms = message
        .get("timeout_ms")
        .and_then(Value::as_u64)
        .unwrap_or(20_000)
        .clamp(1, 120_000);
    let stdin = Arc::clone(stdin);
    let session_id = session_id.to_string();
    thread::spawn(move || {
        let response = match browser_executor_cdp_call(
            &session_id,
            &method,
            requested_session_id.as_deref(),
            params,
            Duration::from_millis(timeout_ms),
        ) {
            Ok(result) => json!({
                "type": "cdp.result",
                "call_id": call_id,
                "result": result,
            }),
            Err(error) => json!({
                "type": "cdp.error",
                "call_id": call_id,
                "error": format!("{error:#}"),
            }),
        };
        if let Ok(mut stdin) = stdin.lock() {
            let _ = serde_json::to_writer(&mut *stdin, &response);
            let _ = stdin.write_all(b"\n");
            let _ = stdin.flush();
        }
    });
}

fn browser_executor_cdp_call(
    session_id: &str,
    method: &str,
    requested_session_id: Option<&str>,
    params: Value,
    timeout: Duration,
) -> Result<Value> {
    if method == "BrowserUse.drainEvents" {
        let connection = {
            let sessions = sessions()
                .lock()
                .expect("browser session registry poisoned");
            let session = sessions.get(session_id).ok_or_else(|| {
                anyhow!("browser is not connected or is busy; run browser_status")
            })?;
            session
                .connection
                .clone()
                .ok_or_else(|| anyhow!("browser is not connected; run browser_status"))?
        };
        return Ok(json!({ "events": connection.drain_events() }));
    }
    let (connection, routed_session_id) = {
        let sessions = sessions()
            .lock()
            .expect("browser session registry poisoned");
        let session = sessions
            .get(session_id)
            .ok_or_else(|| anyhow!("browser is not connected or is busy; run browser_status"))?;
        let connection = session
            .connection
            .clone()
            .ok_or_else(|| anyhow!("browser is not connected; run browser_status"))?;
        let routed_session_id = requested_session_id.map(ToOwned::to_owned).or_else(|| {
            (!is_browser_level_cdp_method(method))
                .then(|| session.current_session_id.clone())
                .flatten()
        });
        (connection, routed_session_id)
    };
    connection.call_with_timeout(method, routed_session_id.as_deref(), params, timeout)
}

fn is_browser_level_cdp_method(method: &str) -> bool {
    method.starts_with("Target.")
        || method.starts_with("Browser.")
        || method.starts_with("Storage.")
}

fn browser_job_image_artifact(job: &BrowserJobState, message: &Value) -> Option<Value> {
    let data = message.get("data").and_then(Value::as_str)?;
    let mime_type = message
        .get("mime_type")
        .and_then(Value::as_str)
        .unwrap_or("image/png");
    let extension = match mime_type {
        "image/jpeg" => "jpg",
        "image/webp" => "webp",
        _ => "png",
    };
    let label = message
        .get("label")
        .and_then(Value::as_str)
        .unwrap_or("screenshot")
        .chars()
        .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '-' })
        .collect::<String>();
    let path = job.artifact_dir.join(format!(
        "{}-{}.{}",
        job.id,
        label.trim_matches('-'),
        extension
    ));
    let bytes = general_purpose::STANDARD.decode(data).ok()?;
    if fs::write(&path, bytes).is_err() {
        return None;
    }
    Some(json!({
        "path": path.display().to_string(),
        "label": label,
        "mime_type": mime_type,
        "detail": message.get("detail").and_then(Value::as_str).unwrap_or("auto"),
    }))
}

fn wait_for_browser_job(
    state: &Arc<BrowserExecutorSharedState>,
    run_id: &str,
    start_seq: u64,
    wait: Duration,
) -> Result<BrowserJobOutput> {
    let deadline = Instant::now() + wait;
    let mut guard = state
        .inner
        .lock()
        .expect("browser JS executor state poisoned");
    loop {
        if let Some(job) = guard.jobs.get(run_id) {
            if job.status != "running" || job.seq != start_seq {
                return Ok(browser_job_output(job));
            }
        } else {
            bail!("unknown browser job {run_id:?}");
        }
        let now = Instant::now();
        if now >= deadline {
            let job = guard
                .jobs
                .get(run_id)
                .ok_or_else(|| anyhow!("unknown browser job {run_id:?}"))?;
            return Ok(browser_job_output(job));
        }
        let timeout = deadline.saturating_duration_since(now);
        let (next_guard, _) = state
            .changed
            .wait_timeout(guard, timeout)
            .expect("browser JS executor state poisoned");
        guard = next_guard;
    }
}

fn browser_job_output(job: &BrowserJobState) -> BrowserJobOutput {
    let mut output = BrowserJobOutput {
        ok: job.ok,
        status: Some(job.status.clone()),
        run_id: Some(job.id.clone()),
        next_observe_ms: (job.status == "running").then_some(BROWSER_JOB_DEFAULT_OBSERVE_MS),
        text: job.text.clone(),
        error: job.error.clone(),
        diagnosis: job.diagnosis.clone(),
        data: job.data.clone(),
        outputs: job.outputs.clone(),
        summary: job.summary.clone(),
        artifacts: job.artifacts.clone(),
        images: job.images.clone(),
        browser_events: job.browser_events.clone(),
    };
    if output.status.as_deref() == Some("running") && output.text.trim().is_empty() {
        output.text = format!(
            "browser_execute is still running.\nrun_id: {}\nNext: call browser_observe with run_id=\"{}\".",
            job.id, job.id
        );
    }
    output
}

fn ensure_browser_session_has_connection(session_id: &str) -> Result<()> {
    let sessions = sessions()
        .lock()
        .expect("browser session registry poisoned");
    let session = sessions.get(session_id).ok_or_else(|| {
        anyhow!("browser is not connected. Run browser_status or browser_configure.")
    })?;
    if session.connection.is_none() {
        bail!("browser is not connected. Run browser_status or browser_configure.");
    }
    Ok(())
}

fn set_current_browser_target(
    session_id: &str,
    target_id: &str,
    cdp_session_id: &str,
) -> Result<()> {
    let mut sessions = sessions()
        .lock()
        .expect("browser session registry poisoned");
    let session = sessions
        .get_mut(session_id)
        .ok_or_else(|| anyhow!("browser is not connected. Run browser_status."))?;
    session.current_target_id = Some(target_id.to_string());
    session.current_session_id = Some(cdp_session_id.to_string());
    session.connection_generation += 1;
    Ok(())
}

fn browser_job_failure_diagnosis(session_id: &str, error: &str) -> BrowserIssueDiagnosis {
    let state = browser_issue_state_for_session(session_id);
    browser_issue_diagnosis(
        classify_browser_job_failure(error),
        state.browser_connected,
        state.page_usable,
        state.next_step.as_deref(),
    )
}

fn new_browser_job_run_id() -> String {
    let n = BROWSER_JOB_RUN_COUNTER.fetch_add(1, Ordering::SeqCst);
    format!("br-{}-{n}", unix_time_ms())
}

pub fn run_browser_command(
    session_id: &str,
    cwd: impl AsRef<Path>,
    artifact_dir: impl AsRef<Path>,
    raw_cmd: &str,
) -> Result<BrowserCommandOutput> {
    let mut argv = shell_words(raw_cmd)?;
    if argv.first().is_some_and(|arg| arg == "browser") {
        argv.remove(0);
    }
    if argv.is_empty() {
        argv.push("help".to_string());
    }

    let mut sessions = sessions()
        .lock()
        .expect("browser session registry poisoned");
    let session = sessions.entry(session_id.to_string()).or_default();
    session.session_id = Some(session_id.to_string());
    session.log(format!("browser {}", argv.join(" ")));
    let content = dispatch_browser_command(session, cwd.as_ref(), artifact_dir.as_ref(), &argv)?;
    Ok(BrowserCommandOutput {
        events: session.browser_events(),
        content,
    })
}

#[derive(Debug, Default)]
struct BrowserIssueState {
    browser_connected: bool,
    page_usable: bool,
    next_step: Option<String>,
}

fn browser_issue_state_for_session(session_id: &str) -> BrowserIssueState {
    let Ok(sessions) = sessions().lock() else {
        return BrowserIssueState::default();
    };
    let Some(session) = sessions.get(session_id) else {
        return BrowserIssueState::default();
    };
    let browser_connected = session.connection.is_some();
    BrowserIssueState {
        browser_connected,
        page_usable: browser_connected
            && session.current_target_id.is_some()
            && session.current_session_id.is_some(),
        next_step: session.next_step().map(ToOwned::to_owned),
    }
}

fn command_exists(name: &str) -> bool {
    std::env::var_os("PATH").is_some_and(|paths| {
        std::env::split_paths(&paths).any(|dir| {
            let candidate = dir.join(name);
            if candidate.is_file() {
                return true;
            }
            #[cfg(windows)]
            {
                dir.join(format!("{name}.exe")).is_file()
            }
            #[cfg(not(windows))]
            {
                false
            }
        })
    })
}

fn domain_skill_roots_for(agent_workspace_dir: &Path) -> Vec<PathBuf> {
    let mut roots = Vec::new();
    for var in ["BH_DOMAIN_SKILLS_ROOT", "BH_DOMAIN_SKILLS_DIR"] {
        if let Some(value) = std::env::var_os(var).filter(|value| !value.is_empty()) {
            for path in std::env::split_paths(&value) {
                push_unique_existing_dir(&mut roots, path);
            }
        }
    }
    push_unique_existing_dir(&mut roots, agent_workspace_dir.join("domain-skills"));
    if let Some(home) = home_dir() {
        push_unique_existing_dir(
            &mut roots,
            home.join(".browser-use-terminal")
                .join("agent-workspace")
                .join("domain-skills"),
        );
        push_unique_existing_dir(
            &mut roots,
            home.join("repos")
                .join("browser-harness")
                .join("agent-workspace")
                .join("domain-skills"),
        );
        push_unique_existing_dir(
            &mut roots,
            home.join("repos")
                .join("browser-harness")
                .join("domain-skills"),
        );
    }
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    if let Some(repo_root) = manifest_dir.parent().and_then(Path::parent) {
        push_unique_existing_dir(&mut roots, repo_root.join("domain-skills"));
    }
    roots
}

fn push_unique_existing_dir(roots: &mut Vec<PathBuf>, path: PathBuf) {
    if !path.is_dir() {
        return;
    }
    let key = fs::canonicalize(&path).unwrap_or(path);
    if !roots.iter().any(|existing| existing == &key) {
        roots.push(key);
    }
}

fn domain_skills_enabled() -> bool {
    match std::env::var("BH_DOMAIN_SKILLS") {
        Ok(value) => {
            let normalized = value.trim().to_ascii_lowercase();
            !matches!(normalized.as_str(), "" | "0" | "false" | "no" | "off")
        }
        Err(_) => true,
    }
}

fn normalize_domain_like_browser(value: &str) -> String {
    let mut host = value.trim();
    if let Some(rest) = host.strip_prefix("https://") {
        host = rest;
    } else if let Some(rest) = host.strip_prefix("http://") {
        host = rest;
    }
    host = host
        .split(['/', '?', '#'])
        .next()
        .unwrap_or(host)
        .split('@')
        .next_back()
        .unwrap_or(host)
        .split(':')
        .next()
        .unwrap_or(host);
    host.trim_start_matches("www.").to_ascii_lowercase()
}

fn domain_skill_aliases(value: &str) -> HashSet<String> {
    let host = normalize_domain_like_browser(value);
    let labels = host
        .split('.')
        .filter(|label| !label.is_empty())
        .collect::<Vec<_>>();
    let mut aliases = HashSet::from([host.clone(), host.replace('.', "-")]);
    if let Some(first) = labels.first() {
        aliases.insert((*first).to_string());
    }
    if labels.len() >= 2 {
        aliases.insert(labels[labels.len() - 2].to_string());
        aliases.insert(format!(
            "{}-{}",
            labels[labels.len() - 2],
            labels[labels.len() - 1]
        ));
    }
    if labels.len() >= 3 {
        aliases.insert(format!("{}-{}", labels[labels.len() - 2], labels[0]));
        aliases.insert(format!("{}-{}", labels[0], labels[labels.len() - 2]));
    }
    aliases
        .into_iter()
        .map(|alias| alias.replace('_', "-").to_ascii_lowercase())
        .collect()
}

fn domain_skill_matches(
    domain: &str,
    roots: &[PathBuf],
    include_content: bool,
    max_files: usize,
    max_bytes: usize,
) -> Result<Vec<Value>> {
    if !domain_skills_enabled() {
        return Ok(Vec::new());
    }
    let aliases = domain_skill_aliases(domain);
    let mut matches = Vec::new();
    let mut remaining = max_bytes;
    for root in roots {
        let Ok(entries) = fs::read_dir(root) else {
            continue;
        };
        let mut entries = entries.filter_map(|entry| entry.ok()).collect::<Vec<_>>();
        entries.sort_by_key(|entry| entry.file_name());
        for entry in entries {
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            if !file_type.is_dir() {
                continue;
            }
            let site = entry.file_name().to_string_lossy().to_string();
            let site_key = site.replace('_', "-").to_ascii_lowercase();
            if !aliases.contains(&site_key) {
                continue;
            }
            let mut files = collect_domain_skill_files(
                &entry.path(),
                include_content,
                max_files,
                &mut remaining,
            )?;
            if !files.is_empty() {
                files.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
                matches.push(json!({
                    "site": site,
                    "root": root.display().to_string(),
                    "files": files,
                }));
            }
        }
    }
    Ok(matches)
}

fn collect_domain_skill_files(
    site_dir: &Path,
    include_content: bool,
    max_files: usize,
    remaining: &mut usize,
) -> Result<Vec<Value>> {
    let mut stack = vec![site_dir.to_path_buf()];
    let mut files = Vec::new();
    while let Some(dir) = stack.pop() {
        let Ok(entries) = fs::read_dir(&dir) else {
            continue;
        };
        let mut entries = entries.filter_map(|entry| entry.ok()).collect::<Vec<_>>();
        entries.sort_by_key(|entry| entry.file_name());
        for entry in entries {
            let path = entry.path();
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            if file_type.is_dir() {
                stack.push(path);
                continue;
            }
            if !file_type.is_file() {
                continue;
            }
            let extension = path
                .extension()
                .and_then(|extension| extension.to_str())
                .unwrap_or("")
                .to_ascii_lowercase();
            if !matches!(extension.as_str(), "md" | "py") {
                continue;
            }
            let name = path
                .strip_prefix(site_dir)
                .unwrap_or(&path)
                .display()
                .to_string();
            let mut item = json!({
                "name": name,
                "path": path.display().to_string(),
            });
            if include_content && *remaining > 0 {
                let content = fs::read_to_string(&path)
                    .unwrap_or_else(|error| format!("[failed to read domain skill: {error}]"));
                let take = content
                    .char_indices()
                    .map(|(idx, _)| idx)
                    .chain(std::iter::once(content.len()))
                    .take_while(|idx| *idx <= *remaining)
                    .last()
                    .unwrap_or(0);
                item["content"] = Value::String(content[..take].to_string());
                item["truncated"] = Value::Bool(take < content.len());
                *remaining = remaining.saturating_sub(take);
            }
            files.push(item);
            if files.len() >= max_files {
                return Ok(files);
            }
        }
    }
    Ok(files)
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("USERPROFILE")
                .filter(|value| !value.is_empty())
                .map(PathBuf::from)
        })
}

pub fn cleanup_session(session_id: &str) -> usize {
    stop_browser_js_executor(session_id);
    let mut sessions = sessions()
        .lock()
        .expect("browser session registry poisoned");
    if let Some(mut session) = sessions.remove(session_id) {
        session.stop_owned_managed();
        if session.owner == BrowserOwner::Rust && session.mode == BrowserMode::RemoteCloud {
            let _ = session.stop_owned_remote();
        }
        1
    } else {
        0
    }
}

fn stop_browser_js_executor(session_id: &str) {
    let executor = browser_js_executors()
        .lock()
        .expect("browser JS executor registry poisoned")
        .remove(session_id);
    let Some(mut executor) = executor else {
        return;
    };
    if let Ok(mut child) = executor.child.lock() {
        let _ = child.kill();
        let _ = child.wait();
    }
    if let Some(reader) = executor.reader.take() {
        let _ = reader.join();
    }
}

fn sessions() -> &'static Mutex<HashMap<String, BrowserSession>> {
    SESSIONS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn dispatch_browser_command(
    session: &mut BrowserSession,
    cwd: &Path,
    artifact_dir: &Path,
    argv: &[String],
) -> Result<Value> {
    match argv.first().map(String::as_str).unwrap_or("help") {
        "help" | "--help" | "-h" => Ok(Value::String(browser_help().to_string())),
        "status" => Ok(session.status_json()),
        "doctor" => {
            let doctor = session.doctor(cwd)?;
            if has_flag(argv, "--json") {
                Ok(doctor)
            } else {
                Ok(Value::String(render_doctor(&doctor)))
            }
        }
        "connect" => dispatch_connect(session, argv),
        "local" => dispatch_local(session, argv, artifact_dir),
        "remote" => dispatch_remote(session, argv),
        "domain" => dispatch_domain(argv),
        "recover" => dispatch_recover(session, argv),
        "runtime" => dispatch_runtime(session, argv),
        other => bail!("unknown browser command: {other}. Run `browser help`."),
    }
}

fn dispatch_domain(argv: &[String]) -> Result<Value> {
    match argv.get(1).map(String::as_str) {
        Some("skills") => {
            let domain = option_value(argv, "--domain")
                .or_else(|| argv.get(2).cloned())
                .ok_or_else(|| anyhow!("browser domain skills requires --domain <domain>"))?;
            let include_content = has_flag(argv, "--include-content");
            let roots = domain_skill_roots_for(
                &std::env::var_os("BH_AGENT_WORKSPACE")
                    .filter(|value| !value.is_empty())
                    .map(PathBuf::from)
                    .unwrap_or_else(|| {
                        home_dir()
                            .map(|home| home.join(".browser-use-terminal").join("agent-workspace"))
                            .unwrap_or_else(|| {
                                PathBuf::from(".browser-use-terminal").join("agent-workspace")
                            })
                    }),
            );
            Ok(json!({
                "status": "ok",
                "domain": normalize_domain_like_browser(&domain),
                "enabled": domain_skills_enabled(),
                "roots": roots.iter().map(|path| path.display().to_string()).collect::<Vec<_>>(),
                "matches": domain_skill_matches(&domain, &roots, include_content, 10, 120_000)?,
                "next_step": "If matches are present and the task is site-specific, read them before inventing selectors, private API routes, or flows.",
            }))
        }
        Some(other) => bail!("unknown browser domain command: {other}"),
        None => bail!("browser domain requires skills"),
    }
}

fn dispatch_connect(session: &mut BrowserSession, argv: &[String]) -> Result<Value> {
    match argv.get(1).map(String::as_str) {
        Some("local") => {
            let candidate_id = option_value(argv, "--candidate");
            session.connect_local(candidate_id)
        }
        Some("managed") => {
            let headless = if has_flag(argv, "--headed") {
                false
            } else {
                !has_flag(argv, "--headful")
            };
            let profile = match option_value(argv, "--profile").as_deref() {
                None | Some("temp") => ManagedProfile::Temp,
                Some(path) => ManagedProfile::Path(PathBuf::from(path)),
            };
            let extra_args = option_values(argv, "--arg");
            session.connect_managed(headless, profile, extra_args)
        }
        Some("remote-cdp") => {
            if let Some(url) = option_value(argv, "--url") {
                session.connect_remote_http(url)
            } else if let Some(ws) = option_value(argv, "--ws") {
                session.connect_remote_ws(ws)
            } else {
                bail!("connect remote-cdp requires --url <http-url> or --ws <ws-url>");
            }
        }
        Some(other) => bail!("unknown browser connect mode: {other}"),
        None => bail!("browser connect requires local, managed, or remote-cdp"),
    }
}

fn dispatch_local(
    _session: &mut BrowserSession,
    argv: &[String],
    _artifact_dir: &Path,
) -> Result<Value> {
    match argv.get(1).map(String::as_str) {
        Some("list") => Ok(json!({ "candidates": local_candidates() })),
        Some("setup") => {
            let url = "chrome://inspect/#remote-debugging";
            let profile_ref = option_value(argv, "--profile");
            let (opened, profile, open_error) = if let Some(profile_ref) = profile_ref {
                let profiles = detect_local_profiles();
                let selected = resolve_local_profile(&profiles, &profile_ref)?;
                match open_local_profile_url(&selected, url) {
                    Ok(()) => (true, Some(selected), None),
                    Err(error) => (false, Some(selected), Some(format!("{error:#}"))),
                }
            } else {
                (open::that(url).is_ok(), None, None)
            };
            Ok(local_setup_user_action_response(
                opened, profile, open_error,
            ))
        }
        Some("profiles") => dispatch_local_profiles(argv),
        Some(other) => bail!("unknown browser local command: {other}"),
        None => bail!("browser local requires list, setup, or profiles"),
    }
}

fn local_setup_user_action_response(
    opened: bool,
    profile: Option<LocalBrowserProfile>,
    open_error: Option<String>,
) -> Value {
    json!({
        "status": "needs-user-action",
        "opened": opened,
        "url": "chrome://inspect/#remote-debugging",
        "profile": profile,
        "open_error": open_error,
        "instructions": [
            "In the browser/profile that opens, enable 'Allow remote debugging for this browser instance' if Chrome reports it is blocked.",
            "If Chrome shows an additional permission prompt, click Allow.",
            "Do not retry until the user confirms that permission is enabled, then run `browser connect local` again."
        ],
        "next_step": "Wait for user confirmation, then run browser connect local."
    })
}

fn dispatch_local_profiles(argv: &[String]) -> Result<Value> {
    if argv.get(2).map(String::as_str) == Some("inspect") {
        let profile = argv
            .get(3)
            .map(String::as_str)
            .ok_or_else(|| anyhow!("local profiles inspect requires <profile-name>"))?;
        return inspect_local_profile(profile, has_flag(argv, "--domains-only"));
    }
    list_local_profiles()
}

fn dispatch_remote(session: &mut BrowserSession, argv: &[String]) -> Result<Value> {
    match argv.get(1).map(String::as_str) {
        Some("start") => session.start_remote_cloud(argv),
        Some("stop") => session.stop_owned_remote(),
        Some("status") => Ok(session.status_json()),
        Some("live-url") => Ok(json!({ "live_url": session.live_url })),
        Some("profiles") => list_cloud_profiles(),
        Some(other) => bail!("unknown browser remote command: {other}"),
        None => bail!("browser remote requires start, stop, status, live-url, or profiles"),
    }
}

fn dispatch_recover(session: &mut BrowserSession, argv: &[String]) -> Result<Value> {
    match argv.get(1).map(String::as_str) {
        Some("reconnect-websocket") => session.reconnect_websocket(),
        Some("reattach-same-target") => session.reattach_same_target(),
        Some("restart-runtime") => session.restart_runtime(),
        Some("restart-owned-browser") => session.restart_owned_browser(),
        Some("stop-owned-remote") => session.stop_owned_remote(),
        Some(other) => bail!("unknown browser recover command: {other}"),
        None => bail!("browser recover requires a recovery action"),
    }
}

fn dispatch_runtime(session: &mut BrowserSession, argv: &[String]) -> Result<Value> {
    match argv.get(1).map(String::as_str) {
        Some("logs") => Ok(Value::String(
            session.logs.iter().cloned().collect::<Vec<_>>().join("\n"),
        )),
        Some("ownership") => Ok(session.ownership_json()),
        Some("cleanup-stale") => Ok(json!({
            "status": "ok",
            "cleaned": 0,
            "note": "No stale runtime files were removed. Rust browser state is in-process for this session.",
        })),
        Some(other) => bail!("unknown browser runtime command: {other}"),
        None => bail!("browser runtime requires logs, ownership, or cleanup-stale"),
    }
}

impl BrowserSession {
    fn log(&mut self, message: impl Into<String>) {
        let message = message.into();
        if self.logs.len() >= LOG_LIMIT {
            self.logs.pop_front();
        }
        self.logs
            .push_back(format!("[{}] {message}", unix_time_ms()));
    }

    fn browser_events(&mut self) -> Vec<Value> {
        let mut events = Vec::new();
        if self.mode == BrowserMode::None {
            self.last_emitted_browser_payload = None;
            return events;
        }
        let payload = self.browser_event_payload();
        if self.last_emitted_browser_payload.as_ref() == Some(&payload) {
            return events;
        }
        let event_type = self.browser_event_type(&payload);
        self.last_emitted_browser_payload = Some(payload.clone());
        events.push(json!({
            "type": event_type,
            "payload": payload,
        }));
        if let Some(live_url) = self.live_url.as_deref() {
            events.push(json!({
                "type": "browser.live_url",
                "payload": {
                    "live_url": live_url,
                    "url": live_url,
                },
            }));
        }
        events
    }

    fn browser_event_type(&self, payload: &Value) -> &'static str {
        let status = payload.get("status").and_then(Value::as_str);
        if status != Some("connected") {
            return "browser.disconnected";
        }
        let Some(previous) = self.last_emitted_browser_payload.as_ref() else {
            return "browser.connected";
        };
        if previous.get("status").and_then(Value::as_str) != Some("connected") {
            return "browser.reconnected";
        }
        if previous.get("target_id") != payload.get("target_id") {
            return "browser.target_changed";
        }
        if previous.get("session_id") != payload.get("session_id")
            || previous.get("generation") != payload.get("generation")
        {
            return "browser.reconnected";
        }
        "browser.connected"
    }

    fn browser_event_payload(&self) -> Value {
        json!({
            "backend": self.mode.as_str(),
            "status": if self.connection.is_some() { "connected" } else { "disconnected" },
            "target_id": self.current_target_id,
            "session_id": self.current_session_id,
            "generation": self.connection_generation,
            "live_url": self.live_url,
            "last_issue": self.last_issue_diagnosis(),
        })
    }

    fn status_json(&self) -> Value {
        let connected = self.connection.is_some();
        let page = json!({
            "target_id": self.current_target_id,
            "session_id": self.current_session_id,
            "last_target_id": self.last_target_id,
            "last_session_id": self.last_session_id,
        });
        json!({
            "mode": self.mode.as_str(),
            "connection": if connected { "connected" } else if self.endpoint.is_some() { "disconnected" } else { "not-configured" },
            "reason": self.last_error,
            "loss_reason": self.last_error_kind,
            "last_issue": self.last_issue_diagnosis(),
            "active_jobs": self.session_id.as_deref().map(active_browser_jobs_json).unwrap_or_default(),
            "next_step": self.next_step(),
            "owner": self.owner.as_str(),
            "browser": self.browser_name,
            "profile": self.profile,
            "endpoint": self.endpoint.as_ref().map(|endpoint| json!({
                "kind": endpoint.kind,
                "http_url": endpoint.http_url,
                "ws_url": redact_ws_url(&endpoint.ws_url),
                "candidate_id": endpoint.candidate_id,
            })),
            "page": page,
            "cdp_event_buffered_count": self.connection.as_ref().map(CdpConnection::buffered_event_count).unwrap_or(0),
            "safety": {
                "can_restart_browser": self.owner == BrowserOwner::Rust && self.mode == BrowserMode::Managed,
                "can_close_browser": self.owner == BrowserOwner::Rust && self.mode == BrowserMode::Managed,
                "can_stop_remote": self.owner == BrowserOwner::Rust && self.mode == BrowserMode::RemoteCloud && self.remote_browser_id.is_some(),
            },
            "connection_generation": self.connection_generation,
            "remote_browser_id": self.remote_browser_id,
            "live_url": self.live_url,
        })
    }

    fn last_issue_diagnosis(&self) -> Option<BrowserIssueDiagnosis> {
        self.last_error_kind.as_deref().map(|kind| {
            browser_issue_diagnosis(
                kind,
                self.connection.is_some(),
                self.connection.is_some()
                    && self.current_target_id.is_some()
                    && self.current_session_id.is_some(),
                self.next_step(),
            )
        })
    }

    fn ownership_json(&self) -> Value {
        json!({
            "owner": self.owner.as_str(),
            "mode": self.mode.as_str(),
            "endpoint": self.endpoint.as_ref().map(|endpoint| json!({
                "kind": endpoint.kind,
                "http_url": endpoint.http_url,
                "ws_url": redact_ws_url(&endpoint.ws_url),
                "candidate_id": endpoint.candidate_id,
            })),
            "managed_pid": self.managed.as_ref().map(|managed| managed.child.id()),
            "remote_browser_id": self.remote_browser_id,
            "target_id": self.current_target_id,
            "session_id": self.current_session_id,
            "connection_generation": self.connection_generation,
            "safe_actions": {
                "restart_runtime": self.endpoint.is_some(),
                "restart_owned_browser": self.owner == BrowserOwner::Rust && self.mode == BrowserMode::Managed,
                "stop_owned_remote": self.owner == BrowserOwner::Rust && self.mode == BrowserMode::RemoteCloud && self.remote_browser_id.is_some(),
            }
        })
    }

    fn next_step(&self) -> Option<&'static str> {
        if self.endpoint.is_none() {
            Some("browser connect local")
        } else if matches!(
            self.last_error_kind.as_deref(),
            Some("browser-closed" | "stale-port")
        ) && self.mode == BrowserMode::Local
        {
            Some("Open Chrome with the selected profile, then run browser connect local")
        } else if matches!(
            self.last_error_kind.as_deref(),
            Some("permission-blocked" | "cdp-disabled")
        ) && self.mode == BrowserMode::Local
        {
            Some("browser local setup")
        } else if self.connection.is_none() {
            Some("browser recover reconnect-websocket")
        } else if self.current_target_id.is_some() && self.current_session_id.is_none() {
            Some("browser recover reattach-same-target")
        } else {
            None
        }
    }

    fn connect_local(&mut self, candidate_id: Option<String>) -> Result<Value> {
        let candidates = local_candidates();
        if candidates.is_empty() {
            let disabled = local_debugging_disabled_statuses();
            if !disabled.is_empty() {
                self.last_error =
                    Some("Chrome is open, but remote debugging is turned off".to_string());
                self.last_error_kind = Some("cdp-disabled".to_string());
                return Ok(json!({
                    "status": "blocked",
                    "state": "cdp-disabled",
                    "reason": "Chrome is open, but remote debugging is turned off for this browser instance.",
                    "local_browsers": disabled,
                    "next_step": "browser local setup",
                }));
            }
            self.last_error =
                Some("No local remote-debugging browser candidates found".to_string());
            self.last_error_kind = Some("browser-not-running".to_string());
            return Ok(json!({
                "status": "blocked",
                "state": "browser-not-running",
                "reason": "No running Chromium-family browser is exposing a reachable local CDP endpoint.",
                "next_step": "browser local setup",
            }));
        }
        let reachable = candidates
            .iter()
            .filter(|candidate| candidate.connectable)
            .cloned()
            .collect::<Vec<_>>();
        if reachable.is_empty() {
            if candidates
                .iter()
                .any(|candidate| candidate.state == "cdp-disabled")
            {
                self.last_error =
                    Some("Chrome is open, but remote debugging is turned off".to_string());
                self.last_error_kind = Some("cdp-disabled".to_string());
                return Ok(json!({
                    "status": "blocked",
                    "state": "cdp-disabled",
                    "reason": "Chrome is open, but remote debugging is turned off for this browser instance.",
                    "candidates": candidates,
                    "next_step": "browser local setup",
                }));
            }
            self.last_error =
                Some("Only stale local browser debug candidates were found".to_string());
            self.last_error_kind = Some("stale-port".to_string());
            return Ok(json!({
                "status": "blocked",
                "state": "stale-port",
                "reason": "Found stale DevToolsActivePort files, but no local Chrome CDP port is reachable. Chrome was likely closed or the debug server stopped.",
                "candidates": candidates,
                "next_step": "Open Chrome with the selected profile, then run browser connect local",
            }));
        }
        let candidate = if let Some(candidate_id) = candidate_id {
            let Some(candidate) = candidates
                .into_iter()
                .find(|candidate| candidate.id == candidate_id)
            else {
                bail!("unknown local candidate id: {candidate_id}");
            };
            if !candidate.connectable {
                self.last_error = candidate.reason.clone();
                self.last_error_kind = Some(candidate.state.clone());
                return Ok(json!({
                    "status": "blocked",
                    "state": candidate.state,
                    "reason": candidate.reason,
                    "candidate": candidate,
                    "next_step": candidate.next_step.as_deref().unwrap_or("Open Chrome with this profile, then run browser connect local"),
                }));
            }
            candidate
        } else if reachable.len() == 1 {
            reachable
                .into_iter()
                .next()
                .expect("one reachable candidate")
        } else {
            return Ok(json!({
                "status": "needs-user-action",
                "reason": "Multiple reachable local browser candidates are available. Ask the user which browser/profile to attach.",
                "candidates": reachable,
                "ignored_candidates": candidates.into_iter().filter(|candidate| !candidate.connectable).collect::<Vec<_>>(),
                "next_step": "browser connect local --candidate <id>",
            }));
        };
        self.stop_owned_managed();
        let endpoint = Endpoint {
            kind: "devtools-active-port".to_string(),
            http_url: candidate.http_url.clone(),
            ws_url: candidate.ws_url.clone(),
            candidate_id: Some(candidate.id.clone()),
        };
        if let Err(error) =
            self.connect_endpoint(endpoint, BrowserMode::Local, BrowserOwner::External)
        {
            let message = format!("{error:#}");
            let kind = classify_browser_error(&message);
            self.last_error = Some(message.clone());
            self.last_error_kind = Some(kind.to_string());
            return Ok(json!({
                "status": "blocked",
                "state": kind,
                "reason": local_connect_error_reason(kind, &message),
                "candidate": candidate,
                "raw_error": message,
                "next_step": local_connect_next_step(kind),
            }));
        }
        self.browser_name = Some(candidate.browser_name.clone());
        self.profile = Some(candidate.profile_path.display().to_string());
        Ok(json!({
            "status": "connected",
            "candidate": candidate,
            "browser": self.status_json(),
        }))
    }

    fn connect_remote_http(&mut self, http_url: String) -> Result<Value> {
        let ws_url = resolve_ws_from_http(&http_url)?;
        self.stop_owned_managed();
        self.connect_endpoint(
            Endpoint {
                kind: "cdp-url".to_string(),
                http_url: Some(http_url),
                ws_url,
                candidate_id: None,
            },
            BrowserMode::RemoteCdp,
            BrowserOwner::External,
        )?;
        Ok(json!({ "status": "connected", "browser": self.status_json() }))
    }

    fn connect_remote_ws(&mut self, ws_url: String) -> Result<Value> {
        self.stop_owned_managed();
        self.connect_endpoint(
            Endpoint {
                kind: "cdp-ws".to_string(),
                http_url: None,
                ws_url,
                candidate_id: None,
            },
            BrowserMode::RemoteCdp,
            BrowserOwner::External,
        )?;
        Ok(json!({ "status": "connected", "browser": self.status_json() }))
    }

    fn connect_managed(
        &mut self,
        headless: bool,
        profile: ManagedProfile,
        extra_args: Vec<String>,
    ) -> Result<Value> {
        self.stop_owned_managed();
        let mut launch_errors = Vec::new();
        let mut launched = None;
        for executable in chromium_candidate_paths(headless) {
            let launch = ManagedLaunch {
                executable,
                profile: profile.clone(),
                headless,
                extra_args: extra_args.clone(),
            };
            match launch_managed_browser(launch.clone()) {
                Ok((managed, http_url)) => {
                    launched = Some((launch, managed, http_url));
                    break;
                }
                Err(error) => {
                    launch_errors.push(format!("{}: {error:#}", launch.executable));
                }
            }
        }
        let Some((launch, managed, http_url)) = launched else {
            if launch_errors.is_empty() {
                bail!(
                    "No Chromium executable found. Set CHROME_PATH or install Playwright Chromium."
                );
            }
            bail!(
                "No Chromium executable successfully exposed DevTools:\n{}",
                launch_errors.join("\n")
            );
        };
        let ws_url = resolve_ws_from_http(&http_url)?;
        self.managed = Some(managed);
        self.connect_endpoint(
            Endpoint {
                kind: "cdp-url".to_string(),
                http_url: Some(http_url),
                ws_url,
                candidate_id: None,
            },
            BrowserMode::Managed,
            BrowserOwner::Rust,
        )?;
        self.browser_name = Some("Managed Chromium".to_string());
        self.profile = Some(match &launch.profile {
            ManagedProfile::Temp => "temp".to_string(),
            ManagedProfile::Path(path) => path.display().to_string(),
        });
        Ok(json!({ "status": "connected", "browser": self.status_json() }))
    }

    fn start_remote_cloud(&mut self, argv: &[String]) -> Result<Value> {
        let mut body = serde_json::Map::new();
        if let Some(profile_id) = option_value(argv, "--profile-id") {
            body.insert("profileId".to_string(), Value::String(profile_id));
        }
        if let Some(profile_name) = option_value(argv, "--profile-name") {
            if body.contains_key("profileId") {
                bail!("pass --profile-id or --profile-name, not both");
            }
            let profile_id = resolve_cloud_profile_name(&profile_name)?;
            body.insert("profileId".to_string(), Value::String(profile_id));
        }
        if let Some(timeout) = option_value(argv, "--timeout") {
            let timeout: i64 = timeout
                .parse()
                .with_context(|| format!("invalid --timeout value: {timeout}"))?;
            body.insert("timeout".to_string(), Value::Number(timeout.into()));
        }
        if let Some(country) = option_value(argv, "--proxy-country") {
            if country.eq_ignore_ascii_case("none") {
                body.insert("proxyCountryCode".to_string(), Value::Null);
            } else {
                body.insert("proxyCountryCode".to_string(), Value::String(country));
            }
        }
        let browser = browser_use_api("/browsers", "POST", Some(Value::Object(body)))?;
        let id = browser
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("Browser Use API response missing browser id"))?
            .to_string();
        let cdp_url = browser
            .get("cdpUrl")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("Browser Use API response missing cdpUrl"))?
            .to_string();
        let ws_url = match resolve_ws_from_http(&cdp_url) {
            Ok(ws_url) => ws_url,
            Err(error) => {
                let _ = stop_cloud_browser(&id);
                return Err(error);
            }
        };
        self.stop_owned_managed();
        self.connect_endpoint(
            Endpoint {
                kind: "browser-use-cloud".to_string(),
                http_url: Some(cdp_url),
                ws_url,
                candidate_id: None,
            },
            BrowserMode::RemoteCloud,
            BrowserOwner::Rust,
        )?;
        self.remote_browser_id = Some(id);
        self.live_url = browser
            .get("liveUrl")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
        self.browser_name = Some("Browser Use cloud".to_string());
        Ok(json!({
            "status": "connected",
            "remote_browser": browser,
            "browser": self.status_json(),
            "live_url": self.live_url,
        }))
    }

    fn stop_owned_remote(&mut self) -> Result<Value> {
        if !(self.owner == BrowserOwner::Rust && self.mode == BrowserMode::RemoteCloud) {
            return Ok(json!({
                "stopped": false,
                "reason": "current browser is not a Rust-owned Browser Use cloud browser",
            }));
        }
        let Some(id) = self.remote_browser_id.clone() else {
            return Ok(json!({ "stopped": false, "reason": "missing remote browser id" }));
        };
        stop_cloud_browser(&id)?;
        self.connection = None;
        self.endpoint = None;
        self.current_session_id = None;
        self.current_target_id = None;
        self.remote_browser_id = None;
        self.live_url = None;
        self.mode = BrowserMode::None;
        self.owner = BrowserOwner::None;
        self.last_error = None;
        self.last_error_kind = None;
        self.last_target_id = None;
        self.last_session_id = None;
        self.connection_generation += 1;
        Ok(json!({ "stopped": true, "browser_id": id }))
    }

    fn connect_endpoint(
        &mut self,
        endpoint: Endpoint,
        mode: BrowserMode,
        owner: BrowserOwner,
    ) -> Result<()> {
        let ws_url = endpoint.ws_url.clone();
        let connection = CdpConnection::connect(&ws_url)?;
        self.endpoint = Some(endpoint);
        self.connection = Some(connection);
        self.mode = mode;
        self.owner = owner;
        self.connection_generation += 1;
        self.last_error = None;
        self.last_error_kind = None;
        self.last_target_id = None;
        self.last_session_id = None;
        self.attach_first_page()?;
        Ok(())
    }

    fn reconnect_websocket(&mut self) -> Result<Value> {
        let Some(endpoint) = self.endpoint.clone() else {
            bail!("no browser endpoint is configured");
        };
        self.connection = Some(CdpConnection::connect(&endpoint.ws_url)?);
        self.connection_generation += 1;
        if self.current_target_id.is_some() {
            let _ = self.reattach_same_target();
        } else {
            let _ = self.attach_first_page();
        }
        Ok(json!({
            "status": "reconnected",
            "browser": self.status_json(),
        }))
    }

    fn reattach_same_target(&mut self) -> Result<Value> {
        let target_id = self
            .current_target_id
            .clone()
            .ok_or_else(|| anyhow!("no previous target_id to reattach"))?;
        let targets = self.targets()?;
        if !targets.iter().any(|target| target["targetId"] == target_id) {
            return Ok(json!({
                "status": "target-gone",
                "target_id": target_id,
                "available_targets": targets,
                "next_step": "Use browser_execute listPageTargets() or open a new tab.",
            }));
        }
        let session_id = self.attach_target(&target_id)?;
        self.current_target_id = Some(target_id.clone());
        self.current_session_id = Some(session_id.clone());
        self.connection_generation += 1;
        Ok(json!({
            "status": "reattached",
            "target_id": target_id,
            "session_id": session_id,
            "browser": self.status_json(),
        }))
    }

    fn restart_runtime(&mut self) -> Result<Value> {
        self.connection = None;
        self.current_session_id = None;
        self.connection_generation += 1;
        self.reconnect_websocket()
    }

    fn restart_owned_browser(&mut self) -> Result<Value> {
        if !(self.owner == BrowserOwner::Rust && self.mode == BrowserMode::Managed) {
            return Ok(json!({
                "restarted": false,
                "reason": "restart-owned-browser only works for Rust-owned managed browsers",
            }));
        }
        let launch = self
            .managed
            .as_ref()
            .map(|managed| managed.launch.clone())
            .ok_or_else(|| anyhow!("missing managed launch config"))?;
        self.stop_owned_managed();
        self.connect_managed(launch.headless, launch.profile, launch.extra_args)?;
        Ok(json!({ "restarted": true, "browser": self.status_json() }))
    }

    fn stop_owned_managed(&mut self) {
        if let Some(mut managed) = self.managed.take() {
            let _ = managed.child.kill();
            let _ = managed.child.wait();
        }
        if self.mode == BrowserMode::Managed {
            self.connection = None;
            self.endpoint = None;
            self.current_target_id = None;
            self.current_session_id = None;
            self.mode = BrowserMode::None;
            self.owner = BrowserOwner::None;
            self.last_error = None;
            self.last_error_kind = None;
            self.last_target_id = None;
            self.last_session_id = None;
            self.connection_generation += 1;
        }
    }

    fn doctor(&mut self, cwd: &Path) -> Result<Value> {
        let candidates = local_candidates();
        let debugging_disabled = local_debugging_disabled_statuses();
        let mut checks = Vec::new();
        checks.push(json!({
            "name": "runtime state",
            "ok": true,
            "detail": "Rust browser runtime is available in-process",
        }));
        checks.push(json!({
            "name": "local browser candidates",
            "ok": candidates.iter().any(|candidate| candidate.connectable),
            "count": candidates.len(),
            "connectable_count": candidates.iter().filter(|candidate| candidate.connectable).count(),
            "stale_count": candidates.iter().filter(|candidate| candidate.stale).count(),
            "cdp_disabled_count": candidates.iter().filter(|candidate| candidate.state == "cdp-disabled").count(),
            "state": if candidates.iter().any(|candidate| candidate.connectable) {
                "reachable"
            } else if candidates.iter().any(|candidate| candidate.state == "cdp-disabled") {
                "cdp-disabled"
            } else if candidates.iter().any(|candidate| candidate.stale) {
                "stale-port"
            } else if !debugging_disabled.is_empty() {
                "cdp-disabled"
            } else {
                "browser-not-running"
            },
            "detail": if candidates.iter().any(|candidate| candidate.connectable) {
                "At least one local browser CDP endpoint is reachable."
            } else if candidates.iter().any(|candidate| candidate.state == "cdp-disabled")
                || !debugging_disabled.is_empty()
            {
                "Chrome is open, but remote debugging is turned off for this browser instance."
            } else if candidates.iter().any(|candidate| candidate.stale) {
                "DevToolsActivePort files exist, but their ports are not reachable. Chrome was likely closed or restarted."
            } else {
                "No local browser CDP endpoint is reachable."
            },
            "next_step": if candidates.iter().any(|candidate| candidate.connectable) {
                "browser connect local"
            } else if candidates.iter().any(|candidate| candidate.state == "cdp-disabled")
                || !debugging_disabled.is_empty()
            {
                "browser local setup"
            } else if candidates.iter().any(|candidate| candidate.stale) {
                "Open Chrome with the selected profile, then run browser connect local"
            } else {
                "browser local setup"
            },
        }));
        let profiles = detect_local_profiles();
        checks.push(json!({
            "name": "local browser profiles",
            "ok": !profiles.is_empty(),
            "count": profiles.len(),
            "detail": "Rust filesystem profile discovery; no external CLI required",
            "next_step": if profiles.is_empty() { "Use `browser local profiles --json` to see scan details." } else { "browser local profiles --json" },
        }));
        checks.push(json!({
            "name": "Browser Use API key",
            "ok": std::env::var("BROWSER_USE_API_KEY").is_ok_and(|value| !value.trim().is_empty()),
            "detail": "Only required for Browser Use cloud browsers and cloud profiles",
        }));
        if let Some(endpoint) = self.endpoint.as_ref() {
            let endpoint_probe = probe_endpoint(endpoint);
            let cdp_ok = endpoint_probe.ok;
            checks.push(json!({
                "name": "CDP websocket",
                "ok": cdp_ok,
                "state": endpoint_probe.state,
                "detail": endpoint_probe.detail,
                "next_step": if cdp_ok {
                    ""
                } else if self.mode == BrowserMode::Local {
                    endpoint_probe.next_step
                } else {
                    "browser recover reconnect-websocket"
                },
            }));
            let target_ok =
                cdp_ok && self.current_target_id.is_some() && self.current_session_id.is_some();
            checks.push(json!({
                "name": "current target",
                "ok": target_ok,
                "target_id": self.current_target_id,
                "last_target_id": self.last_target_id,
                "next_step": if target_ok { "" } else if cdp_ok { "browser recover reattach-same-target" } else { "Recover the browser connection before reattaching a target." },
            }));
        }
        checks.push(json!({
            "name": "cwd",
            "ok": cwd.exists(),
            "path": cwd.display().to_string(),
        }));
        Ok(json!({
            "status": if checks.iter().all(|check| check.get("ok").and_then(Value::as_bool).unwrap_or(false)) { "ok" } else { "needs-action" },
            "checks": checks,
            "browser": self.status_json(),
        }))
    }

    fn cdp(&mut self, method: &str, session_id: Option<&str>, params: Value) -> Result<Value> {
        let Some(connection) = self.connection.as_mut() else {
            bail!(
                "browser is not connected. Run `browser status --json` or `browser connect ...`."
            );
        };
        match connection.call(method, session_id, params.clone()) {
            Ok(value) => Ok(value),
            Err(error) => {
                let mut message = format!("{error:#}");
                let is_current_session = session_id.is_some()
                    && session_id == self.current_session_id.as_deref()
                    && self.current_target_id.is_some();
                if is_current_session && is_stale_session_error(&message) {
                    self.last_error = Some(message.clone());
                    self.last_error_kind = Some("session-gone".to_string());
                    self.last_session_id = self.current_session_id.take();

                    match self.reattach_same_target() {
                        Ok(recovery)
                            if recovery.get("status").and_then(Value::as_str)
                                == Some("reattached") =>
                        {
                            let retry_session_id = self.current_session_id.clone();
                            let retry = self.connection.as_mut().map_or_else(
                                || Err(anyhow!("browser connection was lost during reattach")),
                                |connection| {
                                    connection.call(
                                        method,
                                        retry_session_id.as_deref(),
                                        params.clone(),
                                    )
                                },
                            );
                            match retry {
                                Ok(value) => {
                                    self.last_error = None;
                                    self.last_error_kind = None;
                                    return Ok(value);
                                }
                                Err(retry_error) => {
                                    message = format!("{retry_error:#}");
                                }
                            }
                        }
                        Ok(recovery) => {
                            let failure = format!(
                                "CDP {method} failed because the current session is stale and reattach did not recover it: {message}; reattach result: {recovery}"
                            );
                            self.last_error = Some(failure.clone());
                            self.last_error_kind = Some("target-gone".to_string());
                            bail!(failure);
                        }
                        Err(recovery_error) => {
                            let failure = format!(
                                "CDP {method} failed because the current session is stale and reattach failed: {message}; recovery error: {recovery_error:#}"
                            );
                            self.last_error = Some(failure.clone());
                            self.last_error_kind = Some("session-gone".to_string());
                            bail!(failure);
                        }
                    }
                }
                let error_kind = classify_browser_error(&message);
                if matches!(error_kind, "browser-closed" | "websocket-dropped")
                    && self.endpoint.is_some()
                {
                    self.last_error = Some(message.clone());
                    self.last_error_kind = Some(error_kind.to_string());
                    match self.reconnect_websocket() {
                        Ok(_) => {
                            let retry_session_id = if is_current_session {
                                self.current_session_id.clone()
                            } else {
                                session_id.map(ToOwned::to_owned)
                            };
                            let retry = self.connection.as_mut().map_or_else(
                                || Err(anyhow!("browser connection was lost during reconnect")),
                                |connection| {
                                    connection.call(
                                        method,
                                        retry_session_id.as_deref(),
                                        params.clone(),
                                    )
                                },
                            );
                            match retry {
                                Ok(value) => {
                                    self.last_error = None;
                                    self.last_error_kind = None;
                                    return Ok(value);
                                }
                                Err(retry_error) => {
                                    message = format!("{retry_error:#}");
                                }
                            }
                        }
                        Err(reconnect_error) => {
                            message = format!(
                                "{message}; reconnect after dropped CDP websocket failed: {reconnect_error:#}"
                            );
                        }
                    }
                }
                let final_error_kind = classify_browser_error(&message);
                self.last_error = Some(message.clone());
                self.last_error_kind = Some(final_error_kind.to_string());
                if should_drop_browser_connection(final_error_kind) {
                    self.connection = None;
                    self.last_target_id = self.current_target_id.take();
                    self.last_session_id = self.current_session_id.take();
                }
                bail!(message);
            }
        }
    }

    fn attach_first_page(&mut self) -> Result<()> {
        let targets = self.targets()?;
        let target_id = targets
            .iter()
            .find(|target| is_real_page_target(target))
            .and_then(|target| target.get("targetId").and_then(Value::as_str))
            .map(ToOwned::to_owned);
        let target_id = match target_id {
            Some(target_id) => target_id,
            None => self
                .cdp("Target.createTarget", None, json!({ "url": "about:blank" }))?
                .get("targetId")
                .and_then(Value::as_str)
                .ok_or_else(|| anyhow!("Target.createTarget response missing targetId"))?
                .to_string(),
        };
        let session_id = self.attach_target(&target_id)?;
        self.current_target_id = Some(target_id);
        self.current_session_id = Some(session_id);
        let _ = self.cdp_current("Runtime.enable", json!({}));
        let _ = self.cdp_current("Page.enable", json!({}));
        Ok(())
    }

    fn cdp_current(&mut self, method: &str, params: Value) -> Result<Value> {
        let session_id = self.current_session_id.clone().ok_or_else(|| {
            anyhow!("no current browser session; run `browser recover reattach-same-target`")
        })?;
        self.cdp(method, Some(&session_id), params)
    }

    fn targets(&mut self) -> Result<Vec<Value>> {
        let result = self.cdp("Target.getTargets", None, json!({}))?;
        Ok(result
            .get("targetInfos")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    fn attach_target(&mut self, target_id: &str) -> Result<String> {
        let result = self.cdp(
            "Target.attachToTarget",
            None,
            json!({ "targetId": target_id, "flatten": true }),
        )?;
        result
            .get("sessionId")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned)
            .ok_or_else(|| anyhow!("Target.attachToTarget response missing sessionId"))
    }
}

impl CdpConnection {
    fn connect(ws_url: &str) -> Result<Self> {
        let (mut socket, _) =
            connect(ws_url).with_context(|| format!("connect CDP websocket {ws_url}"))?;
        set_cdp_broker_socket_timeouts(&mut socket);
        let (tx, rx) = mpsc::channel();
        let events = Arc::new(Mutex::new(VecDeque::new()));
        let broker_events = Arc::clone(&events);
        thread::Builder::new()
            .name("browser-cdp-broker".to_string())
            .spawn(move || run_cdp_broker(socket, rx, broker_events))
            .context("spawn CDP broker")?;
        Ok(Self { tx, events })
    }

    fn call(&mut self, method: &str, session_id: Option<&str>, params: Value) -> Result<Value> {
        self.call_with_timeout(method, session_id, params, Duration::from_secs(20))
    }

    fn cdp_storage_cookies(&mut self) -> Result<Vec<Value>> {
        Ok(self
            .call("Storage.getCookies", None, json!({}))?
            .get("cookies")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    fn call_with_timeout(
        &self,
        method: &str,
        session_id: Option<&str>,
        params: Value,
        timeout: Duration,
    ) -> Result<Value> {
        let (response, response_rx) = mpsc::channel();
        self.tx
            .send(CdpBrokerCommand::Call(CdpBrokerCall {
                method: method.to_string(),
                session_id: session_id.map(ToOwned::to_owned),
                params,
                response,
            }))
            .with_context(|| format!("send CDP broker request {method}"))?;
        match response_rx.recv_timeout(timeout) {
            Ok(Ok(value)) => Ok(value),
            Ok(Err(error)) => bail!(error),
            Err(mpsc::RecvTimeoutError::Timeout) => {
                bail!(
                    "read CDP {method} timed out after {} ms",
                    timeout.as_millis()
                )
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                bail!("CDP broker disconnected while waiting for {method}")
            }
        }
    }

    fn buffered_event_count(&self) -> usize {
        self.events.lock().map(|events| events.len()).unwrap_or(0)
    }

    fn drain_events(&self) -> Vec<Value> {
        let Ok(mut events) = self.events.lock() else {
            return Vec::new();
        };
        events.drain(..).collect()
    }
}

fn run_cdp_broker(
    mut socket: WebSocket<MaybeTlsStream<TcpStream>>,
    rx: mpsc::Receiver<CdpBrokerCommand>,
    events: Arc<Mutex<VecDeque<Value>>>,
) {
    let mut next_id = 1u64;
    let mut pending: HashMap<u64, (String, mpsc::Sender<std::result::Result<Value, String>>)> =
        HashMap::new();
    let mut input_closed = false;
    loop {
        loop {
            match rx.try_recv() {
                Ok(CdpBrokerCommand::Call(call)) => {
                    let id = next_id;
                    next_id += 1;
                    let mut message = json!({
                        "id": id,
                        "method": call.method,
                        "params": call.params,
                    });
                    if let Some(session_id) = call.session_id {
                        message["sessionId"] = Value::String(session_id);
                    }
                    let method = message
                        .get("method")
                        .and_then(Value::as_str)
                        .unwrap_or("CDP.call")
                        .to_string();
                    match serde_json::to_string(&message)
                        .map_err(|error| error.to_string())
                        .and_then(|text| {
                            socket
                                .send(Message::Text(text))
                                .map_err(|error| format!("send CDP {method}: {error}"))
                        }) {
                        Ok(()) => {
                            pending.insert(id, (method, call.response));
                        }
                        Err(error) => {
                            let _ = call.response.send(Err(error));
                        }
                    }
                }
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => {
                    input_closed = true;
                    break;
                }
            }
        }

        if input_closed && pending.is_empty() {
            break;
        }

        match socket.read() {
            Ok(Message::Text(text)) => {
                let value = match serde_json::from_str::<Value>(&text) {
                    Ok(value) => value,
                    Err(error) => {
                        push_cdp_event(
                            &events,
                            json!({
                                "method": "BrowserUse.cdpParseError",
                                "params": { "error": error.to_string(), "raw": text },
                            }),
                        );
                        continue;
                    }
                };
                if let Some(id) = value.get("id").and_then(Value::as_u64) {
                    if let Some((method, response)) = pending.remove(&id) {
                        let result = if let Some(error) = value.get("error") {
                            Err(format!("CDP {method} failed: {error}"))
                        } else {
                            Ok(value.get("result").cloned().unwrap_or(Value::Null))
                        };
                        let _ = response.send(result);
                    } else {
                        push_cdp_event(&events, value);
                    }
                } else {
                    push_cdp_event(&events, value);
                }
            }
            Ok(Message::Close(frame)) => {
                fail_pending_cdp(&mut pending, format!("CDP websocket closed: {frame:?}"));
                break;
            }
            Ok(Message::Ping(bytes)) => {
                let _ = socket.send(Message::Pong(bytes));
            }
            Ok(_) => {}
            Err(tungstenite::Error::Io(error))
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                continue;
            }
            Err(error) => {
                fail_pending_cdp(&mut pending, format!("read CDP websocket: {error}"));
                break;
            }
        }
    }
}

fn push_cdp_event(events: &Arc<Mutex<VecDeque<Value>>>, event: Value) {
    let Ok(mut events) = events.lock() else {
        return;
    };
    if events.len() >= 1_000 {
        events.pop_front();
    }
    events.push_back(event);
}

fn fail_pending_cdp(
    pending: &mut HashMap<u64, (String, mpsc::Sender<std::result::Result<Value, String>>)>,
    message: String,
) {
    for (_, (method, response)) in pending.drain() {
        let _ = response.send(Err(format!("{message}; pending method: {method}")));
    }
}

fn set_cdp_broker_socket_timeouts(socket: &mut WebSocket<MaybeTlsStream<TcpStream>>) {
    match socket.get_mut() {
        MaybeTlsStream::Plain(stream) => {
            let _ = stream.set_read_timeout(Some(Duration::from_millis(50)));
            let _ = stream.set_write_timeout(Some(Duration::from_secs(20)));
        }
        MaybeTlsStream::Rustls(stream) => {
            let _ = stream
                .sock
                .set_read_timeout(Some(Duration::from_millis(50)));
            let _ = stream.sock.set_write_timeout(Some(Duration::from_secs(20)));
        }
        _ => {}
    }
}

fn classify_browser_error(message: &str) -> &'static str {
    let lower = message.to_ascii_lowercase();
    if lower.contains("403 forbidden") || lower.contains("http error: 403") {
        "permission-blocked"
    } else if lower.contains("target")
        && (lower.contains("not found")
            || lower.contains("target-gone")
            || lower.contains("no target with given id"))
    {
        "target-gone"
    } else if is_stale_session_error(message) {
        "session-gone"
    } else if (lower.contains("resource temporarily unavailable")
        || lower.contains("would block")
        || lower.contains("timed out"))
        && lower.contains("read cdp")
    {
        "cdp-read-timeout"
    } else if lower.contains("connection refused")
        || lower.contains("couldn't connect to server")
        || lower.contains("unable to connect")
        || lower.contains("operation timed out")
        || lower.contains("broken pipe")
        || lower.contains("connection reset")
        || lower.contains("websocket closed")
        || lower.contains("already closed")
    {
        "browser-closed"
    } else {
        "websocket-dropped"
    }
}

fn classify_browser_job_failure(message: &str) -> &'static str {
    let lower = message.to_ascii_lowercase();
    if lower.contains("timed out") && lower.contains("browser_execute") {
        "browser-job-timeout"
    } else if lower.contains("read cdp")
        || lower.contains("send cdp")
        || lower.contains("cdp websocket")
        || lower.contains("browser is not connected")
        || lower.contains("connection refused")
        || lower.contains("couldn't connect to server")
        || lower.contains("unable to connect")
        || lower.contains("operation timed out")
        || lower.contains("broken pipe")
        || lower.contains("connection reset")
        || lower.contains("websocket closed")
        || lower.contains("already closed")
        || (lower.contains("target")
            && (lower.contains("not found")
                || lower.contains("target-gone")
                || lower.contains("no target with given id")))
        || is_stale_session_error(message)
    {
        classify_browser_error(message)
    } else {
        "browser-job-error"
    }
}

fn browser_issue_diagnosis(
    error_kind: &str,
    browser_connected: bool,
    page_usable: bool,
    status_next_step: Option<&str>,
) -> BrowserIssueDiagnosis {
    let fallback_next_step = || {
        status_next_step
            .unwrap_or("Run browser status --json to check the connection before continuing.")
            .to_string()
    };
    let (summary, what_happened, next_step, browser_usable, page_usable) = match error_kind {
        "cdp-read-timeout" => (
            if page_usable {
                "Browser is still connected; the same page should still be usable."
            } else if browser_connected {
                "Browser is still connected, but the current page attachment is unclear."
            } else {
                "The CDP read timed out and browser state needs a status check."
            },
            "A CDP read for this browser_execute call timed out or returned would-block while waiting for Chrome.",
            if page_usable {
                "Continue on the same page, but rerun a smaller browser_execute job or resume from the last checkpoint.".to_string()
            } else {
                fallback_next_step()
            },
            browser_connected,
            page_usable,
        ),
        "browser-job-timeout" => (
            if page_usable {
                "The browser job timed out, but the browser page should still be reusable."
            } else if browser_connected {
                "The browser job timed out; browser is connected but page state needs checking."
            } else {
                "The browser job timed out and browser state needs a status check."
            },
            "The JavaScript browser job exceeded its timeout before returning a result.",
            if page_usable {
                "Retry with a shorter bounded browser_execute job and continue from the last checkpoint.".to_string()
            } else {
                fallback_next_step()
            },
            browser_connected,
            page_usable,
        ),
        "browser-job-error" => (
            if page_usable {
                "The browser job failed, but the browser page should still be reusable."
            } else if browser_connected {
                "The browser job failed; browser is connected but page state needs checking."
            } else {
                "The browser job failed and browser state needs a status check."
            },
            "The JavaScript browser job raised an error before completing.",
            if page_usable {
                "Fix the browser_execute JavaScript and rerun; keep using the same page state.".to_string()
            } else {
                fallback_next_step()
            },
            browser_connected,
            page_usable,
        ),
        "session-gone" => (
            if browser_connected {
                "Browser is connected, but the current page session is stale."
            } else {
                "The current page session is stale and browser state needs recovery."
            },
            "Chrome reported that the CDP session id no longer exists for the target.",
            if browser_connected {
                "Run browser recover reattach-same-target, then continue on the recovered page."
                    .to_string()
            } else {
                fallback_next_step()
            },
            browser_connected,
            false,
        ),
        "target-gone" => (
            if browser_connected {
                "Browser is connected, but the previous tab or target is gone."
            } else {
                "The previous tab or target is gone and browser state needs recovery."
            },
            "Chrome reported that the controlled target no longer exists.",
            if browser_connected {
                "Select an existing tab or create a new tab, then continue from the last checkpoint."
                    .to_string()
            } else {
                fallback_next_step()
            },
            browser_connected,
            false,
        ),
        "permission-blocked" => (
            "Chrome rejected browser control.",
            "The browser endpoint returned a permission or 403 error for CDP control.",
            status_next_step.unwrap_or("Run browser local setup, then reconnect.").to_string(),
            false,
            false,
        ),
        "cdp-disabled" => (
            "Chrome is open, but remote debugging is turned off.",
            "Chrome is running, but it is not exposing a local CDP endpoint because remote debugging is disabled for this browser instance.",
            status_next_step
                .unwrap_or("Run browser local setup, enable remote debugging, then reconnect.")
                .to_string(),
            false,
            false,
        ),
        "browser-closed" | "websocket-dropped" | "browser-not-running" | "stale-port" => (
            "Browser connection is not usable until it is recovered.",
            "The CDP websocket was closed, reset, refused, or pointed at a stale browser endpoint.",
            fallback_next_step(),
            false,
            false,
        ),
        _ => (
            if page_usable {
                "The browser page may still be reusable, but the failure needs checking."
            } else {
                "Browser state is unclear after this failure."
            },
            "The browser tool reported an unclassified failure.",
            fallback_next_step(),
            browser_connected,
            page_usable,
        ),
    };
    BrowserIssueDiagnosis {
        summary: summary.to_string(),
        what_happened: what_happened.to_string(),
        next_step,
        browser_usable,
        page_usable,
        error_kind: error_kind.to_string(),
    }
}

fn should_drop_browser_connection(error_kind: &str) -> bool {
    matches!(error_kind, "browser-closed" | "websocket-dropped")
}

fn is_stale_session_error(message: &str) -> bool {
    let lower = message.to_ascii_lowercase();
    lower.contains("session")
        && (lower.contains("not found")
            || lower.contains("no session")
            || lower.contains("session closed")
            || lower.contains("session with given id"))
}

fn local_connect_error_reason(kind: &str, raw_error: &str) -> String {
    match kind {
        "permission-blocked" => {
            "A local Chrome DevTools endpoint is reachable, but Chrome rejected CDP control. Remote debugging permission is likely blocked for this browser instance.".to_string()
        }
        "cdp-disabled" => {
            "Chrome is open, but remote debugging is turned off for this browser instance."
                .to_string()
        }
        "browser-closed" => {
            "Chrome is not currently exposing the selected local CDP endpoint. It may have been closed, restarted, or stopped its debug server.".to_string()
        }
        "target-gone" => "The previous browser tab target is gone.".to_string(),
        _ => format!("Local browser CDP connection failed: {raw_error}"),
    }
}

fn local_connect_next_step(kind: &str) -> &'static str {
    match kind {
        "permission-blocked" | "cdp-disabled" => "browser local setup",
        "browser-closed" => "Open Chrome with the selected profile, then run browser connect local",
        "target-gone" => "Use browser_execute listPageTargets() or open a new tab",
        _ => "browser doctor --json",
    }
}

struct EndpointProbe {
    ok: bool,
    state: &'static str,
    detail: String,
    next_step: &'static str,
}

fn probe_endpoint(endpoint: &Endpoint) -> EndpointProbe {
    let Some(http_url) = endpoint.http_url.as_deref() else {
        return EndpointProbe {
            ok: true,
            state: "unknown",
            detail:
                "No DevTools HTTP endpoint is available to probe without touching the websocket."
                    .to_string(),
            next_step: "browser recover reconnect-websocket",
        };
    };
    let url = format!("{}/json/version", http_url.trim_end_matches('/'));
    let response = Client::new()
        .get(&url)
        .timeout(Duration::from_secs(2))
        .send();
    match response {
        Ok(response) if response.status().is_success() => EndpointProbe {
            ok: true,
            state: "reachable",
            detail: format!("{url} is reachable."),
            next_step: "",
        },
        Ok(response) if response.status().as_u16() == 403 => EndpointProbe {
            ok: false,
            state: "permission-blocked",
            detail: "The browser is reachable, but Chrome rejected DevTools access with 403."
                .to_string(),
            next_step: "browser local setup",
        },
        Ok(response) => EndpointProbe {
            ok: false,
            state: "endpoint-error",
            detail: format!("{url} returned HTTP {}.", response.status()),
            next_step: "browser recover reconnect-websocket",
        },
        Err(error) => EndpointProbe {
            ok: false,
            state: if endpoint.kind == "devtools-active-port" {
                "browser-closed"
            } else {
                "websocket-dropped"
            },
            detail: format!("{url} is not reachable: {error:#}"),
            next_step: if endpoint.kind == "devtools-active-port" {
                "Open Chrome with the selected profile, then run browser connect local"
            } else {
                "browser recover reconnect-websocket"
            },
        },
    }
}

#[derive(Debug, Clone, Serialize)]
struct LocalCandidate {
    id: String,
    browser_name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    browser_path: Option<PathBuf>,
    profile_path: PathBuf,
    http_url: Option<String>,
    ws_url: String,
    source: String,
    connectable: bool,
    state: String,
    stale: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    browser_running: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    remote_debugging_enabled: Option<bool>,
    reason: Option<String>,
    next_step: Option<String>,
}

#[derive(Debug, Clone)]
struct LocalCandidateRoot {
    browser_name: String,
    browser_path: Option<PathBuf>,
    user_data_dir: PathBuf,
}

#[derive(Debug, Clone, Serialize)]
struct LocalBrowserDebuggingStatus {
    browser_name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    browser_path: Option<PathBuf>,
    user_data_dir: PathBuf,
    browser_running: bool,
    remote_debugging_enabled: Option<bool>,
}

fn local_candidates() -> Vec<LocalCandidate> {
    let mut roots = local_candidate_roots_from_installs(known_local_browser_installs());
    let mut seen_roots = roots
        .iter()
        .map(|root| (root.browser_name.clone(), root.user_data_dir.clone()))
        .collect::<HashSet<_>>();
    for (browser_name, user_data_dir) in known_profile_roots() {
        if seen_roots.insert((browser_name.to_string(), user_data_dir.clone())) {
            roots.push(LocalCandidateRoot {
                browser_name: browser_name.to_string(),
                browser_path: None,
                user_data_dir,
            });
        }
    }
    local_candidates_from_candidate_roots(roots, &[9222_u16, 9223])
}

fn local_candidate_roots_from_installs(
    installs: Vec<LocalBrowserInstall>,
) -> Vec<LocalCandidateRoot> {
    installs
        .into_iter()
        .map(|install| LocalCandidateRoot {
            browser_name: install.browser_name,
            browser_path: Some(install.browser_path),
            user_data_dir: install.user_data_dir,
        })
        .collect()
}

#[cfg(test)]
fn local_candidates_from_roots(
    roots: Vec<(&'static str, PathBuf)>,
    probe_ports: &[u16],
) -> Vec<LocalCandidate> {
    let roots = roots
        .into_iter()
        .map(|(browser_name, user_data_dir)| LocalCandidateRoot {
            browser_name: browser_name.to_string(),
            browser_path: None,
            user_data_dir,
        })
        .collect();
    local_candidates_from_candidate_roots(roots, probe_ports)
}

fn local_candidates_from_candidate_roots(
    roots: Vec<LocalCandidateRoot>,
    probe_ports: &[u16],
) -> Vec<LocalCandidate> {
    let mut candidates = Vec::new();
    let mut seen = HashSet::new();
    for root in roots {
        let active = root.user_data_dir.join("DevToolsActivePort");
        let Ok(raw) = fs::read_to_string(&active) else {
            continue;
        };
        let mut lines = raw.lines();
        let Some(port) = lines.next().map(str::trim).filter(|line| !line.is_empty()) else {
            continue;
        };
        let Some(path) = lines.next().map(str::trim).filter(|line| !line.is_empty()) else {
            continue;
        };
        let ws_url = format!("ws://127.0.0.1:{port}{path}");
        if !seen.insert(ws_url.clone()) {
            continue;
        }
        let id = format!("local-{}", candidates.len() + 1);
        let http_url = Some(format!("http://127.0.0.1:{port}"));
        let connectable = tcp_port_open("127.0.0.1", port.parse().unwrap_or(0));
        let browser_running = root
            .browser_path
            .as_deref()
            .and_then(|path| browser_process_running(&root.browser_name, path));
        let remote_debugging_enabled = remote_debugging_user_enabled(&root.user_data_dir);
        let (state, reason, next_step) = if connectable {
            ("reachable", None, "browser connect local --candidate <id>")
        } else {
            let (state, reason, next_step) =
                local_disconnected_candidate_details(browser_running, remote_debugging_enabled);
            (state, Some(reason), next_step)
        };
        candidates.push(LocalCandidate {
            id,
            browser_name: root.browser_name,
            browser_path: root.browser_path,
            profile_path: root.user_data_dir,
            http_url,
            ws_url,
            source: active.display().to_string(),
            connectable,
            state: state.to_string(),
            stale: !connectable,
            browser_running,
            remote_debugging_enabled,
            reason,
            next_step: Some(next_step.to_string()),
        });
    }
    for port in probe_ports {
        let http_url = format!("http://127.0.0.1:{port}");
        let Ok(ws_url) = resolve_ws_from_http(&http_url) else {
            continue;
        };
        if !seen.insert(ws_url.clone()) {
            continue;
        }
        candidates.push(LocalCandidate {
            id: format!("local-{}", candidates.len() + 1),
            browser_name: format!("CDP port {port}"),
            browser_path: None,
            profile_path: PathBuf::new(),
            http_url: Some(http_url),
            ws_url,
            source: "port-probe".to_string(),
            connectable: true,
            state: "reachable".to_string(),
            stale: false,
            browser_running: None,
            remote_debugging_enabled: None,
            reason: None,
            next_step: Some("browser connect local --candidate <id>".to_string()),
        });
    }
    candidates
}

fn local_debugging_disabled_statuses() -> Vec<LocalBrowserDebuggingStatus> {
    known_local_browser_installs()
        .into_iter()
        .filter_map(|install| {
            let browser_running =
                browser_process_running(&install.browser_name, &install.browser_path)?;
            let remote_debugging_enabled = remote_debugging_user_enabled(&install.user_data_dir);
            (browser_running && remote_debugging_enabled == Some(false)).then_some(
                LocalBrowserDebuggingStatus {
                    browser_name: install.browser_name,
                    browser_path: Some(install.browser_path),
                    user_data_dir: install.user_data_dir,
                    browser_running,
                    remote_debugging_enabled,
                },
            )
        })
        .collect()
}

fn local_disconnected_candidate_details(
    browser_running: Option<bool>,
    remote_debugging_enabled: Option<bool>,
) -> (&'static str, String, &'static str) {
    if browser_running == Some(true) && remote_debugging_enabled == Some(false) {
        return (
            "cdp-disabled",
            "Chrome is open, but remote debugging is turned off for this browser instance."
                .to_string(),
            "browser local setup",
        );
    }
    if browser_running == Some(true) {
        return (
            "stale-port",
            "DevToolsActivePort exists, but the recorded CDP port is not reachable. Chrome appears open, but it is not exposing that debug endpoint.".to_string(),
            "Open Chrome with this profile, then run browser connect local",
        );
    }
    (
        "stale-port",
        "DevToolsActivePort exists, but the recorded CDP port is not reachable. Chrome was likely closed or the debug server stopped.".to_string(),
        "Open Chrome with this profile, then run browser connect local",
    )
}

fn known_profile_roots() -> Vec<(&'static str, PathBuf)> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    vec![
        (
            "Google Chrome",
            home.join("Library/Application Support/Google/Chrome"),
        ),
        (
            "Chrome Canary",
            home.join("Library/Application Support/Google/Chrome Canary"),
        ),
        ("Comet", home.join("Library/Application Support/Comet")),
        (
            "Arc",
            home.join("Library/Application Support/Arc/User Data"),
        ),
        (
            "Dia",
            home.join("Library/Application Support/Dia/User Data"),
        ),
        (
            "Microsoft Edge",
            home.join("Library/Application Support/Microsoft Edge"),
        ),
        (
            "Microsoft Edge Beta",
            home.join("Library/Application Support/Microsoft Edge Beta"),
        ),
        (
            "Microsoft Edge Dev",
            home.join("Library/Application Support/Microsoft Edge Dev"),
        ),
        (
            "Microsoft Edge Canary",
            home.join("Library/Application Support/Microsoft Edge Canary"),
        ),
        (
            "Brave",
            home.join("Library/Application Support/BraveSoftware/Brave-Browser"),
        ),
        ("Google Chrome", home.join(".config/google-chrome")),
        ("Chromium", home.join(".config/chromium")),
        ("Chromium", home.join(".config/chromium-browser")),
        ("Microsoft Edge", home.join(".config/microsoft-edge")),
        (
            "Microsoft Edge Beta",
            home.join(".config/microsoft-edge-beta"),
        ),
        (
            "Microsoft Edge Dev",
            home.join(".config/microsoft-edge-dev"),
        ),
        (
            "Chromium",
            home.join(".var/app/org.chromium.Chromium/config/chromium"),
        ),
        (
            "Google Chrome",
            home.join(".var/app/com.google.Chrome/config/google-chrome"),
        ),
        (
            "Brave",
            home.join(".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"),
        ),
        (
            "Microsoft Edge",
            home.join(".var/app/com.microsoft.Edge/config/microsoft-edge"),
        ),
        (
            "Google Chrome",
            home.join("AppData/Local/Google/Chrome/User Data"),
        ),
        (
            "Chrome Canary",
            home.join("AppData/Local/Google/Chrome SxS/User Data"),
        ),
        ("Chromium", home.join("AppData/Local/Chromium/User Data")),
        (
            "Microsoft Edge",
            home.join("AppData/Local/Microsoft/Edge/User Data"),
        ),
        (
            "Microsoft Edge Beta",
            home.join("AppData/Local/Microsoft/Edge Beta/User Data"),
        ),
        (
            "Microsoft Edge Dev",
            home.join("AppData/Local/Microsoft/Edge Dev/User Data"),
        ),
        (
            "Microsoft Edge Canary",
            home.join("AppData/Local/Microsoft/Edge SxS/User Data"),
        ),
        (
            "Brave",
            home.join("AppData/Local/BraveSoftware/Brave-Browser/User Data"),
        ),
    ]
}

fn resolve_ws_from_http(http_url: &str) -> Result<String> {
    let url = format!("{}/json/version", http_url.trim_end_matches('/'));
    let value: Value = Client::new()
        .get(&url)
        .timeout(Duration::from_secs(15))
        .send()
        .with_context(|| format!("GET {url}"))?
        .error_for_status()
        .with_context(|| format!("GET {url} returned error"))?
        .json()
        .with_context(|| format!("parse {url}"))?;
    value
        .get("webSocketDebuggerUrl")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| anyhow!("{url} missing webSocketDebuggerUrl"))
}

fn launch_managed_browser(launch: ManagedLaunch) -> Result<(ManagedBrowser, String)> {
    let port = free_port()?;
    let (profile_path, temp_dir) = match &launch.profile {
        ManagedProfile::Temp => {
            let temp = tempfile::Builder::new()
                .prefix("but-managed-browser.")
                .tempdir()
                .context("create managed browser temp profile")?;
            (temp.path().to_path_buf(), Some(temp))
        }
        ManagedProfile::Path(path) => {
            fs::create_dir_all(path)
                .with_context(|| format!("create managed browser profile {}", path.display()))?;
            (path.clone(), None)
        }
    };
    let mut args = vec![
        "--remote-debugging-address=127.0.0.1".to_string(),
        format!("--remote-debugging-port={port}"),
        format!("--user-data-dir={}", profile_path.display()),
        "--no-first-run".to_string(),
        "--no-default-browser-check".to_string(),
    ];
    if launch.headless {
        args.push("--headless=new".to_string());
        args.push("--window-size=1280,720".to_string());
    } else {
        args.extend([
            "--new-window".to_string(),
            "--window-size=1512,900".to_string(),
        ]);
    }
    args.extend(launch.extra_args.clone());
    args.push("about:blank".to_string());
    let mut child = Command::new(&launch.executable)
        .args(&args)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .with_context(|| format!("launch managed browser {}", launch.executable))?;
    let http_url = format!("http://127.0.0.1:{port}");
    let deadline = Instant::now() + Duration::from_secs(20);
    let mut last_error = None;
    while Instant::now() < deadline {
        if child.try_wait()?.is_some() {
            bail!("managed browser exited before DevTools became available");
        }
        match resolve_ws_from_http(&http_url) {
            Ok(_) => {
                return Ok((
                    ManagedBrowser {
                        child,
                        _profile_dir: temp_dir,
                        launch,
                    },
                    http_url,
                ));
            }
            Err(error) => {
                last_error = Some(format!("{error:#}"));
                thread::sleep(Duration::from_millis(250));
            }
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    bail!(
        "managed browser DevTools did not become available: {}",
        last_error.unwrap_or_else(|| "unknown error".to_string())
    );
}

fn chromium_candidate_paths(headless: bool) -> Vec<String> {
    let mut paths = Vec::new();
    if let Ok(path) = std::env::var("CHROME_PATH") {
        if !path.trim().is_empty() {
            paths.push(path);
        }
    }
    let mut candidates = vec![
        PathBuf::from("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        PathBuf::from("/opt/homebrew/Caskroom/chromium/latest/chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
        PathBuf::from("/usr/bin/chromium"),
        PathBuf::from("/usr/bin/chromium-browser"),
        PathBuf::from("/usr/bin/google-chrome"),
        PathBuf::from("/usr/bin/google-chrome-stable"),
        PathBuf::from("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ];
    if !headless {
        candidates.push(PathBuf::from(
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        ));
    }
    for candidate in candidates {
        if candidate.exists() {
            paths.push(candidate.display().to_string());
        }
    }
    for name in [
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
    ] {
        if let Some(path) = which(name) {
            paths.push(path.display().to_string());
        }
    }
    for candidate in playwright_chromium_candidates() {
        if candidate.exists() {
            paths.push(candidate.display().to_string());
        }
    }
    dedupe_strings(paths)
}

fn dedupe_strings(values: Vec<String>) -> Vec<String> {
    let mut seen = HashSet::new();
    values
        .into_iter()
        .filter(|value| seen.insert(value.clone()))
        .collect()
}

fn playwright_chromium_candidates() -> Vec<PathBuf> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let mut matches = Vec::new();
    for root in [
        home.join("Library/Caches/ms-playwright"),
        home.join(".cache/ms-playwright"),
    ] {
        let Ok(entries) = fs::read_dir(root) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if !path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with("chromium-"))
            {
                continue;
            }
            let mac = path.join(
                "chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            );
            let mac_arm = path.join("chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing");
            let linux = path.join("chrome-linux/chrome");
            for candidate in [mac, mac_arm, linux] {
                if candidate.exists() {
                    matches.push(candidate);
                }
            }
        }
    }
    matches.sort();
    matches.reverse();
    matches
}

fn free_port() -> Result<u16> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    Ok(listener.local_addr()?.port())
}

fn list_local_profiles() -> Result<Value> {
    Ok(json!({
        "status": "ok",
        "source": "rust-local-filesystem",
        "profiles": detect_local_profiles(),
    }))
}

fn inspect_local_profile(profile: &str, domains_only: bool) -> Result<Value> {
    let profiles = detect_local_profiles();
    let selected = match resolve_local_profile(&profiles, profile) {
        Ok(profile) => profile,
        Err(error) => {
            return Ok(json!({
                "status": "failed",
                "profile_ref": profile,
                "error": format!("{error:#}"),
                "available_profiles": profiles,
            }));
        }
    };
    match inspect_local_profile_cookies(&selected) {
        Ok(summary) => Ok(json!({
            "status": "ok",
            "source": "rust-local-cdp",
            "profile": selected,
            "domains_only": domains_only,
            "raw_cookie_values_returned": false,
            "cookie_summary": summary,
        })),
        Err(error) => Ok(json!({
            "status": "failed",
            "source": "rust-local-cdp",
            "profile": selected,
            "raw_cookie_values_returned": false,
            "error": format!("{error:#}"),
        })),
    }
}

fn detect_local_profiles() -> Vec<LocalBrowserProfile> {
    detect_profiles_from_installs(known_local_browser_installs())
}

fn detect_profiles_from_installs(installs: Vec<LocalBrowserInstall>) -> Vec<LocalBrowserProfile> {
    let mut profiles = Vec::new();
    let mut seen = HashSet::new();
    for install in installs {
        if !install.user_data_dir.exists() {
            continue;
        }
        let profile_names = load_profile_names_from_local_state(&install.user_data_dir);
        let Ok(entries) = fs::read_dir(&install.user_data_dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            if !file_type.is_dir() {
                continue;
            }
            let profile_dir = entry.file_name().to_string_lossy().to_string();
            let profile_path = entry.path();
            if !is_valid_local_profile_dir(&profile_path) {
                continue;
            }
            if !seen.insert((install.user_data_dir.clone(), profile_dir.clone())) {
                continue;
            }
            let profile_name = profile_names
                .get(&profile_dir)
                .filter(|name| !name.trim().is_empty())
                .cloned()
                .unwrap_or_else(|| profile_dir.clone());
            profiles.push(LocalBrowserProfile {
                id: format!("{}:{profile_dir}", browser_slug(&install.browser_name)),
                browser_name: install.browser_name.clone(),
                browser_path: install.browser_path.clone(),
                user_data_dir: install.user_data_dir.clone(),
                profile_dir,
                profile_name: profile_name.clone(),
                profile_path,
                display_name: format!("{} - {profile_name}", install.browser_name),
            });
        }
    }
    profiles.sort_by(|a, b| {
        a.browser_name
            .cmp(&b.browser_name)
            .then_with(|| {
                profile_dir_sort_key(&a.profile_dir).cmp(&profile_dir_sort_key(&b.profile_dir))
            })
            .then_with(|| natural_cmp(&a.profile_name, &b.profile_name))
    });
    profiles
}

fn known_local_browser_installs() -> Vec<LocalBrowserInstall> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let program_files = std::env::var_os("ProgramFiles")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("C:/Program Files"));
    let program_files_x86 = std::env::var_os("ProgramFiles(x86)")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("C:/Program Files (x86)"));
    let local_app_data = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join("AppData/Local"));
    let candidates = vec![
        (
            "Google Chrome",
            PathBuf::from("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            home.join("Library/Application Support/Google/Chrome"),
        ),
        (
            "Chrome Canary",
            PathBuf::from(
                "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            ),
            home.join("Library/Application Support/Google/Chrome Canary"),
        ),
        (
            "Brave",
            PathBuf::from("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            home.join("Library/Application Support/BraveSoftware/Brave-Browser"),
        ),
        (
            "Microsoft Edge",
            PathBuf::from("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            home.join("Library/Application Support/Microsoft Edge"),
        ),
        (
            "Chromium",
            PathBuf::from("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            home.join("Library/Application Support/Chromium"),
        ),
        (
            "Arc",
            PathBuf::from("/Applications/Arc.app/Contents/MacOS/Arc"),
            home.join("Library/Application Support/Arc/User Data"),
        ),
        (
            "Dia",
            PathBuf::from("/Applications/Dia.app/Contents/MacOS/Dia"),
            home.join("Library/Application Support/Dia"),
        ),
        (
            "Comet",
            PathBuf::from("/Applications/Comet.app/Contents/MacOS/Comet"),
            home.join("Library/Application Support/Comet"),
        ),
        (
            "Helium",
            PathBuf::from("/Applications/Helium.app/Contents/MacOS/Helium"),
            home.join("Library/Application Support/Helium"),
        ),
        (
            "Sidekick",
            PathBuf::from("/Applications/Sidekick.app/Contents/MacOS/Sidekick"),
            home.join("Library/Application Support/Sidekick"),
        ),
        (
            "Thorium",
            PathBuf::from("/Applications/Thorium.app/Contents/MacOS/Thorium"),
            home.join("Library/Application Support/Thorium"),
        ),
        (
            "SigmaOS",
            PathBuf::from("/Applications/SigmaOS.app/Contents/MacOS/SigmaOS"),
            home.join("Library/Application Support/SigmaOS/User Data"),
        ),
        (
            "Wavebox",
            PathBuf::from("/Applications/Wavebox.app/Contents/MacOS/Wavebox"),
            home.join("Library/Application Support/WaveboxApp"),
        ),
        (
            "Ghost Browser",
            PathBuf::from("/Applications/Ghost Browser.app/Contents/MacOS/Ghost Browser"),
            home.join("Library/Application Support/Ghost Browser"),
        ),
        (
            "Blisk",
            PathBuf::from("/Applications/Blisk.app/Contents/MacOS/Blisk"),
            home.join("Library/Application Support/Blisk"),
        ),
        (
            "Opera",
            PathBuf::from("/Applications/Opera.app/Contents/MacOS/Opera"),
            home.join("Library/Application Support/com.operasoftware.Opera"),
        ),
        (
            "Vivaldi",
            PathBuf::from("/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"),
            home.join("Library/Application Support/Vivaldi"),
        ),
        (
            "Yandex",
            PathBuf::from("/Applications/Yandex.app/Contents/MacOS/Yandex"),
            home.join("Library/Application Support/Yandex/YandexBrowser"),
        ),
        (
            "Iridium",
            PathBuf::from("/Applications/Iridium.app/Contents/MacOS/Iridium"),
            home.join("Library/Application Support/Iridium"),
        ),
        (
            "Google Chrome",
            PathBuf::from("/usr/bin/google-chrome"),
            home.join(".config/google-chrome"),
        ),
        (
            "Google Chrome",
            PathBuf::from("/usr/bin/google-chrome-stable"),
            home.join(".config/google-chrome"),
        ),
        (
            "Brave",
            PathBuf::from("/usr/bin/brave-browser"),
            home.join(".config/BraveSoftware/Brave-Browser"),
        ),
        (
            "Brave",
            PathBuf::from("/usr/bin/brave"),
            home.join(".config/BraveSoftware/Brave-Browser"),
        ),
        (
            "Brave",
            PathBuf::from("/snap/bin/brave"),
            home.join(".config/BraveSoftware/Brave-Browser"),
        ),
        (
            "Microsoft Edge",
            PathBuf::from("/usr/bin/microsoft-edge"),
            home.join(".config/microsoft-edge"),
        ),
        (
            "Microsoft Edge",
            PathBuf::from("/usr/bin/microsoft-edge-stable"),
            home.join(".config/microsoft-edge"),
        ),
        (
            "Chromium",
            PathBuf::from("/usr/bin/chromium"),
            home.join(".config/chromium"),
        ),
        (
            "Chromium",
            PathBuf::from("/usr/bin/chromium-browser"),
            home.join(".config/chromium"),
        ),
        (
            "Chromium",
            PathBuf::from("/snap/bin/chromium"),
            home.join(".config/chromium"),
        ),
        (
            "Opera",
            PathBuf::from("/usr/bin/opera"),
            home.join(".config/opera"),
        ),
        (
            "Opera",
            PathBuf::from("/snap/bin/opera"),
            home.join(".config/opera"),
        ),
        (
            "Vivaldi",
            PathBuf::from("/usr/bin/vivaldi"),
            home.join(".config/vivaldi"),
        ),
        (
            "Vivaldi",
            PathBuf::from("/usr/bin/vivaldi-stable"),
            home.join(".config/vivaldi"),
        ),
        (
            "Vivaldi",
            PathBuf::from("/snap/bin/vivaldi"),
            home.join(".config/vivaldi"),
        ),
        (
            "Yandex",
            PathBuf::from("/usr/bin/yandex-browser"),
            home.join(".config/yandex-browser"),
        ),
        (
            "Yandex",
            PathBuf::from("/usr/bin/yandex-browser-stable"),
            home.join(".config/yandex-browser"),
        ),
        (
            "Iridium",
            PathBuf::from("/usr/bin/iridium-browser"),
            home.join(".config/iridium"),
        ),
        (
            "Ungoogled Chromium",
            PathBuf::from("/usr/bin/ungoogled-chromium"),
            home.join(".config/chromium"),
        ),
        (
            "Thorium",
            PathBuf::from("/usr/bin/thorium-browser"),
            home.join(".config/thorium"),
        ),
        (
            "Sidekick",
            home.join(".local/share/sidekick/sidekick"),
            home.join(".config/Sidekick"),
        ),
        (
            "Wavebox",
            PathBuf::from("/usr/bin/wavebox"),
            home.join(".config/Wavebox"),
        ),
        (
            "Google Chrome",
            program_files.join("Google/Chrome/Application/chrome.exe"),
            local_app_data.join("Google/Chrome/User Data"),
        ),
        (
            "Google Chrome",
            program_files_x86.join("Google/Chrome/Application/chrome.exe"),
            local_app_data.join("Google/Chrome/User Data"),
        ),
        (
            "Google Chrome",
            local_app_data.join("Google/Chrome/Application/chrome.exe"),
            local_app_data.join("Google/Chrome/User Data"),
        ),
        (
            "Brave",
            program_files.join("BraveSoftware/Brave-Browser/Application/brave.exe"),
            local_app_data.join("BraveSoftware/Brave-Browser/User Data"),
        ),
        (
            "Brave",
            local_app_data.join("BraveSoftware/Brave-Browser/Application/brave.exe"),
            local_app_data.join("BraveSoftware/Brave-Browser/User Data"),
        ),
        (
            "Microsoft Edge",
            program_files.join("Microsoft/Edge/Application/msedge.exe"),
            local_app_data.join("Microsoft/Edge/User Data"),
        ),
        (
            "Microsoft Edge",
            program_files_x86.join("Microsoft/Edge/Application/msedge.exe"),
            local_app_data.join("Microsoft/Edge/User Data"),
        ),
        (
            "Chromium",
            local_app_data.join("Chromium/Application/chrome.exe"),
            local_app_data.join("Chromium/User Data"),
        ),
        (
            "Opera",
            local_app_data.join("Programs/Opera/opera.exe"),
            home.join("AppData/Roaming/Opera Software/Opera Stable"),
        ),
        (
            "Opera",
            program_files.join("Opera/opera.exe"),
            home.join("AppData/Roaming/Opera Software/Opera Stable"),
        ),
        (
            "Vivaldi",
            local_app_data.join("Vivaldi/Application/vivaldi.exe"),
            local_app_data.join("Vivaldi/User Data"),
        ),
        (
            "Vivaldi",
            program_files.join("Vivaldi/Application/vivaldi.exe"),
            local_app_data.join("Vivaldi/User Data"),
        ),
        (
            "Yandex",
            local_app_data.join("Yandex/YandexBrowser/Application/browser.exe"),
            local_app_data.join("Yandex/YandexBrowser/User Data"),
        ),
        (
            "Iridium",
            local_app_data.join("Iridium/Application/iridium.exe"),
            local_app_data.join("Iridium/User Data"),
        ),
        (
            "Sidekick",
            local_app_data.join("Sidekick/Application/sidekick.exe"),
            local_app_data.join("Sidekick/User Data"),
        ),
        (
            "Thorium",
            local_app_data.join("Thorium/Application/thorium.exe"),
            local_app_data.join("Thorium/User Data"),
        ),
        (
            "Wavebox",
            local_app_data.join("WaveboxApp/Application/wavebox.exe"),
            local_app_data.join("WaveboxApp/User Data"),
        ),
        (
            "Blisk",
            local_app_data.join("Blisk/Application/blisk.exe"),
            local_app_data.join("Blisk/User Data"),
        ),
    ];
    let mut installs: Vec<LocalBrowserInstall> = Vec::new();
    let mut seen: HashMap<(String, PathBuf), usize> = HashMap::new();
    for (browser_name, browser_path, user_data_dir) in candidates {
        if !browser_path.exists() && !user_data_dir.exists() {
            continue;
        }
        let key = (browser_name.to_string(), user_data_dir.clone());
        let candidate = LocalBrowserInstall {
            browser_name: browser_name.to_string(),
            browser_path,
            user_data_dir,
        };
        if let Some(index) = seen.get(&key).copied() {
            if !installs[index].browser_path.exists() && candidate.browser_path.exists() {
                installs[index] = candidate;
            }
        } else {
            seen.insert(key, installs.len());
            installs.push(candidate);
        }
    }
    installs
}

fn load_profile_names_from_local_state(user_data_dir: &Path) -> HashMap<String, String> {
    let Ok(raw) = fs::read_to_string(user_data_dir.join("Local State")) else {
        return HashMap::new();
    };
    let Ok(value) = serde_json::from_str::<Value>(&raw) else {
        return HashMap::new();
    };
    value
        .pointer("/profile/info_cache")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
        .filter_map(|(profile_dir, info)| {
            info.get("name")
                .and_then(Value::as_str)
                .filter(|name| !name.trim().is_empty())
                .map(|name| (profile_dir.clone(), name.to_string()))
        })
        .collect()
}

fn remote_debugging_user_enabled(user_data_dir: &Path) -> Option<bool> {
    let raw = fs::read_to_string(user_data_dir.join("Local State")).ok()?;
    let value = serde_json::from_str::<Value>(&raw).ok()?;
    remote_debugging_user_enabled_from_local_state(&value)
}

fn remote_debugging_user_enabled_from_local_state(value: &Value) -> Option<bool> {
    value
        .pointer("/devtools/remote_debugging/user-enabled")
        .and_then(Value::as_bool)
}

#[cfg(unix)]
fn browser_process_running(_browser_name: &str, browser_path: &Path) -> Option<bool> {
    let output = Command::new("ps")
        .args(["-axo", "pid=,comm=,args="])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let browser_path = browser_path.to_string_lossy();
    Some(
        !browser_path.is_empty()
            && text
                .lines()
                .any(|line| line.contains(browser_path.as_ref())),
    )
}

#[cfg(windows)]
fn browser_process_running(_browser_name: &str, browser_path: &Path) -> Option<bool> {
    let output = Command::new("tasklist")
        .args(["/FO", "CSV"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).to_ascii_lowercase();
    let executable = browser_path
        .file_name()?
        .to_string_lossy()
        .to_ascii_lowercase();
    Some(!executable.is_empty() && text.contains(&executable))
}

#[cfg(not(any(unix, windows)))]
fn browser_process_running(_browser_name: &str, _browser_path: &Path) -> Option<bool> {
    None
}

fn is_valid_local_profile_dir(path: &Path) -> bool {
    ["Preferences", "Cookies", "History", "Network/Cookies"]
        .iter()
        .any(|relative| path.join(relative).exists())
}

fn browser_slug(name: &str) -> String {
    let mut slug = String::new();
    let mut last_dash = false;
    for ch in name.chars().flat_map(char::to_lowercase) {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            last_dash = false;
        } else if !last_dash {
            slug.push('-');
            last_dash = true;
        }
    }
    slug.trim_matches('-').to_string()
}

fn profile_dir_sort_key(profile_dir: &str) -> (u8, String) {
    if profile_dir == "Default" {
        (0, String::new())
    } else {
        (1, profile_dir.to_string())
    }
}

fn natural_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    let a_bytes = a.as_bytes();
    let b_bytes = b.as_bytes();
    let mut ia = 0;
    let mut ib = 0;
    while ia < a_bytes.len() && ib < b_bytes.len() {
        if a_bytes[ia].is_ascii_digit() && b_bytes[ib].is_ascii_digit() {
            let (na, next_a) = parse_ascii_number(a_bytes, ia);
            let (nb, next_b) = parse_ascii_number(b_bytes, ib);
            match na.cmp(&nb) {
                std::cmp::Ordering::Equal => {
                    ia = next_a;
                    ib = next_b;
                }
                other => return other,
            }
        } else {
            match a_bytes[ia].cmp(&b_bytes[ib]) {
                std::cmp::Ordering::Equal => {
                    ia += 1;
                    ib += 1;
                }
                other => return other,
            }
        }
    }
    a_bytes.len().cmp(&b_bytes.len())
}

fn parse_ascii_number(bytes: &[u8], mut index: usize) -> (u64, usize) {
    let mut number = 0_u64;
    while index < bytes.len() && bytes[index].is_ascii_digit() {
        number = number
            .saturating_mul(10)
            .saturating_add((bytes[index] - b'0') as u64);
        index += 1;
    }
    (number, index)
}

fn resolve_local_profile(
    profiles: &[LocalBrowserProfile],
    profile_ref: &str,
) -> Result<LocalBrowserProfile> {
    if let Some(profile) = profiles.iter().find(|profile| profile.id == profile_ref) {
        return Ok(profile.clone());
    }
    let matches = profiles
        .iter()
        .filter(|profile| {
            profile.profile_name == profile_ref
                || profile.profile_dir == profile_ref
                || profile.display_name == profile_ref
        })
        .cloned()
        .collect::<Vec<_>>();
    match matches.as_slice() {
        [profile] => Ok(profile.clone()),
        [] => {
            bail!("no local profile matched {profile_ref:?}; run `browser local profiles --json`")
        }
        _ => bail!("multiple local profiles matched {profile_ref:?}; pass the exact profile id"),
    }
}

fn open_local_profile_url(profile: &LocalBrowserProfile, url: &str) -> Result<()> {
    let mut command = Command::new(&profile.browser_path);
    command
        .arg(format!("--profile-directory={}", profile.profile_dir))
        .arg(url)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    command
        .spawn()
        .with_context(|| format!("open {} with {}", url, profile.display_name))?;
    Ok(())
}

fn inspect_local_profile_cookies(profile: &LocalBrowserProfile) -> Result<Value> {
    let temp = tempfile::Builder::new()
        .prefix("but-profile-inspect.")
        .tempdir()
        .context("create temp profile inspection dir")?;
    copy_local_state_for_profile(&profile.user_data_dir, temp.path())?;
    copy_profile_dir_for_inspection(&profile.profile_path, &temp.path().join("Default"))?;
    let launch = ManagedLaunch {
        executable: profile.browser_path.display().to_string(),
        profile: ManagedProfile::Path(temp.path().to_path_buf()),
        headless: true,
        extra_args: vec!["--no-startup-window".to_string()],
    };
    let (mut managed, http_url) = launch_managed_browser(launch)?;
    let result = (|| -> Result<Value> {
        let ws_url = resolve_ws_from_http(&http_url)?;
        let mut connection = CdpConnection::connect(&ws_url)?;
        let cookies = connection.cdp_storage_cookies()?;
        Ok(cookie_domain_summary(&cookies))
    })();
    let _ = managed.child.kill();
    let _ = managed.child.wait();
    result
}

fn copy_local_state_for_profile(src_user_data_dir: &Path, dst_user_data_dir: &Path) -> Result<()> {
    fs::create_dir_all(dst_user_data_dir)
        .with_context(|| format!("create temp user data dir {}", dst_user_data_dir.display()))?;
    let src = src_user_data_dir.join("Local State");
    if src.exists() {
        let _ = fs::copy(&src, dst_user_data_dir.join("Local State"));
    }
    Ok(())
}

fn copy_profile_dir_for_inspection(src: &Path, dst: &Path) -> Result<()> {
    const SKIP_DIRS: &[&str] = &[
        "Service Worker",
        "Extensions",
        "IndexedDB",
        "Local Extension Settings",
        "Local Storage",
        "GPUCache",
        "Shared Dictionary",
        "SharedCache",
    ];
    const SKIP_FILES: &[&str] = &[
        "SingletonLock",
        "SingletonSocket",
        "SingletonCookie",
        "lockfile",
        "RunningChromeVersion",
        "History",
    ];
    fn copy_inner(src: &Path, dst: &Path) -> Result<()> {
        fs::create_dir_all(dst).with_context(|| format!("create {}", dst.display()))?;
        let entries = fs::read_dir(src).with_context(|| format!("read {}", src.display()))?;
        for entry in entries.flatten() {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_string();
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            if file_type.is_dir() {
                if SKIP_DIRS.contains(&name.as_str()) {
                    continue;
                }
                let _ = copy_inner(&path, &dst.join(&name));
            } else if file_type.is_file() {
                if SKIP_FILES.contains(&name.as_str()) {
                    continue;
                }
                let _ = fs::copy(&path, dst.join(&name));
            }
        }
        Ok(())
    }
    copy_inner(src, dst)
}

fn cookie_domain_summary(cookies: &[Value]) -> Value {
    #[derive(Default)]
    struct DomainStats {
        count: usize,
        session_count: usize,
        persistent_count: usize,
        earliest_expiry: Option<i64>,
        latest_expiry: Option<i64>,
    }

    let mut domains = HashMap::<String, DomainStats>::new();
    for cookie in cookies {
        let Some(domain) = cookie.get("domain").and_then(Value::as_str) else {
            continue;
        };
        let domain = domain.trim_start_matches('.').to_string();
        if domain.is_empty() {
            continue;
        }
        let stats = domains.entry(domain).or_default();
        stats.count += 1;
        let session = cookie
            .get("session")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if session {
            stats.session_count += 1;
        } else {
            stats.persistent_count += 1;
            if let Some(expiry) = cookie.get("expires").and_then(Value::as_f64) {
                if expiry > 0.0 {
                    let expiry = expiry as i64;
                    stats.earliest_expiry = Some(
                        stats
                            .earliest_expiry
                            .map_or(expiry, |current| current.min(expiry)),
                    );
                    stats.latest_expiry = Some(
                        stats
                            .latest_expiry
                            .map_or(expiry, |current| current.max(expiry)),
                    );
                }
            }
        }
    }
    let mut rows = domains
        .into_iter()
        .map(|(domain, stats)| {
            json!({
                "domain": domain,
                "count": stats.count,
                "session_count": stats.session_count,
                "persistent_count": stats.persistent_count,
                "earliest_expiry": stats.earliest_expiry,
                "latest_expiry": stats.latest_expiry,
            })
        })
        .collect::<Vec<_>>();
    rows.sort_by(|a, b| {
        b.get("count")
            .and_then(Value::as_u64)
            .cmp(&a.get("count").and_then(Value::as_u64))
            .then_with(|| {
                a.get("domain")
                    .and_then(Value::as_str)
                    .cmp(&b.get("domain").and_then(Value::as_str))
            })
    });
    Value::Array(rows)
}

fn list_cloud_profiles() -> Result<Value> {
    let first = browser_use_api("/profiles?pageSize=100&pageNumber=1", "GET", None)?;
    let items = first
        .get("items")
        .and_then(Value::as_array)
        .cloned()
        .or_else(|| first.as_array().cloned())
        .unwrap_or_default();
    let mut profiles = Vec::new();
    for profile in items {
        let Some(id) = profile.get("id").and_then(Value::as_str) else {
            continue;
        };
        let detail = browser_use_api(&format!("/profiles/{id}"), "GET", None).unwrap_or(profile);
        profiles.push(json!({
            "id": detail.get("id"),
            "name": detail.get("name"),
            "userId": detail.get("userId"),
            "cookieDomains": detail.get("cookieDomains").cloned().unwrap_or(Value::Array(Vec::new())),
            "lastUsedAt": detail.get("lastUsedAt"),
        }));
    }
    Ok(json!({ "status": "ok", "profiles": profiles }))
}

fn resolve_cloud_profile_name(profile_name: &str) -> Result<String> {
    let profiles = list_cloud_profiles()?;
    let matches = profiles
        .get("profiles")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|profile| profile.get("name").and_then(Value::as_str) == Some(profile_name))
        .collect::<Vec<_>>();
    match matches.as_slice() {
        [profile] => profile
            .get("id")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned)
            .ok_or_else(|| anyhow!("cloud profile {profile_name:?} missing id")),
        [] => {
            bail!("no cloud profile named {profile_name:?}; run `browser remote profiles --json`")
        }
        _ => bail!("multiple cloud profiles named {profile_name:?}; pass --profile-id <uuid>"),
    }
}

fn browser_use_api(path: &str, method: &str, body: Option<Value>) -> Result<Value> {
    let key = std::env::var("BROWSER_USE_API_KEY")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| anyhow!("BROWSER_USE_API_KEY missing"))?;
    let client = Client::new();
    let url = format!("{BU_API}{path}");
    let request = match method {
        "GET" => client.get(&url),
        "POST" => client.post(&url),
        "PATCH" => client.patch(&url),
        other => bail!("unsupported Browser Use API method: {other}"),
    }
    .header("X-Browser-Use-API-Key", key)
    .header("Content-Type", "application/json")
    .timeout(Duration::from_secs(60));
    let request = if let Some(body) = body {
        request.json(&body)
    } else {
        request
    };
    let response = request
        .send()
        .with_context(|| format!("{method} {url}"))?
        .error_for_status()
        .with_context(|| format!("{method} {url} returned error"))?;
    Ok(response.json().unwrap_or_else(|_| json!({})))
}

fn stop_cloud_browser(browser_id: &str) -> Result<Value> {
    browser_use_api(
        &format!("/browsers/{browser_id}"),
        "PATCH",
        Some(json!({ "action": "stop" })),
    )
}

fn is_real_page_target(target: &Value) -> bool {
    if target.get("type").and_then(Value::as_str) != Some("page") {
        return false;
    }
    let url = target.get("url").and_then(Value::as_str).unwrap_or("");
    !matches!(url, "" | "about:blank")
        || target
            .get("title")
            .and_then(Value::as_str)
            .is_some_and(|title| !title.trim().is_empty())
}

fn browser_help() -> &'static str {
    include_str!("../../../prompts/browser-tool-description.md").trim()
}

fn render_doctor(value: &Value) -> String {
    let mut lines = vec![format!(
        "browser doctor: {}",
        value
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
    )];
    for check in value
        .get("checks")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let ok = if check.get("ok").and_then(Value::as_bool).unwrap_or(false) {
            "ok"
        } else {
            "needs action"
        };
        let name = check.get("name").and_then(Value::as_str).unwrap_or("check");
        lines.push(format!("- {name}: {ok}"));
        if let Some(next) = check.get("next_step").and_then(Value::as_str) {
            if !next.is_empty() {
                lines.push(format!("  next: {next}"));
            }
        }
    }
    lines.join("\n")
}

fn shell_words(input: &str) -> Result<Vec<String>> {
    let mut words = Vec::new();
    let mut current = String::new();
    let mut chars = input.chars().peekable();
    let mut quote = None;
    while let Some(ch) = chars.next() {
        match (quote, ch) {
            (Some(q), c) if c == q => quote = None,
            (Some(_), '\\') => {
                if let Some(next) = chars.next() {
                    current.push(next);
                }
            }
            (Some(_), c) => current.push(c),
            (None, '"' | '\'') => quote = Some(ch),
            (None, c) if c.is_whitespace() => {
                if !current.is_empty() {
                    words.push(std::mem::take(&mut current));
                }
            }
            (None, '\\') => {
                if let Some(next) = chars.next() {
                    current.push(next);
                }
            }
            (None, c) => current.push(c),
        }
    }
    if quote.is_some() {
        bail!("unterminated quote in browser command");
    }
    if !current.is_empty() {
        words.push(current);
    }
    Ok(words)
}

fn option_value(argv: &[String], name: &str) -> Option<String> {
    argv.windows(2)
        .find_map(|pair| (pair[0] == name).then(|| pair[1].clone()))
}

fn option_values(argv: &[String], name: &str) -> Vec<String> {
    argv.windows(2)
        .filter_map(|pair| (pair[0] == name).then(|| pair[1].clone()))
        .collect()
}

fn has_flag(argv: &[String], name: &str) -> bool {
    argv.iter().any(|arg| arg == name)
}

fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|path| path.exists())
}

fn tcp_port_open(host: &str, port: u16) -> bool {
    if port == 0 {
        return false;
    }
    TcpStream::connect_timeout(
        &format!("{host}:{port}").parse().expect("valid socket addr"),
        Duration::from_millis(150),
    )
    .is_ok()
}

fn unix_time_ms() -> u128 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn redact_ws_url(url: &str) -> String {
    if let Some((prefix, _)) = url.split_once('?') {
        format!("{prefix}?...")
    } else {
        url.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::MutexGuard;

    static ENV_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

    struct EnvRestore {
        _guard: MutexGuard<'static, ()>,
        values: Vec<(&'static str, Option<String>)>,
    }

    impl EnvRestore {
        fn set(vars: &[(&'static str, &str)]) -> Self {
            let guard = ENV_LOCK
                .get_or_init(|| Mutex::new(()))
                .lock()
                .expect("env lock poisoned");
            let values = vars
                .iter()
                .map(|(key, _)| (*key, std::env::var(key).ok()))
                .collect::<Vec<_>>();
            for (key, value) in vars {
                std::env::set_var(key, value);
            }
            Self {
                _guard: guard,
                values,
            }
        }
    }

    impl Drop for EnvRestore {
        fn drop(&mut self) {
            for (key, value) in self.values.drain(..) {
                if let Some(value) = value {
                    std::env::set_var(key, value);
                } else {
                    std::env::remove_var(key);
                }
            }
        }
    }

    #[test]
    fn shell_words_accepts_browser_prefix_and_quotes() {
        assert_eq!(
            shell_words("browser remote start --profile-name 'Work Profile'").unwrap(),
            vec![
                "browser",
                "remote",
                "start",
                "--profile-name",
                "Work Profile"
            ]
        );
    }

    #[test]
    fn status_shape_contains_llm_recovery_fields() {
        let session = BrowserSession::default();
        let status = session.status_json();
        assert_eq!(status["mode"], "none");
        assert_eq!(status["connection"], "not-configured");
        assert_eq!(status["next_step"], "browser connect local");
        assert!(status.get("safety").is_some());
        assert!(status.get("connection_generation").is_some());
    }

    #[test]
    fn local_setup_waits_for_user_confirmation_before_retry() {
        let status = local_setup_user_action_response(false, None, None);
        assert_eq!(status["status"], "needs-user-action");
        assert!(status["next_step"]
            .as_str()
            .unwrap()
            .contains("Wait for user confirmation"));
        assert!(status["instructions"][2]
            .as_str()
            .unwrap()
            .contains("Do not retry until the user confirms"));
    }

    #[test]
    fn stale_session_errors_are_classified_for_reattach() {
        let message = r#"CDP Runtime.evaluate failed: {"code":-32001,"message":"Session with given id not found."}"#;
        assert!(is_stale_session_error(message));
        assert_eq!(classify_browser_error(message), "session-gone");
    }

    #[test]
    fn cdp_read_timeouts_are_not_classified_as_dropped_websockets() {
        let message =
            "read CDP Runtime.evaluate: IO error: Resource temporarily unavailable (os error 35)";
        assert_eq!(classify_browser_error(message), "cdp-read-timeout");
        assert!(!should_drop_browser_connection(classify_browser_error(
            message
        )));
    }

    #[test]
    fn cdp_read_timeout_diagnosis_keeps_page_reusable() {
        let diagnosis = browser_issue_diagnosis("cdp-read-timeout", true, true, None);
        assert!(diagnosis.browser_usable);
        assert!(diagnosis.page_usable);
        assert!(diagnosis.summary.contains("same page"));
        assert!(diagnosis.next_step.contains("smaller browser_execute job"));
    }

    #[test]
    fn runtime_evaluate_script_errors_are_not_websocket_drops() {
        let message = r#"RuntimeError: CDP Runtime.evaluate failed: {"code":-32000,"message":"Exception thrown"}"#;
        assert_eq!(classify_browser_job_failure(message), "browser-job-error");
    }

    #[test]
    fn terminal_websocket_errors_still_drop_browser_connection() {
        assert!(should_drop_browser_connection(classify_browser_error(
            "read CDP Target.getTargets: IO error: Connection reset by peer"
        )));
        assert!(should_drop_browser_connection(classify_browser_error(
            "CDP websocket closed: None"
        )));
    }

    #[test]
    fn browser_events_are_transition_based_not_heartbeats() {
        let mut session = BrowserSession::default();
        assert!(session.browser_events().is_empty());

        session.mode = BrowserMode::Local;
        session.live_url = Some("https://live.browser-use.com/watch".to_string());
        session.endpoint = Some(Endpoint {
            kind: "local".to_string(),
            http_url: Some("http://127.0.0.1:9222".to_string()),
            ws_url: "ws://127.0.0.1:9222/devtools/browser/example".to_string(),
            candidate_id: Some("local-1".to_string()),
        });

        let first = session.browser_events();
        assert_eq!(first.len(), 2);
        assert_eq!(first[0]["type"], "browser.disconnected");
        assert_eq!(first[1]["type"], "browser.live_url");
        assert_eq!(
            first[1]["payload"]["live_url"],
            "https://live.browser-use.com/watch"
        );
        assert!(session.browser_events().is_empty());

        let connected = json!({
            "status": "connected",
            "target_id": "target-1",
            "session_id": "session-1",
            "generation": 1,
        });
        session.last_emitted_browser_payload = None;
        assert_eq!(session.browser_event_type(&connected), "browser.connected");
        session.last_emitted_browser_payload = Some(connected.clone());
        assert_eq!(
            session.browser_event_type(&json!({
                "status": "connected",
                "target_id": "target-2",
                "session_id": "session-1",
                "generation": 1,
            })),
            "browser.target_changed"
        );
        assert_eq!(
            session.browser_event_type(&json!({
                "status": "connected",
                "target_id": "target-1",
                "session_id": "session-2",
                "generation": 2,
            })),
            "browser.reconnected"
        );
    }

    #[test]
    fn browser_help_is_cli_like() {
        let help = browser_help();
        assert!(help.contains("browser status --json"));
        assert!(help.contains("browser connect local"));
        assert!(help.contains("browser domain skills --domain"));
        assert!(help.contains("browser_execute"));
        assert!(help
            .to_ascii_lowercase()
            .contains("remote start means start and connect"));
    }

    #[test]
    fn connection_skill_uses_rust_owned_setup_contract() {
        let skill = include_str!("../../../prompts/interaction-skills/connection.md");
        assert!(skill.contains("Browser setup is Rust-owned"));
        assert!(skill.contains("Do not connect from inside `browser_execute`"));
        assert!(!skill.contains("Just call"));
        assert!(!skill.contains("await session.connect()"));
        assert!(!skill.contains("detectBrowsers"));
    }

    #[test]
    fn browser_executor_has_connect_compatibility_without_setup_ownership() {
        assert!(BROWSER_EXECUTOR_JS.contains("async connect"));
        assert!(BROWSER_EXECUTOR_JS.contains("already-connected"));
        assert!(BROWSER_EXECUTOR_JS.contains("Rust owns browser setup"));
        assert!(BROWSER_EXECUTOR_JS.contains("session.connect(options) is not supported"));
    }

    #[test]
    fn browser_domain_skills_command_lists_matching_files() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("domain-skills");
        fs::create_dir_all(root.join("parity")).unwrap();
        fs::write(
            root.join("parity/scraping.md"),
            "# Parity\n\nUse the stable API before DOM scraping.",
        )
        .unwrap();
        let root_text = root.display().to_string();
        let _env = EnvRestore::set(&[
            ("BH_DOMAIN_SKILLS_ROOT", &root_text),
            ("BH_DOMAIN_SKILLS", "1"),
        ]);

        let output = run_browser_command(
            "domain-skills",
            temp.path(),
            temp.path(),
            "browser domain skills --domain https://www.parity.test/path --include-content --json",
        )
        .unwrap();

        assert_eq!(output.content["status"], "ok");
        assert_eq!(output.content["matches"][0]["site"], "parity");
        assert_eq!(
            output.content["matches"][0]["files"][0]["name"],
            "scraping.md"
        );
        assert!(output.content["matches"][0]["files"][0]["content"]
            .as_str()
            .unwrap()
            .contains("stable API"));
    }

    #[test]
    fn doctor_is_read_only_and_points_to_explicit_next_steps() {
        let temp = tempfile::tempdir().unwrap();
        let output =
            run_browser_command("doctor-empty", temp.path(), temp.path(), "browser doctor")
                .unwrap();
        let text = output.content.as_str().unwrap();
        assert!(text.contains("browser doctor"));
        assert!(text.contains("next:"));
    }

    #[test]
    fn recovery_without_connection_fails_without_side_effects() {
        let temp = tempfile::tempdir().unwrap();
        let error = run_browser_command(
            "recover-empty",
            temp.path(),
            temp.path(),
            "browser recover reconnect-websocket",
        )
        .unwrap_err();
        assert!(format!("{error:#}").contains("no browser endpoint is configured"));
    }

    #[test]
    fn local_profiles_command_uses_native_rust_detector() {
        let temp = tempfile::tempdir().unwrap();
        let output = run_browser_command(
            "profiles-list",
            temp.path(),
            temp.path(),
            "browser local profiles --json",
        )
        .unwrap();
        assert_eq!(output.content["source"], "rust-local-filesystem");
        assert!(output.content["profiles"].is_array());
        assert!(!output.content.to_string().contains("profile-use"));
    }

    #[test]
    fn stale_devtools_active_port_is_not_connectable() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(
            temp.path().join("DevToolsActivePort"),
            "9\n/devtools/browser/stale\n",
        )
        .unwrap();
        let candidates =
            local_candidates_from_roots(vec![("Test Chrome", temp.path().to_path_buf())], &[]);
        assert_eq!(candidates.len(), 1);
        assert!(!candidates[0].connectable);
        assert_eq!(candidates[0].state, "stale-port");
        assert!(candidates[0].stale);
        assert!(candidates[0]
            .reason
            .as_deref()
            .unwrap()
            .contains("DevToolsActivePort"));
    }

    #[test]
    fn remote_debugging_flag_reads_chrome_local_state() {
        let value = json!({
            "devtools": {
                "remote_debugging": {
                    "user-enabled": false
                }
            }
        });
        assert_eq!(
            remote_debugging_user_enabled_from_local_state(&value),
            Some(false)
        );
    }

    #[test]
    fn running_browser_with_disabled_cdp_gets_specific_local_state() {
        let (state, reason, next_step) =
            local_disconnected_candidate_details(Some(true), Some(false));
        assert_eq!(state, "cdp-disabled");
        assert!(reason.contains("remote debugging is turned off"));
        assert_eq!(next_step, "browser local setup");
    }

    #[test]
    fn local_profiles_inspect_missing_profile_never_mentions_external_cli() {
        let temp = tempfile::tempdir().unwrap();
        let output = run_browser_command(
            "profiles-inspect-missing",
            temp.path(),
            temp.path(),
            "browser local profiles inspect 'missing profile' --domains-only",
        )
        .unwrap();
        assert_eq!(output.content["status"], "failed");
        assert!(output.content.get("available_profiles").is_some());
        assert!(!output.content.to_string().contains("profile-use"));
    }

    #[test]
    fn local_profile_detection_reads_local_state_names_and_stable_ids() {
        let temp = tempfile::tempdir().unwrap();
        let user_data_dir = temp.path().join("Chrome");
        fs::create_dir_all(user_data_dir.join("Default")).unwrap();
        fs::create_dir_all(user_data_dir.join("Profile 10")).unwrap();
        fs::write(user_data_dir.join("Default/Preferences"), "{}").unwrap();
        fs::write(user_data_dir.join("Profile 10/Preferences"), "{}").unwrap();
        fs::write(
            user_data_dir.join("Local State"),
            r#"{
              "profile": {
                "info_cache": {
                  "Default": { "name": "Personal" },
                  "Profile 10": { "name": "Work" }
                }
              }
            }"#,
        )
        .unwrap();
        let profiles = detect_profiles_from_installs(vec![LocalBrowserInstall {
            browser_name: "Google Chrome".to_string(),
            browser_path: temp.path().join("Chrome.app"),
            user_data_dir: user_data_dir.clone(),
        }]);
        assert_eq!(profiles.len(), 2);
        assert_eq!(profiles[0].id, "google-chrome:Default");
        assert_eq!(profiles[0].profile_name, "Personal");
        assert_eq!(profiles[1].id, "google-chrome:Profile 10");
        assert_eq!(profiles[1].display_name, "Google Chrome - Work");
    }

    #[test]
    fn local_profile_resolution_requires_exact_id_when_names_collide() {
        let profiles = vec![
            LocalBrowserProfile {
                id: "chrome:Default".to_string(),
                browser_name: "Chrome".to_string(),
                browser_path: PathBuf::from("/chrome"),
                user_data_dir: PathBuf::from("/profiles/chrome"),
                profile_dir: "Default".to_string(),
                profile_name: "Work".to_string(),
                profile_path: PathBuf::from("/profiles/chrome/Default"),
                display_name: "Chrome - Work".to_string(),
            },
            LocalBrowserProfile {
                id: "brave:Default".to_string(),
                browser_name: "Brave".to_string(),
                browser_path: PathBuf::from("/brave"),
                user_data_dir: PathBuf::from("/profiles/brave"),
                profile_dir: "Default".to_string(),
                profile_name: "Work".to_string(),
                profile_path: PathBuf::from("/profiles/brave/Default"),
                display_name: "Brave - Work".to_string(),
            },
        ];
        assert!(resolve_local_profile(&profiles, "Work")
            .unwrap_err()
            .to_string()
            .contains("multiple local profiles"));
        assert_eq!(
            resolve_local_profile(&profiles, "brave:Default")
                .unwrap()
                .browser_name,
            "Brave"
        );
    }

    #[test]
    fn profile_inspection_copy_skips_heavy_and_lock_files() {
        let temp = tempfile::tempdir().unwrap();
        let src = temp.path().join("src");
        let dst = temp.path().join("dst");
        fs::create_dir_all(src.join("Network")).unwrap();
        fs::create_dir_all(src.join("IndexedDB")).unwrap();
        fs::write(src.join("Preferences"), "{}").unwrap();
        fs::write(src.join("History"), "skip").unwrap();
        fs::write(src.join("SingletonLock"), "skip").unwrap();
        fs::write(src.join("Network/Cookies"), "copy").unwrap();
        fs::write(src.join("IndexedDB/data"), "skip").unwrap();
        copy_profile_dir_for_inspection(&src, &dst).unwrap();
        assert!(dst.join("Preferences").exists());
        assert!(dst.join("Network/Cookies").exists());
        assert!(!dst.join("History").exists());
        assert!(!dst.join("SingletonLock").exists());
        assert!(!dst.join("IndexedDB").exists());
    }

    #[test]
    fn cookie_domain_summary_never_returns_cookie_values() {
        let cookies = vec![
            json!({
                "name": "sid",
                "value": "secret",
                "domain": ".gusto.com",
                "session": false,
                "expires": 2000.0
            }),
            json!({
                "name": "tmp",
                "value": "secret2",
                "domain": "gusto.com",
                "session": true
            }),
            json!({
                "name": "other",
                "value": "secret3",
                "domain": "example.com",
                "session": false,
                "expires": 3000.0
            }),
        ];
        let summary = cookie_domain_summary(&cookies);
        let text = serde_json::to_string(&summary).unwrap();
        assert!(!text.contains("secret"));
        assert_eq!(summary[0]["domain"], "gusto.com");
        assert_eq!(summary[0]["count"], 2);
        assert_eq!(summary[0]["session_count"], 1);
        assert_eq!(summary[0]["persistent_count"], 1);
    }

    #[test]
    #[ignore = "launches a real local Chromium-family browser for end-to-end smoke verification"]
    fn managed_browser_smoke_navigates_and_captures_screenshot() {
        if chromium_candidate_paths(true).is_empty() {
            eprintln!("skipping managed browser smoke: no Chromium-family browser found");
            return;
        }

        let temp = tempfile::tempdir().unwrap();
        let artifacts = temp.path().join("artifacts");
        let session_id = "managed-smoke";

        let connect = run_browser_command(
            session_id,
            temp.path(),
            &artifacts,
            "browser connect managed --headless",
        )
        .unwrap();
        assert_eq!(connect.content["status"], "connected");

        let job = execute_browser_js(
            session_id,
            temp.path(),
            &artifacts,
            r##"
const created = await cdp("Target.createTarget", { url: "about:blank" });
if (!created.targetId) throw new Error("missing created target id");
await session.use(created.targetId);
const targets = await listPageTargets();
if (!targets.every(target => target.targetId)) throw new Error("target list missing ids");
await session.Page.navigate({ url: "about:blank" });
await session.waitFor("Page.loadEventFired", undefined, 5_000).catch(() => null);
await session.Runtime.evaluate({ expression: `
(() => {
  document.title = "Browser Smoke";
  document.body.style.margin = "0";
  document.body.innerHTML = '<canvas id="ok" width="1280" height="900"></canvas>';
  const canvas = document.querySelector("#ok");
  canvas.style.display = "block";
  canvas.style.width = "1280px";
  canvas.style.height = "900px";
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(canvas.width, canvas.height);
  let seed = 0x12345678;
  for (let i = 0; i < img.data.length; i += 4) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    img.data[i] = seed & 255;
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    img.data[i + 1] = seed & 255;
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    img.data[i + 2] = seed & 255;
    img.data[i + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
  return true;
})()
`, awaitPromise: true, returnByValue: true });
const large = await session.Runtime.evaluate({ expression: "'x'.repeat(200000)", returnByValue: true });
if (large.result.value.length !== 200000) throw new Error("large response length mismatch");
const info = (await session.Runtime.evaluate({
  expression: `({ url: location.href, title: document.title, readyState: document.readyState })`,
  returnByValue: true
})).result.value;
checkpoint("page_info", info);
await session.Page.captureScreenshot({ format: "png" });
return info;
"##,
            30_000,
            30_000,
        )
        .unwrap();
        assert!(job.ok, "{:?}\n{}", job.error, job.text);
        assert_eq!(job.data["title"], "Browser Smoke");
        assert!(!job.images.is_empty(), "expected screenshot image artifact");

        cleanup_session(session_id);
    }

    #[test]
    #[ignore = "launches a real local Chromium-family browser for controlled-input smoke verification"]
    fn managed_browser_fill_input_controlled_textarea_smoke() {
        if chromium_candidate_paths(true).is_empty() {
            eprintln!("skipping controlled textarea smoke: no Chromium-family browser found");
            return;
        }

        let temp = tempfile::tempdir().unwrap();
        let artifacts = temp.path().join("artifacts");
        let session_id = "managed-controlled-input-smoke";

        let connect = run_browser_command(
            session_id,
            temp.path(),
            &artifacts,
            "browser connect managed --headless",
        )
        .unwrap();
        assert_eq!(connect.content["status"], "connected");

        let job = execute_browser_js(
            session_id,
            temp.path(),
            &artifacts,
            r##"
const created = await cdp("Target.createTarget", { url: "about:blank" });
await session.use(created.targetId);
await session.Runtime.evaluate({ expression: `
(() => {
  document.title = "Controlled Input Smoke";
  document.body.innerHTML = `
    <textarea id="composer" placeholder="Message"></textarea>
    <button id="send" disabled>Send</button>
    <output id="result"></output>
  `;
  const textarea = document.querySelector("#composer");
  const send = document.querySelector("#send");
  const result = document.querySelector("#result");
  let state = "";
  const render = () => {
    send.disabled = state.length === 0;
  };
  textarea.addEventListener("input", event => {
    state = event.target.value;
    render();
  });
  send.addEventListener("click", () => {
    result.textContent = state;
  });
  render();
  return true;
})()
`, awaitPromise: true, returnByValue: true });
await session.DOM.enable();
const { root } = await session.DOM.getDocument({});
const { nodeId } = await session.DOM.querySelector({ nodeId: root.nodeId, selector: "#composer" });
const { model } = await session.DOM.getBoxModel({ nodeId });
const xs = model.border.filter((_, idx) => idx % 2 === 0);
const ys = model.border.filter((_, idx) => idx % 2 === 1);
const x = (Math.min(...xs) + Math.max(...xs)) / 2;
const y = (Math.min(...ys) + Math.max(...ys)) / 2;
await session.Input.dispatchMouseEvent({ type: "mousePressed", x, y, button: "left", clickCount: 1 });
await session.Input.dispatchMouseEvent({ type: "mouseReleased", x, y, button: "left", clickCount: 1 });
await session.Input.insertText({ text: "go to google" });
await session.Runtime.evaluate({ expression: `document.querySelector("#composer").dispatchEvent(new Event("input", { bubbles: true })); true`, returnByValue: true });
let state = (await session.Runtime.evaluate({ expression: `
(() => {
  const textarea = document.querySelector("#composer");
  const send = document.querySelector("#send");
  const result = document.querySelector("#result");
  return {
    value: textarea.value,
    disabled: send.disabled,
    result: result.textContent,
  };
})()
`, returnByValue: true })).result.value;
if (state.value !== "go to google" || state.disabled !== false) throw new Error(JSON.stringify(state));
await session.Runtime.evaluate({ expression: 'document.querySelector("#send").click(); true', returnByValue: true });
const submitted = (await session.Runtime.evaluate({ expression: 'document.querySelector("#result").textContent', returnByValue: true })).result.value;
if (submitted !== "go to google") throw new Error(`unexpected submitted value ${submitted}`);
checkpoint("controlled_input", state);
return state;
"##,
            30_000,
            30_000,
        )
        .unwrap();
        assert!(job.ok, "{:?}\n{}", job.error, job.text);
        assert_eq!(job.data["value"], "go to google");

        cleanup_session(session_id);
    }

    #[test]
    #[ignore = "launches a dedicated local Chromium-family browser and attaches through remote CDP"]
    fn remote_cdp_smoke_attaches_recovers_and_preserves_target() {
        if chromium_candidate_paths(true).is_empty() {
            eprintln!("skipping remote CDP smoke: no Chromium-family browser found");
            return;
        }

        let temp = tempfile::tempdir().unwrap();
        let artifacts = temp.path().join("artifacts");
        let source_session = "remote-cdp-source";
        let remote_session = "remote-cdp-client";

        let connect = run_browser_command(
            source_session,
            temp.path(),
            &artifacts,
            "browser connect managed --headless",
        )
        .unwrap();
        assert_eq!(connect.content["status"], "connected");
        let http_url = connect.content["browser"]["endpoint"]["http_url"]
            .as_str()
            .expect("managed browser http url")
            .to_string();

        let job = execute_browser_js(
            source_session,
            temp.path(),
            &artifacts,
            r##"
await session.Page.navigate({ url: "data:text/html,<title>Remote CDP Smoke</title><h1 id='ok'>Remote CDP Smoke</h1>" });
await session.waitFor("Page.loadEventFired", undefined, 5_000).catch(() => null);
return (await session.Runtime.evaluate({
  expression: `({ url: location.href, title: document.title, readyState: document.readyState })`,
  returnByValue: true
})).result.value;
"##,
            30_000,
            30_000,
        )
        .unwrap();
        assert!(job.ok, "{:?}\n{}", job.error, job.text);

        let connect_remote = run_browser_command(
            remote_session,
            temp.path(),
            &artifacts,
            &format!("browser connect remote-cdp --url {http_url}"),
        )
        .unwrap();
        assert_eq!(connect_remote.content["status"], "connected");
        assert_eq!(
            connect_remote.content["browser"]["owner"],
            BrowserOwner::External.as_str()
        );
        assert_eq!(connect_remote.content["browser"]["mode"], "remote-cdp");
        let before_target = connect_remote.content["browser"]["page"]["target_id"]
            .as_str()
            .expect("target id")
            .to_string();

        for command in [
            "browser recover reconnect-websocket",
            "browser recover reattach-same-target",
            "browser recover restart-runtime",
        ] {
            let recovered =
                run_browser_command(remote_session, temp.path(), &artifacts, command).unwrap();
            assert_eq!(
                recovered.content["browser"]["connection"], "connected",
                "recovery command failed: {command}: {}",
                recovered.content
            );
            assert_eq!(
                recovered.content["browser"]["page"]["target_id"], before_target,
                "target changed after {command}"
            );
        }

        let probe = execute_browser_js(
            remote_session,
            temp.path(),
            &artifacts,
            r##"
return (await session.Runtime.evaluate({
  expression: `({ url: location.href, title: document.title, readyState: document.readyState })`,
  returnByValue: true
})).result.value;
"##,
            30_000,
            30_000,
        )
        .unwrap();
        assert!(probe.ok, "{:?}\n{}", probe.error, probe.text);
        assert_eq!(probe.data["title"], "Remote CDP Smoke");

        let ownership = run_browser_command(
            remote_session,
            temp.path(),
            &artifacts,
            "browser runtime ownership --json",
        )
        .unwrap();
        assert_eq!(ownership.content["owner"], BrowserOwner::External.as_str());
        assert_eq!(
            ownership.content["safe_actions"]["restart_owned_browser"],
            false
        );

        cleanup_session(remote_session);
        cleanup_session(source_session);
    }
}
