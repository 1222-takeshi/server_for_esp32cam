//! Offline-only replay: envelope order is checked; clocks/ACK/freshness are not.
use crate::{Frame, FrameOrder, detector::Result, read_frame};
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    time::Duration,
};

pub const MAX_FRAMES: usize = 120;
pub const MAX_LINE: usize = 64 * 1024;

pub fn run(
    reader: &mut impl Read,
    writer: &mut impl Write,
    process: impl FnMut(&Frame) -> Result<Value>,
) -> Result<usize> {
    run_with_gap(reader, writer, Duration::ZERO, process)
}

/// Explicit experiment setting: `0` (default) or `40` milliseconds.
pub fn parse_gap(value: std::result::Result<String, std::env::VarError>) -> Result<Duration> {
    match value {
        Err(std::env::VarError::NotPresent) => Ok(Duration::ZERO),
        Ok(value) if value == "0" => Ok(Duration::ZERO),
        Ok(value) if value == "40" => Ok(Duration::from_millis(40)),
        _ => Err("invalid frame gap setting".into()),
    }
}

/// Like [`run`], pausing `gap` after each result line and before the next frame is
/// processed. Nothing is inserted after the final frame, so `n` frames contain
/// `n - 1` gaps.
pub fn run_with_gap(
    reader: &mut impl Read,
    writer: &mut impl Write,
    gap: Duration,
    mut process: impl FnMut(&Frame) -> Result<Value>,
) -> Result<usize> {
    let mut order = FrameOrder::default();
    for count in 0..=MAX_FRAMES {
        let Some(frame) = read_frame(reader, true)? else {
            writer.flush()?;
            return Ok(count);
        };
        if count == MAX_FRAMES || frame.generation.is_none() {
            return Err("replay limit/profile".into());
        }
        order.observe(&frame)?;
        if count > 0 && !gap.is_zero() {
            std::thread::sleep(gap);
        }
        let mut event = process(&frame)?;
        let event = event.as_object_mut().ok_or("invalid result object")?;
        for (key, value) in [
            ("boot_id", json!(frame.boot)),
            ("sequence", json!(frame.sequence)),
            ("mode_generation", json!(frame.generation)),
            ("captured_us", json!(frame.captured_us)),
            ("sent_us", json!(frame.sent_us)),
            ("replay_only", json!(true)),
            ("freshness_checked", json!(false)),
            ("actuation_state", json!("hold")),
        ] {
            event.insert(key.into(), value);
        }
        let line = serde_json::to_vec(event)?;
        if line.len() + 1 > MAX_LINE {
            return Err("result line limit".into());
        }
        writer.write_all(&line)?;
        writer.write_all(b"\n")?;
        writer.flush()?;
    }
    unreachable!()
}

