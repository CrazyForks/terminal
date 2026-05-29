//! The runtime composition layer: `Protocol × Endpoint × Auth × Framing`
//! (most added in later work packages). `framing` (the SSE byte→frame decoder)
//! lands first because it is pure and shared by every streaming protocol.

pub mod framing;

pub use framing::{SseDecoder, SseFrame};
