//! Wire-format protocols (added per WP 1.3–1.5) and the shared stream-decoding
//! utilities they all build on (`utils`).

pub mod openai_responses;
pub mod utils;

pub use openai_responses::OpenAiResponsesProtocol;
