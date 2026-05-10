use anyhow::{bail, Context, Result};
use base64::{engine::general_purpose, Engine as _};
use browser_use_protocol::{ModelEvent, ModelUsage, ToolCall, ToolSpec};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

#[derive(Clone, Debug, Default)]
pub struct ProviderTurn {
    pub messages: Vec<Value>,
    pub tools: Vec<ToolSpec>,
}

pub trait ModelProvider {
    fn provider_name(&self) -> &'static str {
        "unknown"
    }

    fn model_name(&self) -> &str {
        "unknown"
    }

    fn start_turn(&self, turn: ProviderTurn) -> Result<Vec<ModelEvent>>;
}

#[derive(Clone, Debug)]
pub struct FakeProvider {
    events: Vec<ModelEvent>,
}

impl FakeProvider {
    pub fn new(events: Vec<ModelEvent>) -> Self {
        Self { events }
    }

    pub fn with_text(text: impl Into<String>) -> Self {
        Self {
            events: vec![
                ModelEvent::TextDelta { text: text.into() },
                ModelEvent::Done,
            ],
        }
    }
}

impl Default for FakeProvider {
    fn default() -> Self {
        Self::with_text("ok")
    }
}

impl ModelProvider for FakeProvider {
    fn provider_name(&self) -> &'static str {
        "fake"
    }

    fn model_name(&self) -> &str {
        "fake"
    }

    fn start_turn(&self, _turn: ProviderTurn) -> Result<Vec<ModelEvent>> {
        Ok(self.events.clone())
    }
}

#[derive(Debug)]
pub struct ScriptedProvider {
    turns: Mutex<VecDeque<Vec<ModelEvent>>>,
}

impl ScriptedProvider {
    pub fn new(turns: Vec<Vec<ModelEvent>>) -> Self {
        Self {
            turns: Mutex::new(VecDeque::from(turns)),
        }
    }
}

impl ModelProvider for ScriptedProvider {
    fn provider_name(&self) -> &'static str {
        "scripted"
    }

    fn model_name(&self) -> &str {
        "scripted"
    }

    fn start_turn(&self, _turn: ProviderTurn) -> Result<Vec<ModelEvent>> {
        Ok(self
            .turns
            .lock()
            .expect("scripted provider lock")
            .pop_front()
            .unwrap_or_else(|| vec![ModelEvent::Done]))
    }
}

#[derive(Clone, Debug)]
pub struct OpenAIResponsesProvider {
    api_key: String,
    model: String,
    base_url: String,
    instructions: String,
    client: reqwest::blocking::Client,
}

impl OpenAIResponsesProvider {
    pub fn new(api_key: impl Into<String>, model: impl Into<String>) -> Self {
        Self::with_base_url(api_key, model, "https://api.openai.com/v1")
    }

    pub fn with_base_url(
        api_key: impl Into<String>,
        model: impl Into<String>,
        base_url: impl Into<String>,
    ) -> Self {
        Self {
            api_key: api_key.into(),
            model: model.into(),
            base_url: base_url.into().trim_end_matches('/').to_string(),
            instructions: default_instructions().to_string(),
            client: reqwest::blocking::Client::new(),
        }
    }

    pub fn from_env(model: impl Into<String>) -> Result<Self> {
        let api_key = std::env::var("LLM_BROWSER_OPENAI_API_KEY")
            .or_else(|_| std::env::var("OPENAI_API_KEY"))
            .context("set LLM_BROWSER_OPENAI_API_KEY or OPENAI_API_KEY")?;
        let base_url = std::env::var("LLM_BROWSER_OPENAI_BASE_URL")
            .unwrap_or_else(|_| "https://api.openai.com/v1".to_string());
        Ok(Self::with_base_url(api_key, model, base_url))
    }

    pub fn with_instructions(mut self, instructions: impl Into<String>) -> Self {
        self.instructions = instructions.into();
        self
    }
}

impl ModelProvider for OpenAIResponsesProvider {
    fn provider_name(&self) -> &'static str {
        "openai"
    }

    fn model_name(&self) -> &str {
        &self.model
    }

    fn start_turn(&self, turn: ProviderTurn) -> Result<Vec<ModelEvent>> {
        let input = messages_to_responses_input(&turn.messages)?;
        let tools = tool_specs_to_responses_tools(&turn.tools);
        let mut body = json!({
            "model": self.model,
            "input": input,
            "instructions": self.instructions,
            "store": false,
            "parallel_tool_calls": true,
        });
        if !tools.is_empty() {
            body["tools"] = Value::Array(tools);
        }

        let response = self
            .client
            .post(format!("{}/responses", self.base_url))
            .bearer_auth(&self.api_key)
            .json(&body)
            .send()
            .context("send OpenAI Responses request")?;
        let status = response.status();
        let body: Value = response.json().context("parse OpenAI Responses JSON")?;
        if !status.is_success() {
            bail!(
                "OpenAI Responses request failed ({status}): {}",
                openai_error_message(&body)
            );
        }
        parse_responses_output(&body)
    }
}

#[derive(Clone, Debug)]
pub struct OpenAICompatibleChatProvider {
    api_key: String,
    model: String,
    base_url: String,
    instructions: String,
    client: reqwest::blocking::Client,
}

impl OpenAICompatibleChatProvider {
    pub fn new(api_key: impl Into<String>, model: impl Into<String>) -> Self {
        Self::with_base_url(api_key, model, "https://openrouter.ai/api/v1")
    }

    pub fn with_base_url(
        api_key: impl Into<String>,
        model: impl Into<String>,
        base_url: impl Into<String>,
    ) -> Self {
        Self {
            api_key: api_key.into(),
            model: model.into(),
            base_url: base_url.into().trim_end_matches('/').to_string(),
            instructions: default_instructions().to_string(),
            client: reqwest::blocking::Client::new(),
        }
    }

    pub fn from_env(model: impl Into<String>) -> Result<Self> {
        let api_key = std::env::var("LLM_BROWSER_OPENAI_COMPAT_API_KEY")
            .or_else(|_| std::env::var("OPENROUTER_API_KEY"))
            .context("set LLM_BROWSER_OPENAI_COMPAT_API_KEY or OPENROUTER_API_KEY")?;
        let base_url = std::env::var("LLM_BROWSER_OPENAI_COMPAT_BASE_URL")
            .or_else(|_| std::env::var("OPENROUTER_BASE_URL"))
            .unwrap_or_else(|_| "https://openrouter.ai/api/v1".to_string());
        Ok(Self::with_base_url(api_key, model, base_url))
    }

