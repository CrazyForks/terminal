//! Pure stream-retry decision core (codex `turn.rs:924-1021`).

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RetryAction {
    Fail,
    SwitchTransport,
    Backoff { delay_ms: u64 },
}

pub fn retry_decision(
    retries: u32,
    max: u32,
    retryable: bool,
    can_switch_transport: bool,
    requested_delay_ms: Option<u64>,
) -> RetryAction {
    if !retryable {
        return RetryAction::Fail;
    }
    if retries >= max {
        return if can_switch_transport {
            RetryAction::SwitchTransport
        } else {
            RetryAction::Fail
        };
    }
    RetryAction::Backoff {
        delay_ms: requested_delay_ms.unwrap_or(backoff_ms(retries)),
    }
}

/// Deterministic, pure exponential backoff.
pub fn backoff_ms(retries: u32) -> u64 {
    200u64.saturating_mul(1u64 << retries.min(6))
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- retry_decision matrix (turn.rs:968-1021) ----

    #[test]
    fn non_retryable_always_fails() {
        // turn.rs:968 — `if !err.is_retryable() return Err`. Non-retryable short-circuits
        // before the at-max / transport / backoff branches, regardless of other inputs.
        for retries in [0u32, 1, 5, 100] {
            for can_switch in [false, true] {
                for delay in [None, Some(0u64), Some(1234)] {
                    assert_eq!(
                        retry_decision(retries, 5, false, can_switch, delay),
                        RetryAction::Fail,
                        "non-retryable should Fail (retries={retries}, switch={can_switch}, delay={delay:?})"
                    );
                }
            }
        }
    }

    #[test]
    fn at_max_with_transport_switches() {
        // turn.rs:974 — `retries >= max && try_switch_fallback_transport()`.
        // retries == max boundary and retries > max both switch when transport available.
        assert_eq!(
            retry_decision(5, 5, true, true, None),
            RetryAction::SwitchTransport,
            "retries == max + transport available"
        );
        assert_eq!(
            retry_decision(6, 5, true, true, Some(999)),
            RetryAction::SwitchTransport,
            "retries > max + transport available (requested_delay ignored)"
        );
    }

    #[test]
    fn at_max_without_transport_fails() {
        // turn.rs:1019-1020 — the `else` arm: at/over max and cannot switch -> Err.
        assert_eq!(
            retry_decision(5, 5, true, false, None),
            RetryAction::Fail,
            "retries == max, no transport"
        );
        assert_eq!(
            retry_decision(7, 5, true, false, Some(50)),
            RetryAction::Fail,
            "retries > max, no transport (requested_delay ignored)"
        );
    }

    #[test]
    fn under_max_backs_off_with_default_delay() {
        // turn.rs:990-997 — `retries < max_retries` -> backoff. With no server-requested
        // delay, fall back to the deterministic exponential backoff.
        assert_eq!(
            retry_decision(0, 5, true, false, None),
            RetryAction::Backoff {
                delay_ms: backoff_ms(0)
            }
        );
        assert_eq!(
            retry_decision(3, 5, true, true, None),
            // can_switch is irrelevant while under max.
            RetryAction::Backoff {
                delay_ms: backoff_ms(3)
            }
        );
    }

    #[test]
    fn under_max_honors_requested_delay() {
        // turn.rs:992-995 — `CodexErr::Stream(_, requested_delay)` uses the server-requested
        // delay verbatim, only falling back to backoff when absent.
        assert_eq!(
            retry_decision(2, 5, true, false, Some(7500)),
            RetryAction::Backoff { delay_ms: 7500 }
        );
        // A requested delay of 0 is honored (it is Some, not None).
        assert_eq!(
            retry_decision(2, 5, true, false, Some(0)),
            RetryAction::Backoff { delay_ms: 0 }
        );
    }

    #[test]
    fn max_zero_never_backs_off() {
        // With max == 0, every retryable error is immediately at-max.
        assert_eq!(
            retry_decision(0, 0, true, true, None),
            RetryAction::SwitchTransport
        );
        assert_eq!(retry_decision(0, 0, true, false, None), RetryAction::Fail);
    }

    // ---- backoff_ms: determinism, monotonicity, saturation ----

    #[test]
    fn backoff_ms_is_deterministic() {
        // Pure: same input -> same output, no rand/clock. Repeated calls agree.
        for r in 0u32..10 {
            assert_eq!(
                backoff_ms(r),
                backoff_ms(r),
                "backoff_ms({r}) must be stable"
            );
        }
    }

    #[test]
    fn backoff_ms_known_values() {
        // 200 * 2^min(r, 6).
        assert_eq!(backoff_ms(0), 200);
        assert_eq!(backoff_ms(1), 400);
        assert_eq!(backoff_ms(2), 800);
        assert_eq!(backoff_ms(3), 1_600);
        assert_eq!(backoff_ms(4), 3_200);
        assert_eq!(backoff_ms(5), 6_400);
        assert_eq!(backoff_ms(6), 12_800);
    }

    #[test]
    fn backoff_ms_monotonic_until_cap() {
        // Strictly increasing while retries <= 6, then flat (capped at the min(6) shift).
        for r in 0u32..6 {
            assert!(
                backoff_ms(r) < backoff_ms(r + 1),
                "backoff must grow from {r} to {}",
                r + 1
            );
        }
        let capped = backoff_ms(6);
        for r in 6u32..1000 {
            assert_eq!(backoff_ms(r), capped, "backoff_ms({r}) must stay at cap");
        }
    }

    #[test]
    fn backoff_ms_saturates_at_extremes() {
        // No overflow/panic for huge retry counts; stays at the capped value.
        assert_eq!(backoff_ms(u32::MAX), 12_800);
    }
}
