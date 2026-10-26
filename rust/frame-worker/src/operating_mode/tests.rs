use super::*;
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};

fn setup() -> (OperatingMode, Arc<AtomicU64>) {
    let time = Arc::new(AtomicU64::new(0));
    let copy = time.clone();
    (
        OperatingMode::new(10, move || copy.load(Ordering::SeqCst)).unwrap(),
        time,
    )
}
fn frame(mode: &OperatingMode, seq: u64) -> Frame {
    Frame {
        boot: 1,
        sequence: seq,
        captured_us: 100 + seq,
        sent_us: 100 + seq,
        generation: Some(mode.generation()),
        jpeg: vec![],
    }
}
#[test]
fn publication_failure_and_generation_fence() {
    let (mut mode, _) = setup();
    mode.hello(1, 0).unwrap();
    let old = frame(&mode, 1);
    assert!(!mode.publish_result(&old, || panic!("before ACK")).unwrap());
    assert!(
        mode.acknowledge(1, mode.generation(), Mode::Eco, 100)
            .unwrap()
    );
    mode.request(Mode::Tracking, mode.generation(), true)
        .unwrap();
    assert!(
        !mode
            .publish_result(&old, || panic!("old generation"))
            .unwrap()
    );
    assert!(
        mode.acknowledge(1, mode.generation(), Mode::Tracking, 100)
            .unwrap()
    );
    let current = frame(&mode, 2);
    assert_eq!(mode.publish_result(&current, || false), Err(ModeError(503)));
    assert_eq!(mode.requested(), Mode::Eco);
    assert_eq!(mode.status().unwrap()["reason"], "publication_failed");
}
#[test]
fn invalid_versions_and_bounded_history() {
    assert!(OperatingMode::new(0, || 0).is_err());
    let (mut mode, _) = setup();
    assert_eq!(mode.hello(0, 0), Err(ModeError(400)));
    assert_eq!(mode.request(Mode::Eco, 0, true), Err(ModeError(400)));
    mode.hello(1, 0).unwrap();
    mode.acknowledge(1, mode.generation(), Mode::Eco, 100)
        .unwrap();
    for seq in 1..=200 {
        assert!(mode.publish_result(&frame(&mode, seq), || true).unwrap());
    }
    assert_eq!(mode.frames.len(), 128);
    assert!(
        !mode
            .publish_result(&frame(&mode, 199), || panic!("duplicate"))
            .unwrap()
    );
}

fn assert_json(actual: &Value, expected: &Value) {
    match (actual, expected) {
        (Value::Number(a), Value::Number(b)) if a.is_f64() || b.is_f64() => {
            assert!(
                (a.as_f64().unwrap() - b.as_f64().unwrap()).abs() < 1e-8,
                "{actual} != {expected}"
            );
        }
        (Value::Object(a), Value::Object(b)) => {
            assert_eq!(a.len(), b.len());
            for (key, value) in b {
                assert_json(&a[key], value);
            }
        }
        _ => assert_eq!(actual, expected),
    }
}

#[test]
fn python_controller_traces() {
    check_traces(include_str!("../../fixtures/mode.jsonl"), false);
}

#[test]
fn python_tracking_recovery_traces() {
    check_traces(include_str!("../../fixtures/mode-recovery.jsonl"), true);
}

