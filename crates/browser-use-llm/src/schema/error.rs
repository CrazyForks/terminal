//! Typed error taxonomy for the LLM layer.

use serde::{Deserialize, Serialize};

/// Coarse, provider-independent failure category.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LlmErrorReason {
    Authentication,
    RateLimit,
    QuotaExceeded,
    ContentPolicy,
    InvalidRequest,
    ProviderInternal,
    Transport,
    UnknownProvider,
    Decode,
}

/// An error from the LLM layer. `retryable` is set by the executor based on the
/// reason + provider signals (e.g. `Retry-After`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LlmError {
    pub reason: LlmErrorReason,
    pub message: String,
    #[serde(default)]
    pub retryable: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
}

impl LlmError {
    pub fn new(reason: LlmErrorReason, message: impl Into<String>) -> Self {
        let retryable = matches!(
            reason,
            LlmErrorReason::RateLimit
                | LlmErrorReason::ProviderInternal
                | LlmErrorReason::Transport
        );
        Self {
            reason,
            message: message.into(),
            retryable,
            status: None,
            request_id: None,
        }
    }
}

impl std::fmt::Display for LlmError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}: {}", self.reason, self.message)
    }
}

impl std::error::Error for LlmError {}