/// Linux process-wide CPU ticks and KiB RSS; unavailable values stay unknown.
pub fn usage() -> Value {
    let stat = std::fs::read_to_string("/proc/self/stat").ok();
    let ticks = stat
        .as_deref()
        .and_then(|s| s.rsplit_once(") "))
        .and_then(|(_, s)| {
            let fields: Vec<_> = s.split_whitespace().collect();
            fields
                .get(11)?
                .parse::<u64>()
                .ok()?
                .checked_add(fields.get(12)?.parse::<u64>().ok()?)
        });
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    let kib = |key: &str| {
        status.lines().find_map(|line| {
            line.strip_prefix(key)?
                .split_whitespace()
                .next()?
                .parse::<u64>()
                .ok()
        })
    };
    json!({"cpu_ticks": ticks, "rss_kib": kib("VmRSS:"), "hwm_kib": kib("VmHWM:")})
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn wire(sequence: u64, profile: bool) -> Vec<u8> {
        let mut bytes = if profile {
            b"ECF2".to_vec()
        } else {
            b"ECF1".to_vec()
        };
        for number in [1, sequence, sequence, sequence + 1] {
            bytes.extend(number.to_be_bytes());
        }
        if profile {
            bytes.extend(1u64.to_be_bytes());
        }
        bytes.extend(1u32.to_be_bytes());
        bytes.push(0);
        bytes
    }

    #[test]
    fn provenance_overrides_callback_and_never_claims_live_results() {
        let mut out = vec![];
        assert_eq!(
            run(&mut Cursor::new(wire(1, true)), &mut out, |_| Ok(
                json!({"sequence":99, "freshness_checked":true, "actuation_state":"move"})
            ))
            .unwrap(),
            1
        );
        let value: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(value["sequence"], 1);
        assert_eq!(value["replay_only"], true);
        assert_eq!(value["freshness_checked"], false);
        assert_eq!(value["actuation_state"], "hold");
    }

    #[test]
    fn frame_and_output_budgets_and_invalid_input_fail_closed() {
        let stream: Vec<_> = (1..=MAX_FRAMES as u64)
            .flat_map(|s| wire(s, true))
            .collect();
        assert_eq!(
            run(&mut Cursor::new(stream.clone()), &mut vec![], |_| Ok(
                json!({})
            ))
            .unwrap(),
            MAX_FRAMES
        );
        let mut excess = stream;
        excess.extend(wire(121, true));
        assert!(run(&mut Cursor::new(excess), &mut vec![], |_| Ok(json!({}))).is_err());
        for stream in [
            wire(1, false),
            [wire(1, true), wire(1, true)].concat(),
            wire(1, true)[..20].to_vec(),
        ] {
            assert!(run(&mut Cursor::new(stream), &mut vec![], |_| Ok(json!({}))).is_err());
        }
        assert!(
            run(&mut Cursor::new(wire(1, true)), &mut vec![], |_| Ok(
                json!({"data":"x".repeat(MAX_LINE)})
            ))
            .is_err()
        );
        assert!(
            run(&mut Cursor::new(wire(1, true)), &mut vec![], |_| Ok(json!(
                []
            )))
            .is_err()
        );
        assert!(
            run(&mut Cursor::new(wire(1, true)), &mut vec![], |_| Err(
                "detector failed".into()
            ))
            .is_err()
        );
    }

    #[test]
    fn gap_setting_is_strict_and_defaults_to_none() {
        for setting in [Err(std::env::VarError::NotPresent), Ok("0".into())] {
            assert_eq!(parse_gap(setting).unwrap(), Duration::ZERO);
        }
        assert_eq!(
            parse_gap(Ok("40".into())).unwrap(),
            Duration::from_millis(40)
        );
        for value in ["", "1", "39", "041", "40 ", "40ms", "-40", "true"] {
            assert!(parse_gap(Ok(value.into())).is_err());
        }
        assert!(parse_gap(Err(std::env::VarError::NotUnicode("x".into()))).is_err());
    }

    #[test]
    fn gap_precedes_every_frame_but_the_first_and_never_follows_the_last() {
        use std::time::Instant;
        let stream: Vec<_> = (1..=4u64).flat_map(|s| wire(s, true)).collect();
        let (mut starts, mut out) = (vec![], vec![]);
        let begin = Instant::now();
        let count = run_with_gap(
            &mut Cursor::new(stream),
            &mut out,
            Duration::from_millis(40),
            |_| {
                starts.push(begin.elapsed());
                Ok(json!({}))
            },
        )
        .unwrap();
        assert_eq!(count, 4);
        assert!(starts[0] < Duration::from_millis(40));
        for pair in starts.windows(2) {
            assert!(pair[1] - pair[0] >= Duration::from_millis(40));
        }
        // 4 frames = 3 gaps; the end of the run carries no trailing gap.
        assert!(begin.elapsed() < Duration::from_millis(40 * 3 + 35));
    }

    #[test]
    fn output_failure_propagates_without_retry() {
        struct Failed;
        impl std::io::Write for Failed {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::Error::other("sink failed"))
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let mut calls = 0;
        let result = run(&mut Cursor::new(wire(1, true)), &mut Failed, |_| {
            calls += 1;
            Ok(json!({}))
        });
        assert!(result.is_err());
        assert_eq!(calls, 1);
    }
}