fn check_traces(input: &str, recovery: bool) {
    for line in input.lines() {
        let trace: Vec<Value> = serde_json::from_str(line).unwrap();
        let time = Arc::new(AtomicU64::new(0));
        let copy = time.clone();
        let mut mode = OperatingMode::new_with_tracking_recovery(
            10,
            move || copy.load(Ordering::SeqCst),
            recovery,
        )
        .unwrap();
        for event in trace {
            let a = &event["args"];
            time.store(event["at"].as_u64().unwrap(), Ordering::SeqCst);
            let number = |key: &str, default| a[key].as_u64().unwrap_or(default);
            let version = number("version", 0);
            let boot = number("boot", 1);
            let target = match a["mode"].as_str().unwrap() {
                "eco" => Mode::Eco,
                "tracking" => Mode::Tracking,
                _ => panic!(),
            };
            let result: Result<Value> = match event["op"].as_str().unwrap() {
                "hello" => mode.hello(boot, number("floor", 0)).map(|_| Value::Null),
                "ack" => mode
                    .acknowledge(boot, version, target, number("applied", 100))
                    .map(Value::from),
                "request" => mode
                    .request(target, version, a["ready"].as_bool().unwrap_or(true))
                    .map(|_| Value::Null),
                "disconnect" => mode.disconnect().map(|_| Value::Null),
                "tick" => mode.tick().map(|_| Value::Null),
                "command" => mode.command().map(|r| r.unwrap_or(Value::Null)),
                "ready" => {
                    mode.set_recovery_ready(a["value"].as_bool().unwrap());
                    Ok(Value::Null)
                }
                "protect" => mode
                    .protect(match a["reason"].as_str().unwrap() {
                        "disk_low" => "disk_low",
                        "frames_expired" => "frames_expired",
                        "inference_failed" => "inference_failed",
                        "publication_failed" => "publication_failed",
                        "resource_thermal_fault" => "resource_thermal_fault",
                        _ => panic!(),
                    })
                    .map(|_| Value::Null),
                "frame" | "burst" => {
                    let mut accepted = 0u64;
                    let mut failure = None;
                    for i in 0..number("count", 1) {
                        time.fetch_add(number("step", 0), Ordering::SeqCst);
                        let seq = number("seq", 1) + i;
                        let captured = number("captured", 100 + seq);
                        let frame = Frame {
                            boot,
                            sequence: seq,
                            captured_us: captured,
                            sent_us: captured,
                            generation: Some(version),
                            jpeg: vec![],
                        };
                        match mode.publish_result(&frame, || a["success"].as_bool().unwrap_or(true))
                        {
                            Ok(value) => accepted += u64::from(value),
                            Err(error) => {
                                failure = Some(error);
                                break;
                            }
                        }
                    }
                    failure.map_or(Ok(Value::from(accepted)), Err)
                }
                _ => panic!(),
            };
            let actual = result.unwrap_or_else(|e| json!({"error":e.0}));
            assert_json(&actual, &event["result"]);
            assert_json(&mode.status().unwrap(), &event["status"]);
        }
    }
}

#[test]
fn publication_panic_latches_eco_without_poisoning_owner_mutex() {
    let (mut mode, _) = setup();
    mode.hello(1, 0).unwrap();
    mode.request(Mode::Tracking, mode.generation(), true)
        .unwrap();
    mode.acknowledge(1, mode.generation(), Mode::Tracking, 100)
        .unwrap();
    let frame = frame(&mode, 1);
    let owner = std::sync::Mutex::new(mode);
    assert_eq!(
        owner
            .lock()
            .unwrap()
            .publish_result(&frame, || panic!("RAM publication failed")),
        Err(ModeError(503))
    );
    let status = owner.lock().unwrap().status().unwrap();
    assert_eq!(status["requested_mode"], "eco");
    assert_eq!(status["actuation_state"], "hold");
    assert_eq!(status["reason"], "publication_failed");
}

#[test]
fn watchdog_and_gate_share_one_clock_sample_at_the_gap_boundary() {
    use std::sync::atomic::{AtomicBool, AtomicUsize};
    let armed = Arc::new(AtomicBool::new(false));
    let reads = Arc::new(AtomicUsize::new(0));
    let (a, r) = (armed.clone(), reads.clone());
    let mut mode = OperatingMode::new(10, move || {
        if !a.load(Ordering::SeqCst) {
            100_000
        } else if r.fetch_add(1, Ordering::SeqCst) == 0 {
            599_999
        } else {
            600_001
        }
    })
    .unwrap();
    mode.hello(1, 0).unwrap();
    mode.request(Mode::Tracking, mode.generation(), true)
        .unwrap();
    mode.acknowledge(1, mode.generation(), Mode::Tracking, 100)
        .unwrap();
    assert!(mode.publish_result(&frame(&mode, 1), || true).unwrap());
    armed.store(true, Ordering::SeqCst);
    assert!(
        mode.publish_if(
            &frame(&mode, 2),
            |at| {
                assert_eq!(at, 599_999);
                true
            },
            || true
        )
        .unwrap()
    );
    assert_eq!(mode.frames.back().unwrap().at, 599_999);
    assert_eq!(reads.load(Ordering::SeqCst), 1);
    assert_eq!(
        mode.status().unwrap()["last_result_age_seconds"],
        json!(0.000002)
    );
}

