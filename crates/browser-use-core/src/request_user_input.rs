//! `request_user_input` tool data types and response helpers extracted from `lib.rs`
//! (Phase 0.1 carve).
//!
//! Code motion only — behavior is byte-identical to the original definitions.

use std::collections::{HashMap, HashSet};
use std::time::Duration;

use anyhow::{bail, Result};
use browser_use_protocol::EventRecord;
use browser_use_store::Store;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::constants::*;
use crate::CollaborationModeKind;

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub(crate) struct RequestUserInputOption {
    label: String,
    description: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub(crate) struct RequestUserInputQuestion {
    id: String,
    header: String,
    question: String,
    #[serde(rename = "isOther", default)]
    is_other: bool,
    #[serde(rename = "isSecret", default)]
    is_secret: bool,
    options: Option<Vec<RequestUserInputOption>>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub(crate) struct RequestUserInputArgs {
    pub(crate) questions: Vec<RequestUserInputQuestion>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub(crate) struct RequestUserInputAnswer {
    answers: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
pub(crate) struct RequestUserInputResponse {
    answers: HashMap<String, RequestUserInputAnswer>,
}

pub(crate) fn normalize_request_user_input_args(
    mut args: RequestUserInputArgs,
) -> std::result::Result<RequestUserInputArgs, String> {
    let missing_options = args
        .questions
        .iter()
        .any(|question| question.options.as_ref().is_none_or(Vec::is_empty));
    if missing_options {
        return Err("request_user_input requires non-empty options for every question".to_string());
    }

    for question in &mut args.questions {
        question.is_other = true;
    }
    Ok(args)
}

pub(crate) fn request_user_input_unavailable_message(
    mode: CollaborationModeKind,
    default_mode_enabled: bool,
) -> Option<&'static str> {
    match mode {
        CollaborationModeKind::Plan => None,
        CollaborationModeKind::Default if default_mode_enabled => None,
        CollaborationModeKind::Default => Some("request_user_input is unavailable in Default mode"),
    }
}

pub(crate) fn active_request_user_input_turn_id(
    events: &[EventRecord],
    fallback_call_id: &str,
) -> String {
    let terminal_turn_ids = events
        .iter()
        .filter(|event| {
            matches!(
                event.event_type.as_str(),
                CODEX_TURN_COMPLETE_EVENT | CODEX_TURN_ABORTED_EVENT
            )
        })
        .filter_map(|event| {
            event
                .payload
                .get("turn_id")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect::<HashSet<_>>();
    events
        .iter()
        .rev()
        .filter(|event| event.event_type == CODEX_TURN_STARTED_EVENT)
        .filter_map(|event| {
            event
                .payload
                .get("turn_id")
                .and_then(Value::as_str)
                .filter(|turn_id| !terminal_turn_ids.contains(*turn_id))
                .map(str::to_string)
        })
        .next()
        .unwrap_or_else(|| fallback_call_id.to_string())
}

fn request_user_input_response_from_event(
    event: &EventRecord,
    turn_id: &str,
    call_id: &str,
) -> Option<RequestUserInputResponse> {
    if event.event_type != REQUEST_USER_INPUT_RESPONSE_EVENT {
        return None;
    }
    if let Some(response_turn_id) = event.payload.get("turn_id").and_then(Value::as_str) {
        if response_turn_id != turn_id {
            return None;
        }
    } else if event.payload.get("call_id").and_then(Value::as_str) != Some(call_id) {
        return None;
    }
    serde_json::from_value::<RequestUserInputResponse>(event.payload.clone())
        .ok()
        .or_else(|| {
            event
                .payload
                .get("response")
                .cloned()
                .and_then(|value| serde_json::from_value(value).ok())
        })
}

pub(crate) fn wait_for_request_user_input_response(
    store: &Store,
    session: &browser_use_protocol::SessionMeta,
    after_seq: i64,
    turn_id: &str,
    call_id: &str,
) -> Result<RequestUserInputResponse> {
    let mut last_seq = after_seq;
    loop {
        for event in
            store.wait_for_events_after_seq(&session.id, last_seq, Duration::from_millis(250))?
        {
            last_seq = last_seq.max(event.seq);
            if event.event_type == "session.cancelled" {
                bail!("{REQUEST_USER_INPUT_TOOL_NAME} was cancelled before receiving a response");
            }
            if let Some(response) = request_user_input_response_from_event(&event, turn_id, call_id)
            {
                return Ok(response);
            }
        }
    }
}