    pub fn with_instructions(mut self, instructions: impl Into<String>) -> Self {
        self.instructions = instructions.into();
        self
    }
}

impl ModelProvider for OpenAICompatibleChatProvider {
    fn provider_name(&self) -> &'static str {
        "openai-compatible"
    }

    fn model_name(&self) -> &str {
        &self.model
    }

    fn start_turn(&self, turn: ProviderTurn) -> Result<Vec<ModelEvent>> {
        let mut messages = vec![json!({
            "role": "system",
            "content": self.instructions,
        })];
        messages.extend(messages_to_chat_messages(&turn.messages)?);
        let tools = tool_specs_to_chat_tools(&turn.tools);
        let mut body = json!({
            "model": self.model,
            "messages": messages,
            "parallel_tool_calls": true,
        });
        if !tools.is_empty() {
            body["tools"] = Value::Array(tools);
            body["tool_choice"] = json!("auto");
        }
        let response = self
            .client
            .post(format!("{}/chat/completions", self.base_url))
            .bearer_auth(&self.api_key)
            .json(&body)
            .send()
            .context("send OpenAI-compatible chat request")?;
        let status = response.status();
        let body: Value = response
            .json()
            .context("parse OpenAI-compatible chat JSON")?;
        if !status.is_success() {
            bail!(
                "OpenAI-compatible chat request failed ({status}): {}",
                openai_error_message(&body)
            );
        }
        parse_chat_completion_output(&body)
    }
}

#[derive(Clone, Debug)]
pub struct AnthropicMessagesProvider {
    credential: AnthropicCredential,
    model: String,
    base_url: String,
    instructions: String,
    client: reqwest::blocking::Client,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum AnthropicCredential {
    ApiKey(String),
    AuthToken(String),
}

impl AnthropicMessagesProvider {
    pub fn new(api_key: impl Into<String>, model: impl Into<String>) -> Self {
        Self::with_base_url(api_key, model, "https://api.anthropic.com/v1")
    }

    pub fn with_base_url(
        api_key: impl Into<String>,
        model: impl Into<String>,
        base_url: impl Into<String>,
    ) -> Self {
        Self::with_credential(AnthropicCredential::ApiKey(api_key.into()), model, base_url)
    }

    pub fn with_auth_token(
        auth_token: impl Into<String>,
        model: impl Into<String>,
        base_url: impl Into<String>,
    ) -> Self {
        Self::with_credential(
            AnthropicCredential::AuthToken(auth_token.into()),
            model,
            base_url,
        )
    }

    fn with_credential(
        credential: AnthropicCredential,
        model: impl Into<String>,
        base_url: impl Into<String>,
    ) -> Self {
        Self {
            credential,
            model: model.into(),
            base_url: base_url.into().trim_end_matches('/').to_string(),
            instructions: default_instructions().to_string(),
            client: reqwest::blocking::Client::new(),
        }
    }

    pub fn from_env(model: impl Into<String>) -> Result<Self> {
        let base_url = std::env::var("LLM_BROWSER_ANTHROPIC_BASE_URL")
            .unwrap_or_else(|_| "https://api.anthropic.com/v1".to_string());
        if let Ok(api_key) = std::env::var("LLM_BROWSER_ANTHROPIC_API_KEY")
            .or_else(|_| std::env::var("ANTHROPIC_API_KEY"))
        {
            if !api_key.trim().is_empty() {
                return Ok(Self::with_base_url(api_key, model, base_url));
            }
        }
        if let Ok(auth_token) = std::env::var("LLM_BROWSER_CLAUDE_CODE_OAUTH_TOKEN")
            .or_else(|_| std::env::var("CLAUDE_CODE_OAUTH_TOKEN"))
            .or_else(|_| std::env::var("ANTHROPIC_AUTH_TOKEN"))
        {
            if !auth_token.trim().is_empty() {
                return Ok(Self::with_auth_token(auth_token, model, base_url));
            }
        }
        bail!("set LLM_BROWSER_ANTHROPIC_API_KEY, ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, or ANTHROPIC_AUTH_TOKEN")
    }

    pub fn with_instructions(mut self, instructions: impl Into<String>) -> Self {
        self.instructions = instructions.into();
        self
    }
}

impl ModelProvider for AnthropicMessagesProvider {
    fn provider_name(&self) -> &'static str {
        "anthropic"
    }

    fn model_name(&self) -> &str {
        &self.model
    }

