//! Email one-time-code 2FA via AgentMail: provision an inbox, then poll it for
//! the arriving verification code.

use anyhow::{anyhow, bail, Result};
use serde_json::Value;

const BASE_URL: &str = "https://api.agentmail.to";
/// Stable client id so repeated provisioning returns the same inbox/address.
const INBOX_CLIENT_ID: &str = "browser-use-terminal";

/// A thin AgentMail REST client.
#[derive(Clone)]
pub struct AgentMail {
    token: String,
}

impl AgentMail {
    pub fn new(token: impl Into<String>) -> Self {
        Self {
            token: token.into(),
        }
    }

    /// Run a blocking reqwest call off-thread to avoid panicking inside a Tokio
    /// runtime (the bridge resolver may run on a worker thread).
    fn off_runtime<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
        std::thread::spawn(f)
            .join()
            .expect("agentmail http thread panicked")
    }

    /// Provision (idempotently) an inbox and return its email address.
    pub fn inbox_address(&self) -> Result<String> {
        let token = self.token.clone();
        Self::off_runtime(move || {
            let resp = reqwest::blocking::Client::new()
                .post(format!("{BASE_URL}/inboxes"))
                .bearer_auth(&token)
                .json(&serde_json::json!({ "client_id": INBOX_CLIENT_ID }))
                .send()
                .map_err(|err| anyhow!("AgentMail create-inbox request failed: {err}"))?;
            let status = resp.status();
            let body: Value = resp
                .json()
                .map_err(|err| anyhow!("AgentMail inbox response not JSON: {err}"))?;
            if !status.is_success() {
                bail!(
                    "AgentMail inbox error ({}): {}",
                    status.as_u16(),
                    body.get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("request rejected")
                );
            }
            // `inbox_id` is the email address (e.g. name@agentmail.to).
            body.get("inbox_id")
                .or_else(|| body.get("id"))
                .and_then(Value::as_str)
                .map(str::to_string)
                .ok_or_else(|| anyhow!("AgentMail response missing inbox_id"))
        })
    }

    /// Return the most recent one-time code found in the inbox, if any.
    pub fn latest_code(&self, inbox_id: &str) -> Result<Option<String>> {
        let token = self.token.clone();
        let inbox = inbox_id.to_string();
        Self::off_runtime(move || {
            let resp = reqwest::blocking::Client::new()
                .get(format!("{BASE_URL}/inboxes/{inbox}/messages"))
                .bearer_auth(&token)
                .query(&[("limit", "10")])
                .send()
                .map_err(|err| anyhow!("AgentMail list-messages request failed: {err}"))?;
            let status = resp.status();
            let body: Value = resp
                .json()
                .map_err(|err| anyhow!("AgentMail messages response not JSON: {err}"))?;
            if !status.is_success() {
                bail!("AgentMail messages error ({})", status.as_u16());
            }
            let messages = body
                .get("messages")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            // Newest first; return the first message that yields a code.
            for message in &messages {
                let subject = message.get("subject").and_then(Value::as_str).unwrap_or("");
                let text = message
                    .get("extracted_text")
                    .and_then(Value::as_str)
                    .or_else(|| message.get("text").and_then(Value::as_str))
                    .unwrap_or("");
                if let Some(code) = extract_otp(subject, text) {
                    return Ok(Some(code));
                }
            }
            Ok(None)
        })
    }
}

/// Extract an OTP from an email. 6-digit codes (the common length) are tried
/// first so years/counts don't win over a real code; keyword-adjacent matches
/// before bare ones.
pub fn extract_otp(subject: &str, body: &str) -> Option<String> {
    let haystack = format!("{subject}\n{body}");
    let kw =
        r"(?i)code|verification|verify|one[\s-]?time|passcode|otp|\bpin\b|security|authenticat";

    let first_capture = |pattern: &str| -> Option<String> {
        regex::Regex::new(pattern)
            .ok()
            .and_then(|re| re.captures(&haystack))
            .and_then(|caps| caps.get(1).map(|m| m.as_str().to_string()))
    };

    let patterns = [
        format!(r"(?:{kw})[^0-9]{{0,24}}(\d{{6}})\b"),
        format!(r"\b(\d{{6}})[^0-9]{{0,24}}(?:{kw})"),
        r"\b(\d{6})\b".to_string(),
        format!(r"(?:{kw})[^0-9]{{0,24}}(\d{{4,8}})\b"),
        format!(r"\b(\d{{4,8}})[^0-9]{{0,24}}(?:{kw})"),
        r"\b(\d{4,8})\b".to_string(),
    ];
    patterns.iter().find_map(|p| first_capture(p))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_code_after_keyword() {
        assert_eq!(
            extract_otp(
                "Your verification code",
                "Your code is 482913. It expires in 10 minutes."
            ),
            Some("482913".to_string())
        );
    }

    #[test]
    fn extracts_code_before_keyword() {
        assert_eq!(
            extract_otp("Sign in", "839201 is your one-time passcode."),
            Some("839201".to_string())
        );
    }

    #[test]
    fn prefers_six_digit_code_over_unrelated_numbers() {
        // Year 2026 and "10" should not win over the 6-digit code.
        assert_eq!(
            extract_otp(
                "Verify",
                "© 2026. Use 4029 31? No — your code: 715342 (valid 10 min)."
            ),
            Some("715342".to_string())
        );
    }

    #[test]
    fn returns_none_without_a_code() {
        assert_eq!(extract_otp("Welcome", "Thanks for signing up!"), None);
    }
}
