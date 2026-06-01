//! `SseDecoder` — turns a chunked byte stream into Server-Sent-Events frames.
//!
//! Shared by every JSON-streaming protocol (OpenAI Responses/Chat, Anthropic).
//! Pure and synchronous: feed it byte chunks as they arrive off the wire and it
//! yields complete frames, buffering partial lines across `push` calls. The
//! protocol layer interprets the frame `data` (e.g. parses JSON, handles
//! `[DONE]`); framing only deals with the SSE envelope.

/// One dispatched SSE frame: an optional `event:` name and the joined `data:`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SseFrame {
    pub event: Option<String>,
    pub data: String,
}

#[derive(Debug, Default)]
pub struct SseDecoder {
    buf: Vec<u8>,
    event: Option<String>,
    data: Vec<String>,
}

impl SseDecoder {
    pub fn new() -> Self {
        Self::default()
    }

    /// Feed the next chunk of bytes; returns any frames that completed.
    pub fn push(&mut self, bytes: &[u8]) -> Vec<SseFrame> {
        self.buf.extend_from_slice(bytes);
        let mut frames = Vec::new();
        while let Some(pos) = self.buf.iter().position(|&b| b == b'\n') {
            let mut line: Vec<u8> = self.buf.drain(..=pos).collect();
            line.pop(); // drop '\n'
            if line.last() == Some(&b'\r') {
                line.pop(); // drop '\r' (CRLF)
            }
            let line = String::from_utf8_lossy(&line);
            if let Some(frame) = self.process_line(&line) {
                frames.push(frame);
            }
        }
        frames
    }

    fn process_line(&mut self, line: &str) -> Option<SseFrame> {
        // Blank line: dispatch the buffered event (if any).
        if line.is_empty() {
            if self.event.is_none() && self.data.is_empty() {
                return None;
            }
            let frame = SseFrame {
                event: self.event.take(),
                data: self.data.join("\n"),
            };
            self.data.clear();
            return Some(frame);
        }
        // Comment line.
        if line.starts_with(':') {
            return None;
        }
        let (field, value) = match line.find(':') {
            Some(i) => {
                let v = &line[i + 1..];
                // SSE: strip exactly one leading space after the colon.
                let v = v.strip_prefix(' ').unwrap_or(v);
                (&line[..i], v)
            }
            None => (line, ""),
        };
        match field {
            "data" => self.data.push(value.to_string()),
            "event" => self.event = Some(value.to_string()),
            // `id` and `retry` are not meaningful for our streaming use.
            _ => {}
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn single_complete_event() {
        let mut d = SseDecoder::new();
        let frames = d.push(b"data: {\"a\":1}\n\n");
        assert_eq!(
            frames,
            vec![SseFrame {
                event: None,
                data: "{\"a\":1}".into()
            }]
        );
    }

    #[test]
    fn buffers_partial_line_across_pushes() {
        let mut d = SseDecoder::new();
        assert_eq!(d.push(b"data: hel"), vec![]);
        let frames = d.push(b"lo\n\n");
        assert_eq!(
            frames,
            vec![SseFrame {
                event: None,
                data: "hello".into()
            }]
        );
    }

    #[test]
    fn joins_multiple_data_lines() {
        let mut d = SseDecoder::new();
        let frames = d.push(b"data: a\ndata: b\n\n");
        assert_eq!(frames[0].data, "a\nb");
    }

    #[test]
    fn captures_event_name_and_ignores_comments() {
        let mut d = SseDecoder::new();
        let frames = d.push(b": keep-alive\nevent: ping\ndata: x\n\n");
        assert_eq!(
            frames,
            vec![SseFrame {
                event: Some("ping".into()),
                data: "x".into()
            }]
        );
    }

    #[test]
    fn handles_crlf_and_done_sentinel() {
        let mut d = SseDecoder::new();
        let frames = d.push(b"data: [DONE]\r\n\r\n");
        assert_eq!(
            frames,
            vec![SseFrame {
                event: None,
                data: "[DONE]".into()
            }]
        );
    }

    #[test]
    fn two_events_in_one_chunk() {
        let mut d = SseDecoder::new();
        let frames = d.push(b"data: one\n\ndata: two\n\n");
        assert_eq!(frames.len(), 2);
        assert_eq!(frames[0].data, "one");
        assert_eq!(frames[1].data, "two");
    }
}