    fn start_turn(&self, turn: ProviderTurn) -> Result<Vec<ModelEvent>> {
        let tools = tool_specs_to_anthropic_tools(&turn.tools);
        let mut body = json!({
            "model": self.model,
            "max_tokens": 4096,
            "system": self.instructions,
            "messages": messages_to_anthropic_messages(&turn.messages)?,
        });
        if !tools.is_empty() {
            body["tools"] = Value::Array(tools);
            body["tool_choice"] = json!({"type": "auto"});
        }
        let mut request = self
            .client
            .post(format!("{}/messages", self.base_url))
            .header("anthropic-version", "2023-06-01");
        request = match &self.credential {
            AnthropicCredential::ApiKey(api_key) => request.header("x-api-key", api_key),
            AnthropicCredential::AuthToken(auth_token) => request
                .bearer_auth(auth_token)
                .header("anthropic-beta", "oauth-2025-04-20"),
        };
        let response = request
            .json(&body)
            .send()
            .context("send Anthropic Messages request")?;
        let status = response.status();
        let body: Value = response.json().context("parse Anthropic Messages JSON")?;
        if !status.is_success() {
            bail!(
                "Anthropic Messages request failed ({status}): {}",
                anthropic_error_message(&body)
            );
        }
        parse_anthropic_messages_output(&body)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CodexAuth {
    pub access_token: String,
    pub account_id: String,
}

#[derive(Clone, Debug)]
pub struct CodexResponsesProvider {
    auth: CodexAuth,
    model: String,
    base_url: String,
    instructions: String,
    client: reqwest::blocking::Client,
}

impl CodexResponsesProvider {
    pub fn new(auth: CodexAuth, model: impl Into<String>) -> Self {
        Self::with_base_url(auth, model, "https://chatgpt.com/backend-api")
    }

    pub fn with_base_url(
        auth: CodexAuth,
        model: impl Into<String>,
        base_url: impl Into<String>,
    ) -> Self {
        Self {
            auth,
            model: model.into(),
            base_url: base_url.into().trim_end_matches('/').to_string(),
            instructions: default_instructions().to_string(),
            client: reqwest::blocking::Client::new(),
        }
    }

    pub fn from_env(model: impl Into<String>) -> Result<Self> {
        let auth = load_codex_auth()?;
        let base_url = std::env::var("LLM_BROWSER_CODEX_BASE_URL")
            .unwrap_or_else(|_| "https://chatgpt.com/backend-api".to_string());
        Ok(Self::with_base_url(auth, model, base_url))
    }

    pub fn with_instructions(mut self, instructions: impl Into<String>) -> Self {
        self.instructions = instructions.into();
        self
    }
}

impl ModelProvider for CodexResponsesProvider {
    fn provider_name(&self) -> &'static str {
        "codex"
    }

    fn model_name(&self) -> &str {
        &self.model
    }

    fn start_turn(&self, turn: ProviderTurn) -> Result<Vec<ModelEvent>> {
        let input = messages_to_responses_input(&turn.messages)?;
        let tools = tool_specs_to_responses_tools(&turn.tools);
        let mut body = json!({
            "model": self.model,
            "input": input,
            "instructions": self.instructions,
            "store": false,
            "stream": true,
            "text": { "verbosity": "low" },
            "tool_choice": "auto",
            "parallel_tool_calls": true,
        });
        if !tools.is_empty() {
            body["tools"] = Value::Array(tools);
        }

        let response = self
            .client
            .post(codex_responses_url(&self.base_url))
            .bearer_auth(&self.auth.access_token)
            .header("chatgpt-account-id", &self.auth.account_id)
            .header("originator", "browser-use-terminal")
            .header("OpenAI-Beta", "responses=experimental")
            .header("Accept", "text/event-stream")
            .json(&body)
            .send()
            .context("send Codex Responses request")?;
        let status = response.status();
        if !status.is_success() {
            let body = response.text().unwrap_or_default();
            bail!(
                "Codex Responses request failed ({status}): {}",
                body.chars().take(1000).collect::<String>()
            );
        }
        parse_codex_sse(response)
    }
}

fn codex_responses_url(base_url: &str) -> String {
    let normalized = base_url.trim_end_matches('/');
    if normalized.ends_with("/codex/responses") {
        normalized.to_string()
    } else if normalized.ends_with("/codex") {
        format!("{normalized}/responses")
    } else {
        format!("{normalized}/codex/responses")
    }
}

pub fn load_codex_auth() -> Result<CodexAuth> {
    if let Ok(access_token) = std::env::var("LLM_BROWSER_CODEX_ACCESS_TOKEN") {
        let account_id = std::env::var("LLM_BROWSER_CODEX_ACCOUNT_ID")
            .context("set LLM_BROWSER_CODEX_ACCOUNT_ID with LLM_BROWSER_CODEX_ACCESS_TOKEN")?;
        return Ok(CodexAuth {
            access_token,
            account_id,
        });
    }
    let path = codex_auth_path().context("could not resolve Codex auth path")?;
    load_codex_auth_file(path)
}

fn codex_auth_path() -> Option<PathBuf> {
    if let Ok(path) = std::env::var("LLM_BROWSER_CODEX_AUTH_FILE") {
        return Some(PathBuf::from(path));
    }
    if let Ok(home) = std::env::var("CODEX_HOME") {
        return Some(PathBuf::from(home).join("auth.json"));
    }
    std::env::var("HOME")
        .ok()
        .map(|home| PathBuf::from(home).join(".codex").join("auth.json"))
}

pub fn load_codex_auth_file(path: impl AsRef<Path>) -> Result<CodexAuth> {
    let path = path.as_ref();
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("read Codex auth file {}", path.display()))?;
    let file: CodexAuthFile =
        serde_json::from_str(&raw).with_context(|| format!("parse {}", path.display()))?;
    let access_token = file
        .tokens
        .as_ref()
        .and_then(|tokens| tokens.access_token.clone())
        .or(file.access_token)
        .context("Codex auth missing access token")?;
    let account_id = file
        .tokens
        .as_ref()
        .and_then(|tokens| tokens.account_id.clone())
        .or(file.account_id)
        .or(file.chatgpt_account_id)
        .or_else(|| {
            file.tokens
                .as_ref()
                .and_then(|tokens| tokens.id_token.as_deref())
                .and_then(account_id_from_id_token)
        })
        .context("Codex auth missing account id")?;
    Ok(CodexAuth {
        access_token,
        account_id,
    })
}