#[test]
fn false_guard_skips_callback_and_panicking_guard_latches_hold() {
    let (mut mode, _) = setup();
    mode.hello(1, 0).unwrap();
    mode.acknowledge(1, mode.generation(), Mode::Eco, 100)
        .unwrap();
    let input = frame(&mode, 1);
    let generation = mode.generation();
    assert!(
        !mode
            .publish_if(&input, |_| false, || panic!("ineligible callback"))
            .unwrap()
    );
    assert_eq!(mode.generation(), generation);
    assert!(mode.status().unwrap()["reason"].is_null());
    assert_eq!(
        mode.publish_if(&input, |_| panic!("gate failure"), || true),
        Err(ModeError(503))
    );
    assert_eq!(mode.status().unwrap()["reason"], "publication_failed");
}
#[test]
fn paused_guard_uses_its_original_watchdog_stamp() {
    let (mut mode, time) = setup();
    time.store(100_000, Ordering::SeqCst);
    mode.hello(1, 0).unwrap();
    mode.request(Mode::Tracking, mode.generation(), true)
        .unwrap();
    mode.acknowledge(1, mode.generation(), Mode::Tracking, 100)
        .unwrap();
    assert!(
        mode.publish_if(
            &frame(&mode, 1),
            |at| {
                assert_eq!(at, 100_000);
                time.store(700_001, Ordering::SeqCst);
                true
            },
            || true
        )
        .unwrap()
    );
    let status = mode.status().unwrap();
    assert_eq!(status["requested_mode"], "eco");
    assert_eq!(status["reason"], "frames_expired");
    assert_eq!(status["actuation_state"], "hold");
}

#[test]
fn recovery_requires_owner_permit_and_never_reuses_an_old_ack() {
    let time = Arc::new(AtomicU64::new(1));
    let copy = time.clone();
    let mut mode =
        OperatingMode::new_with_tracking_recovery(10, move || copy.load(Ordering::SeqCst), true)
            .unwrap();
    mode.hello(1, 0).unwrap();
    mode.request(Mode::Tracking, mode.generation(), true)
        .unwrap();
    let old_generation = mode.generation();
    mode.acknowledge(1, old_generation, Mode::Tracking, 100)
        .unwrap();
    let old = frame(&mode, 1);
    mode.protect("frames_expired").unwrap();
    assert!(
        !mode
            .acknowledge(1, old_generation, Mode::Tracking, 100)
            .unwrap()
    );
    assert!(
        !mode
            .publish_result(&old, || panic!("old generation"))
            .unwrap()
    );
    mode.acknowledge(1, mode.generation(), Mode::Tracking, 100)
        .unwrap();
    for seq in 1..=54 {
        time.fetch_add(190_000, Ordering::SeqCst);
        assert!(mode.publish_result(&frame(&mode, seq), || true).unwrap());
    }
    assert_eq!(mode.status().unwrap()["tracking_automatic_recoveries"], 0);
    mode.set_recovery_ready(true); // Test owner holds its synthetic resource permit.
    mode.set_recovery_valid_until(Some(time.load(Ordering::SeqCst) - 1));
    assert_eq!(mode.status().unwrap()["tracking_automatic_recoveries"], 0);
    mode.set_recovery_valid_until(Some(time.load(Ordering::SeqCst)));
    let status = mode.status().unwrap();
    mode.set_recovery_ready(false);
    assert_eq!(status["actuation_state"], "observe_only");
    assert_eq!(status["tracking_automatic_recoveries"], 1);
    assert_eq!(status["tracking_hold_seconds"], 10.26);
    assert_eq!(mode.last_tracking_hold(), Some((1, 10_260_001)));
    assert_eq!(mode.tracking_hold_started(), None);
    time.fetch_add(500_001, Ordering::SeqCst);
    assert_eq!(mode.status().unwrap()["actuation_state"], "hold");
    let generation = mode.generation();
    for _ in 0..10 {
        mode.protect("frames_expired").unwrap();
    }
    assert_eq!(mode.generation(), generation);
    assert_eq!(mode.status().unwrap()["tracking_hold_events"], 2);
    mode.protect("resource_thermal_fault").unwrap();
    assert_eq!(mode.requested(), Mode::Eco);
    assert_eq!(
        mode.status().unwrap()["tracking_auto_recovery_pending"],
        false
    );
}