#[derive(Debug, Deserialize)]
struct CodexAuthFile {
    tokens: Option<CodexAuthTokens>,
    access_token: Option<String>,
    account_id: Option<String>,
    chatgpt_account_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct CodexAuthTokens {
    access_token: Option<String>,
    account_id: Option<String>,
    id_token: Option<String>,
}

fn account_id_from_id_token(id_token: &str) -> Option<String> {
    let payload = id_token.split('.').nth(1)?;
    let decoded = general_purpose::URL_SAFE_NO_PAD.decode(payload).ok()?;
    let value: Value = serde_json::from_slice(&decoded).ok()?;
    value
        .get("chatgpt_account_id")
        .or_else(|| value.get("account_id"))
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
}

fn parse_codex_sse(response: reqwest::blocking::Response) -> Result<Vec<ModelEvent>> {
    let mut events = Vec::new();
    let mut data_lines = Vec::new();
    let mut seen_tool_calls = std::collections::HashSet::new();
    for line in BufReader::new(response).lines() {
        let line = line.context("read Codex SSE line")?;
        if line.is_empty() {
            flush_sse_event(&mut data_lines, &mut seen_tool_calls, &mut events)?;
        } else if let Some(data) = line.strip_prefix("data:") {
            data_lines.push(data.trim().to_string());
        }
    }
    flush_sse_event(&mut data_lines, &mut seen_tool_calls, &mut events)?;
    if !events.iter().any(|event| matches!(event, ModelEvent::Done)) {
        events.push(ModelEvent::Done);
    }
    Ok(events)
}

fn flush_sse_event(
    data_lines: &mut Vec<String>,
    seen_tool_calls: &mut std::collections::HashSet<String>,
    events: &mut Vec<ModelEvent>,
) -> Result<()> {
    if data_lines.is_empty() {
        return Ok(());
    }
    let data = data_lines.join("\n");
    data_lines.clear();
    if data.trim().is_empty() || data.trim() == "[DONE]" {
        return Ok(());
    }
    let event: Value = serde_json::from_str(&data).context("parse Codex SSE JSON")?;
    match event.get("type").and_then(Value::as_str) {
        Some("response.output_text.delta") => {
            if let Some(delta) = event.get("delta").and_then(Value::as_str) {
                events.push(ModelEvent::TextDelta {
                    text: delta.to_string(),
                });
            }
        }
        Some("response.output_item.done") => {
            if let Some(item) = event.get("item") {
                maybe_push_codex_output_item(item, seen_tool_calls, events)?;
            }
        }
        Some("response.completed") | Some("response.done") | Some("response.incomplete") => {
            if let Some(response) = event.get("response") {
                if let Some(items) = response.get("output").and_then(Value::as_array) {
                    for item in items {
                        maybe_push_codex_output_item(item, seen_tool_calls, events)?;
                    }
                }
                if let Some(usage) = parse_usage(response.get("usage")) {
                    events.push(ModelEvent::Usage { usage });
                }
            }
            events.push(ModelEvent::Done);
        }
        Some("error") => bail!("Codex stream error: {event}"),
        _ => {}
    }
    Ok(())
}

fn maybe_push_codex_output_item(
    item: &Value,
    seen_tool_calls: &mut std::collections::HashSet<String>,
    events: &mut Vec<ModelEvent>,
) -> Result<()> {
    if item.get("type").and_then(Value::as_str) == Some("function_call") {
        let call_id = item
            .get("call_id")
            .or_else(|| item.get("id"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if seen_tool_calls.insert(call_id) {
            parse_response_output_item(item, events)?;
        }
    } else {
        parse_response_output_item(item, events)?;
    }
    Ok(())
}

fn default_instructions() -> &'static str {
    concat!(
        "You are a browser-use agent. Use the python tool for browser work and call done when the user-facing task is complete.\n",
        "\n",
        "The python tool runs in a persistent namespace with browser-harness helpers already imported when available. ",
        "For browser tasks, start with goto_url(url), then use wait_for_load(), wait_for_element(), page_info(), js(...), ",
        "fill_input(selector, text), click_at_xy(x, y), press_key(key), scroll(...), wait_for_network_idle(), ",
        "capture_screenshot(...), current_tab(), list_tabs(), switch_tab(...), new_tab(...), cdp(...), and drain_events(). ",
        "Do not import or install Playwright, Selenium, or Pyppeteer; the browser connection is already provided by these helpers. ",
        "Use requests/http_get only after the browser path reveals a stable static endpoint or for downloaded files. ",
        "Use copy_artifact() and emit_image() for user-visible files and screenshots."
    )
}

fn tool_specs_to_responses_tools(tools: &[ToolSpec]) -> Vec<Value> {
    tools
        .iter()
        .map(|tool| {
            json!({
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            })
        })
        .collect()
}

fn tool_specs_to_chat_tools(tools: &[ToolSpec]) -> Vec<Value> {
    tools
        .iter()
        .map(|tool| {
            json!({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                }
            })
        })
        .collect()
}

fn tool_specs_to_anthropic_tools(tools: &[ToolSpec]) -> Vec<Value> {
    tools
        .iter()
        .map(|tool| {
            json!({
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            })
        })
        .collect()
}

fn messages_to_responses_input(messages: &[Value]) -> Result<Vec<Value>> {
    let mut input = Vec::new();
    for message in messages {
        let role = message
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("user");
        match role {
            "tool" => {
                let call_id = message
                    .get("tool_call_id")
                    .and_then(Value::as_str)
                    .context("tool message missing tool_call_id")?;
                input.push(json!({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": message_content_as_responses_output(message),
                }));
            }
            "system" => {
                let content = message_content_as_text(message);
                if !content.is_empty() {
                    input.push(json!({
                        "type": "message",
                        "role": "user",
                        "content": [{
                            "type": "input_text",
                            "text": format!("System context:\n{content}"),
                        }],
                    }));
                }
            }
            _ => {
                let content = message_content_parts(message, role);
                if !content.is_empty() {
                    input.push(json!({
                        "type": "message",
                        "role": role,
                        "content": content,
                    }));
                }
                if role == "assistant" {
                    for call in message
                        .get("tool_calls")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                    {
                        input.push(tool_call_to_responses_input(call)?);
                    }
                }
            }
        }
    }
    Ok(input)
}

fn messages_to_chat_messages(messages: &[Value]) -> Result<Vec<Value>> {
    let mut out = Vec::new();
    for message in messages {
        let role = message
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("user");
        match role {
            "tool" => out.push(json!({
                "role": "tool",
                "tool_call_id": message
                    .get("tool_call_id")
                    .and_then(Value::as_str)
                    .context("tool message missing tool_call_id")?,
                "content": message_content_as_text(message),
            })),
            "assistant" => {
                let mut item = json!({
                    "role": "assistant",
                    "content": message_content_as_text(message),
                });
                let tool_calls = message
                    .get("tool_calls")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .map(chat_tool_call)
                    .collect::<Result<Vec<_>>>()?;
                if !tool_calls.is_empty() {
                    item["tool_calls"] = Value::Array(tool_calls);
                }
                out.push(item);
            }
            "system" => out.push(json!({
                "role": "system",
                "content": message_content_as_text(message),
            })),
            _ => out.push(json!({
                "role": "user",
                "content": chat_content(message),
            })),
        }
    }
    Ok(out)
}

fn chat_content(message: &Value) -> Value {
    match message.get("content") {
        Some(Value::Array(parts)) => Value::Array(
            parts
                .iter()
                .filter_map(|part| match part.get("type").and_then(Value::as_str) {
                    Some("input_image") => {
                        let image_url = part.get("image_url").and_then(Value::as_str)?;
                        Some(json!({
                            "type": "image_url",
                            "image_url": { "url": image_url },
                        }))
                    }
                    Some("input_text") | Some("output_text") | Some("text") | None => part
                        .get("text")
                        .and_then(Value::as_str)
                        .filter(|text| !text.is_empty())
                        .map(|text| json!({ "type": "text", "text": text })),
                    _ => None,
                })
                .collect(),
        ),
        _ => Value::String(message_content_as_text(message)),
    }
}

fn chat_tool_call(call: &Value) -> Result<Value> {
    let call_id = call
        .get("id")
        .or_else(|| call.get("call_id"))
        .and_then(Value::as_str)
        .context("assistant tool call missing id")?;
    let name = call
        .get("name")
        .and_then(Value::as_str)
        .context("assistant tool call missing name")?;
    let arguments = call.get("arguments").cloned().unwrap_or_else(|| json!({}));
    Ok(json!({
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json_string(arguments)?,
        }
    }))
}

fn messages_to_anthropic_messages(messages: &[Value]) -> Result<Vec<Value>> {
    let mut out = Vec::new();
    for message in messages {
        let role = message
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("user");
        match role {
            "assistant" => out.push(json!({
                "role": "assistant",
                "content": anthropic_assistant_content(message)?,
            })),
            "tool" => out.push(json!({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message
                        .get("tool_call_id")
                        .and_then(Value::as_str)
                        .context("tool message missing tool_call_id")?,
                    "content": message_content_as_text(message),
                }],
            })),
            "system" => out.push(json!({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": format!("System context:\n{}", message_content_as_text(message)),
                }],
            })),
            _ => out.push(json!({
                "role": "user",
                "content": anthropic_user_content(message),
            })),
        }
    }
    Ok(out)
}

fn anthropic_user_content(message: &Value) -> Vec<Value> {
    match message.get("content") {
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(|part| match part.get("type").and_then(Value::as_str) {
                Some("input_image") => {
                    let image_url = part.get("image_url").and_then(Value::as_str)?;
                    data_url_source(image_url).map(|(media_type, data)| {
                        json!({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            }
                        })
                    })
                }
                Some("input_text") | Some("output_text") | Some("text") | None => part
                    .get("text")
                    .and_then(Value::as_str)
                    .filter(|text| !text.is_empty())
                    .map(|text| json!({ "type": "text", "text": text })),
                _ => None,
            })
            .collect(),
        _ => vec![json!({
            "type": "text",
            "text": message_content_as_text(message),
        })],
    }
}

fn anthropic_assistant_content(message: &Value) -> Result<Vec<Value>> {
    let mut blocks = Vec::new();
    let text = message_content_as_text(message);
    if !text.is_empty() {
        blocks.push(json!({ "type": "text", "text": text }));
    }
    for call in message
        .get("tool_calls")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        blocks.push(anthropic_tool_use_block(call)?);
    }
    Ok(blocks)
}

fn anthropic_tool_use_block(call: &Value) -> Result<Value> {
    let call_id = call
        .get("id")
        .or_else(|| call.get("call_id"))
        .and_then(Value::as_str)
        .context("assistant tool call missing id")?;
    let name = call
        .get("name")
        .and_then(Value::as_str)
        .context("assistant tool call missing name")?;
    let input = call.get("arguments").cloned().unwrap_or_else(|| json!({}));
    Ok(json!({
        "type": "tool_use",
        "id": call_id,
        "name": name,
        "input": input,
    }))
}

fn data_url_source(image_url: &str) -> Option<(String, String)> {
    let rest = image_url.strip_prefix("data:")?;
    let (header, data) = rest.split_once(',')?;
    let media_type = header.split(';').next()?.to_string();
    Some((media_type, data.to_string()))
}

fn input_text_type_for_role(role: &str) -> &'static str {
    if role == "assistant" {
        "output_text"
    } else {
        "input_text"
    }
}

fn message_content_parts(message: &Value, role: &str) -> Vec<Value> {
    match message.get("content") {
        Some(Value::String(content)) if !content.is_empty() => vec![json!({
            "type": input_text_type_for_role(role),
            "text": content,
        })],
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(|part| normalize_content_part(part, role))
            .collect(),
        Some(other) if !other.is_null() => vec![json!({
            "type": input_text_type_for_role(role),
            "text": other.to_string(),
        })],
        _ => Vec::new(),
    }
}

fn normalize_content_part(part: &Value, role: &str) -> Option<Value> {
    match part.get("type").and_then(Value::as_str) {
        Some("input_image") => {
            let image_url = part.get("image_url").and_then(Value::as_str)?;
            let mut out = json!({
                "type": "input_image",
                "image_url": image_url,
            });
            if let Some(detail) = part.get("detail").and_then(Value::as_str) {
                out["detail"] = json!(detail);
            }
            Some(out)
        }
        Some("input_text") | Some("output_text") | Some("text") | None => part
            .get("text")
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
            .map(|text| {
                json!({
                    "type": input_text_type_for_role(role),
                    "text": text,
                })
            }),
        _ => None,
    }
}

fn message_content_as_text(message: &Value) -> String {
    match message.get("content") {
        Some(Value::String(content)) => content.clone(),
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(|part| part.get("text").and_then(Value::as_str))
            .collect::<Vec<_>>()
            .join("\n"),
        Some(other) if !other.is_null() => other.to_string(),
        _ => String::new(),
    }
}

fn message_content_as_responses_output(message: &Value) -> Value {
    match message.get("content") {
        Some(Value::Array(parts)) => Value::Array(
            parts
                .iter()
                .filter_map(|part| normalize_tool_output_part(part))
                .collect(),
        ),
        _ => Value::String(message_content_as_text(message)),
    }
}

fn normalize_tool_output_part(part: &Value) -> Option<Value> {
    match part.get("type").and_then(Value::as_str) {
        Some("input_image") => {
            let image_url = part.get("image_url").and_then(Value::as_str)?;
            let mut out = json!({
                "type": "input_image",
                "image_url": image_url,
            });
            if let Some(detail) = part.get("detail").and_then(Value::as_str) {
                out["detail"] = json!(detail);
            }
            Some(out)
        }
        Some("output_text") | Some("input_text") | Some("text") | None => part
            .get("text")
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
            .map(|text| {
                json!({
                    "type": "input_text",
                    "text": text,
                })
            }),
        _ => None,
    }
}

fn tool_call_to_responses_input(call: &Value) -> Result<Value> {
    let call_id = call
        .get("id")
        .or_else(|| call.get("call_id"))
        .and_then(Value::as_str)
        .context("assistant tool call missing id")?;
    let name = call
        .get("name")
        .and_then(Value::as_str)
        .context("assistant tool call missing name")?;
    let arguments = call.get("arguments").cloned().unwrap_or_else(|| json!({}));
    Ok(json!({
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json_string(arguments)?,
    }))
}

fn json_string(value: Value) -> Result<String> {
    match value {
        Value::String(raw) => Ok(raw),
        other => serde_json::to_string(&other).context("serialize tool call arguments"),
    }
}

fn parse_responses_output(body: &Value) -> Result<Vec<ModelEvent>> {
    let mut events = Vec::new();
    if let Some(items) = body.get("output").and_then(Value::as_array) {
        for item in items {
            parse_response_output_item(item, &mut events)?;
        }
    }
    if events
        .iter()
        .all(|event| !matches!(event, ModelEvent::TextDelta { .. }))
    {
        if let Some(text) = body.get("output_text").and_then(Value::as_str) {
            if !text.is_empty() {
                events.push(ModelEvent::TextDelta {
                    text: text.to_string(),
                });
            }
        }
    }
    if let Some(usage) = parse_usage(body.get("usage")) {
        events.push(ModelEvent::Usage { usage });
    }
    events.push(ModelEvent::Done);
    Ok(events)
}

fn parse_chat_completion_output(body: &Value) -> Result<Vec<ModelEvent>> {
    let mut events = Vec::new();
    let Some(message) = body
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|choices| choices.first())
        .and_then(|choice| choice.get("message"))
    else {
        bail!("OpenAI-compatible chat response missing choices[0].message");
    };
    if let Some(content) = message.get("content").and_then(Value::as_str) {
        if !content.is_empty() {
            events.push(ModelEvent::TextDelta {
                text: content.to_string(),
            });
        }
    }
    for call in message
        .get("tool_calls")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some(function) = call.get("function") else {
            continue;
        };
        let name = function
            .get("name")
            .and_then(Value::as_str)
            .context("chat tool call missing function.name")?;
        let call_id = call
            .get("id")
            .and_then(Value::as_str)
            .context("chat tool call missing id")?;
        let arguments = function
            .get("arguments")
            .and_then(Value::as_str)
            .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
            .unwrap_or_else(|| json!({}));
        events.push(ModelEvent::ToolCall {
            call: ToolCall {
                id: call_id.to_string(),
                name: name.to_string(),
                arguments,
            },
        });
    }
    if let Some(usage) = parse_chat_usage(body.get("usage")) {
        events.push(ModelEvent::Usage { usage });
    }
    events.push(ModelEvent::Done);
    Ok(events)
}

fn parse_anthropic_messages_output(body: &Value) -> Result<Vec<ModelEvent>> {
    let mut events = Vec::new();
    for block in body
        .get("content")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        match block.get("type").and_then(Value::as_str) {
            Some("text") => {
                if let Some(text) = block.get("text").and_then(Value::as_str) {
                    events.push(ModelEvent::TextDelta {
                        text: text.to_string(),
                    });
                }
            }
            Some("tool_use") => {
                let name = block
                    .get("name")
                    .and_then(Value::as_str)
                    .context("Anthropic tool_use missing name")?;
                let call_id = block
                    .get("id")
                    .and_then(Value::as_str)
                    .context("Anthropic tool_use missing id")?;
                events.push(ModelEvent::ToolCall {
                    call: ToolCall {
                        id: call_id.to_string(),
                        name: name.to_string(),
                        arguments: block.get("input").cloned().unwrap_or_else(|| json!({})),
                    },
                });
            }
            _ => {}
        }
    }
    if let Some(usage) = parse_usage(body.get("usage")) {
        events.push(ModelEvent::Usage { usage });
    }
    events.push(ModelEvent::Done);
    Ok(events)
}

fn parse_response_output_item(item: &Value, events: &mut Vec<ModelEvent>) -> Result<()> {
    match item.get("type").and_then(Value::as_str) {
        Some("message") => {
            if let Some(parts) = item.get("content").and_then(Value::as_array) {
                for part in parts {
                    if matches!(
                        part.get("type").and_then(Value::as_str),
                        Some("output_text") | Some("text")
                    ) {
                        if let Some(text) = part.get("text").and_then(Value::as_str) {
                            events.push(ModelEvent::TextDelta {
                                text: text.to_string(),
                            });
                        }
                    }
                }
            }
        }
        Some("function_call") => {
            let name = item
                .get("name")
                .and_then(Value::as_str)
                .context("function_call missing name")?;
            let call_id = item
                .get("call_id")
                .or_else(|| item.get("id"))
                .and_then(Value::as_str)
                .context("function_call missing call_id")?;
            let arguments = item
                .get("arguments")
                .and_then(Value::as_str)
                .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
                .unwrap_or_else(|| json!({}));
            events.push(ModelEvent::ToolCall {
                call: ToolCall {
                    id: call_id.to_string(),
                    name: name.to_string(),
                    arguments,
                },
            });
        }
        _ => {}
    }
    Ok(())
}

fn parse_usage(usage: Option<&Value>) -> Option<ModelUsage> {
    let usage = usage?;
    Some(ModelUsage {
        input_tokens: usage.get("input_tokens").and_then(Value::as_i64),
        output_tokens: usage.get("output_tokens").and_then(Value::as_i64),
        total_tokens: usage.get("total_tokens").and_then(Value::as_i64),
        cost_usd: None,
    })
}

fn parse_chat_usage(usage: Option<&Value>) -> Option<ModelUsage> {
    let usage = usage?;
    Some(ModelUsage {
        input_tokens: usage
            .get("prompt_tokens")
            .or_else(|| usage.get("input_tokens"))
            .and_then(Value::as_i64),
        output_tokens: usage
            .get("completion_tokens")
            .or_else(|| usage.get("output_tokens"))
            .and_then(Value::as_i64),
        total_tokens: usage.get("total_tokens").and_then(Value::as_i64),
        cost_usd: None,
    })
}

fn openai_error_message(body: &Value) -> String {
    body.get("error")
        .and_then(|error| error.get("message"))
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| body.to_string())
}

fn anthropic_error_message(body: &Value) -> String {
    body.get("error")
        .and_then(|error| error.get("message"))
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| body.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn fake_provider_returns_scripted_events() -> Result<()> {
        let provider = FakeProvider::with_text("hello");
        let events = provider.start_turn(ProviderTurn::default())?;
        assert_eq!(
            events,
            vec![
                ModelEvent::TextDelta {
                    text: "hello".to_string()
                },
                ModelEvent::Done
            ]
        );
        Ok(())
    }

    #[test]
    fn scripted_provider_returns_one_turn_at_a_time() -> Result<()> {
        let provider = ScriptedProvider::new(vec![
            vec![ModelEvent::TextDelta {
                text: "first".to_string(),
            }],
            vec![ModelEvent::TextDelta {
                text: "second".to_string(),
            }],
        ]);
        assert_eq!(provider.start_turn(ProviderTurn::default())?.len(), 1);
        let second = provider.start_turn(ProviderTurn::default())?;
        assert_eq!(
            second,
            vec![ModelEvent::TextDelta {
                text: "second".to_string()
            }]
        );
        assert_eq!(
            provider.start_turn(ProviderTurn::default())?,
            vec![ModelEvent::Done]
        );
        Ok(())
    }

    #[test]
    fn responses_input_preserves_user_image_parts() -> Result<()> {
        let input = messages_to_responses_input(&[json!({
            "role": "user",
            "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "data:image/png;base64,abc", "detail": "high"}
            ]
        })])?;
        assert_eq!(input[0]["content"][0]["type"], "input_text");
        assert_eq!(input[0]["content"][1]["type"], "input_image");
        assert_eq!(
            input[0]["content"][1]["image_url"],
            "data:image/png;base64,abc"
        );
        assert_eq!(input[0]["content"][1]["detail"], "high");
        Ok(())
    }

    #[test]
    fn responses_input_preserves_tool_output_image_parts() -> Result<()> {
        let input = messages_to_responses_input(&[json!({
            "role": "tool",
            "tool_call_id": "call_1",
            "content": [
                {"type": "output_text", "text": "screenshot"},
                {"type": "input_image", "image_url": "data:image/png;base64,abc", "detail": "auto"}
            ]
        })])?;
        assert_eq!(input[0]["type"], "function_call_output");
        assert_eq!(input[0]["call_id"], "call_1");
        assert_eq!(input[0]["output"][0]["type"], "input_text");
        assert_eq!(input[0]["output"][1]["type"], "input_image");
        Ok(())
    }

    #[test]
    fn responses_input_converts_system_messages_to_user_context() -> Result<()> {
        let input = messages_to_responses_input(&[json!({
            "role": "system",
            "content": "compact summary"
        })])?;
        assert_eq!(input[0]["role"], "user");
        assert_eq!(input[0]["content"][0]["type"], "input_text");
        assert_eq!(
            input[0]["content"][0]["text"],
            "System context:\ncompact summary"
        );
        Ok(())
    }

    #[test]
    fn loads_codex_auth_file_with_nested_tokens() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let path = temp.path().join("auth.json");
        std::fs::write(
            &path,
            json!({
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "access-123",
                    "account_id": "account-123",
                    "refresh_token": "refresh-123"
                }
            })
            .to_string(),
        )?;
        assert_eq!(
            load_codex_auth_file(path)?,
            CodexAuth {
                access_token: "access-123".to_string(),
                account_id: "account-123".to_string(),
            }
        );
        Ok(())
    }

    #[test]
    fn codex_responses_provider_parses_sse_text_tool_call_and_usage() -> Result<()> {
        let sse = concat!(
            "data: {\"type\":\"response.output_text.delta\",\"delta\":\"Working\\n\"}\n\n",
            "data: {\"type\":\"response.output_item.done\",\"item\":{\"type\":\"function_call\",\"call_id\":\"call_123\",\"name\":\"done\",\"arguments\":\"{\\\"result\\\":\\\"ok\\\"}\"}}\n\n",
            "data: {\"type\":\"response.completed\",\"response\":{\"output\":[],\"usage\":{\"input_tokens\":3,\"output_tokens\":4,\"total_tokens\":7}}}\n\n",
        );
        let (base_url, handle) = spawn_mock_server(sse.to_string(), "text/event-stream")?;
        let provider = CodexResponsesProvider::with_base_url(
            CodexAuth {
                access_token: "chatgpt-token".to_string(),
                account_id: "account-123".to_string(),
            },
            "gpt-test",
            format!("{}/backend-api", base_url.trim_end_matches("/v1")),
        );
        let events = provider.start_turn(ProviderTurn {
            messages: vec![json!({"role": "user", "content": "finish"})],
            tools: vec![ToolSpec {
                name: "done".to_string(),
                description: "finish".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": { "result": { "type": "string" } },
                    "required": ["result"],
                    "additionalProperties": false
                }),
            }],
        })?;
        handle.join().expect("mock server thread");
        assert!(events.contains(&ModelEvent::TextDelta {
            text: "Working\n".to_string(),
        }));
        assert!(events.contains(&ModelEvent::ToolCall {
            call: ToolCall {
                id: "call_123".to_string(),
                name: "done".to_string(),
                arguments: json!({"result": "ok"}),
            },
        }));
        assert!(events.contains(&ModelEvent::Usage {
            usage: ModelUsage {
                input_tokens: Some(3),
                output_tokens: Some(4),
                total_tokens: Some(7),
                cost_usd: None,
            },
        }));
        assert!(events.contains(&ModelEvent::Done));
        Ok(())
    }

    #[test]
    fn openai_responses_provider_parses_text_tool_call_and_usage() -> Result<()> {
        let (base_url, handle) = spawn_mock_server(
            json!({
                "id": "resp_123",
                "object": "response",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Need the browser.\n"
                            }
                        ]
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_123",
                        "name": "done",
                        "arguments": "{\"result\":\"ok\"}",
                        "status": "completed"
                    }
                ],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18
                }
            })
            .to_string(),
            "application/json",
        )?;
        let provider = OpenAIResponsesProvider::with_base_url("test-key", "gpt-test", base_url);
        let events = provider.start_turn(ProviderTurn {
            messages: vec![json!({
                "role": "user",
                "content": "finish"
            })],
            tools: vec![ToolSpec {
                name: "done".to_string(),
                description: "finish".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": {
                        "result": { "type": "string" }
                    },
                    "required": ["result"],
                    "additionalProperties": false
                }),
            }],
        })?;
        handle.join().expect("mock server thread");
        assert!(events.contains(&ModelEvent::TextDelta {
            text: "Need the browser.\n".to_string()
        }));
        assert!(events.contains(&ModelEvent::ToolCall {
            call: ToolCall {
                id: "call_123".to_string(),
                name: "done".to_string(),
                arguments: json!({"result": "ok"}),
            }
        }));
        assert!(events.contains(&ModelEvent::Usage {
            usage: ModelUsage {
                input_tokens: Some(11),
                output_tokens: Some(7),
                total_tokens: Some(18),
                cost_usd: None,
            }
        }));
        assert!(events.contains(&ModelEvent::Done));
        Ok(())
    }

    #[test]
    fn openai_compatible_chat_provider_parses_tool_call_and_usage() -> Result<()> {
        let (base_url, handle) = spawn_mock_server(
            json!({
                "id": "chatcmpl_123",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Need a tool.\n",
                        "tool_calls": [{
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "done",
                                "arguments": "{\"result\":\"ok\"}"
                            }
                        }]
                    }
                }],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 6,
                    "total_tokens": 11
                }
            })
            .to_string(),
            "application/json",
        )?;
        let provider =
            OpenAICompatibleChatProvider::with_base_url("test-key", "openrouter/test", base_url);
        let events = provider.start_turn(ProviderTurn {
            messages: vec![json!({"role": "user", "content": "finish"})],
            tools: vec![ToolSpec {
                name: "done".to_string(),
                description: "finish".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": { "result": { "type": "string" } },
                    "required": ["result"],
                    "additionalProperties": false
                }),
            }],
        })?;
        handle.join().expect("mock server thread");
        assert!(events.contains(&ModelEvent::TextDelta {
            text: "Need a tool.\n".to_string()
        }));
        assert!(events.contains(&ModelEvent::ToolCall {
            call: ToolCall {
                id: "call_123".to_string(),
                name: "done".to_string(),
                arguments: json!({"result": "ok"}),
            }
        }));
        assert!(events.contains(&ModelEvent::Usage {
            usage: ModelUsage {
                input_tokens: Some(5),
                output_tokens: Some(6),
                total_tokens: Some(11),
                cost_usd: None,
            }
        }));
        Ok(())
    }

    #[test]
    fn anthropic_messages_provider_parses_text_tool_use_and_usage() -> Result<()> {
        let (base_url, handle) = spawn_mock_server(
            json!({
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [
                    { "type": "text", "text": "Working.\n" },
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "done",
                        "input": { "result": "ok" }
                    }
                ],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 8
                }
            })
            .to_string(),
            "application/json",
        )?;
        let provider =
            AnthropicMessagesProvider::with_base_url("anthropic-key", "claude-test", base_url);
        let events = provider.start_turn(ProviderTurn {
            messages: vec![json!({"role": "user", "content": "finish"})],
            tools: vec![ToolSpec {
                name: "done".to_string(),
                description: "finish".to_string(),
                input_schema: json!({
                    "type": "object",
                    "properties": { "result": { "type": "string" } },
                    "required": ["result"],
                    "additionalProperties": false
                }),
            }],
        })?;
        handle.join().expect("mock server thread");
        assert!(events.contains(&ModelEvent::TextDelta {
            text: "Working.\n".to_string()
        }));
        assert!(events.contains(&ModelEvent::ToolCall {
            call: ToolCall {
                id: "toolu_123".to_string(),
                name: "done".to_string(),
                arguments: json!({"result": "ok"}),
            }
        }));
        assert!(events.contains(&ModelEvent::Usage {
            usage: ModelUsage {
                input_tokens: Some(7),
                output_tokens: Some(8),
                total_tokens: None,
                cost_usd: None,
            }
        }));
        Ok(())
    }

    #[test]
    fn anthropic_messages_provider_accepts_oauth_auth_token() -> Result<()> {
        let (base_url, handle) = spawn_mock_server(
            json!({
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "content": [{ "type": "text", "text": "OAuth ok." }],
                "usage": { "input_tokens": 1, "output_tokens": 2 }
            })
            .to_string(),
            "application/json",
        )?;
        let provider = AnthropicMessagesProvider::with_auth_token(
            "claude-oauth-token",
            "claude-test",
            base_url,
        );
        let events = provider.start_turn(ProviderTurn {
            messages: vec![json!({"role": "user", "content": "finish"})],
            tools: Vec::new(),
        })?;
        handle.join().expect("mock server thread");
        assert!(events.contains(&ModelEvent::TextDelta {
            text: "OAuth ok.".to_string()
        }));
        assert!(events.contains(&ModelEvent::Usage {
            usage: ModelUsage {
                input_tokens: Some(1),
                output_tokens: Some(2),
                total_tokens: None,
                cost_usd: None,
            }
        }));
        Ok(())
    }

    fn spawn_mock_server(
        body: String,
        content_type: &'static str,
    ) -> Result<(String, thread::JoinHandle<()>)> {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let addr = listener.local_addr()?;
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept request");
            let mut request = Vec::new();
            let mut buf = [0_u8; 1024];
            loop {
                let read = stream.read(&mut buf).expect("read request");
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&buf[..read]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let request_text = String::from_utf8_lossy(&request);
            let request_text_lower = request_text.to_ascii_lowercase();
            assert!(
                request_text.starts_with("POST /v1/responses ")
                    || request_text.starts_with("POST /backend-api/codex/responses ")
                    || request_text.starts_with("POST /v1/chat/completions ")
                    || request_text.starts_with("POST /v1/messages ")
            );
            assert!(
                request_text_lower.contains("authorization: bearer test-key")
                    || request_text_lower.contains("authorization: bearer chatgpt-token")
                    || request_text_lower.contains("authorization: bearer claude-oauth-token")
                    || request_text_lower.contains("x-api-key: anthropic-key")
            );
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream
                .write_all(response.as_bytes())
                .expect("write response");
        });
        Ok((format!("http://{addr}/v1"), handle))
    }
}
