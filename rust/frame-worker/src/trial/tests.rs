use super::*;
use crate::{Frame, pipeline::Clock};
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};
fn setup() -> (Trial, State, Arc<AtomicU64>) {
    setup_policy(false, 10_000_000, false)
}
fn setup_policy(continuous: bool, duration: u64, inject: bool) -> (Trial, State, Arc<AtomicU64>) {
    setup_clock_policy(continuous, duration, inject, false)
}
fn setup_clock_policy(
    continuous: bool,
    duration: u64,
    inject: bool,
    stepping: bool,
) -> (Trial, State, Arc<AtomicU64>) {
    let time = Arc::new(AtomicU64::new(1));
    let copy = time.clone();
    let clock: Clock = Arc::new(move || {
        if stepping {
            copy.fetch_add(1, Ordering::SeqCst)
        } else {
            copy.load(Ordering::SeqCst)
        }
    });
    let mut state = State::new_with_tracking_recovery(10, clock, continuous).unwrap();
    state.mode.hello(1, 0).unwrap();
    state
        .mode
        .acknowledge(1, state.mode.generation(), Mode::Eco, 1)
        .unwrap();
    let trial = if continuous {
        Trial::new_continuous(1, duration, inject)
    } else {
        Trial::new(1, duration)
    }
    .unwrap();
    (trial, state, time)
}
fn publish(state: &mut State, seq: u64) {
    let frame = Frame {
        boot: 1,
        sequence: seq,
        captured_us: seq + 100,
        sent_us: seq + 100,
        generation: Some(state.mode.generation()),
        jpeg: vec![],
    };
    assert!(state.mode.publish_result(&frame, || true).unwrap());
    state.record_test_sample(Sample {
        boot: 1,
        generation: state.mode.generation(),
        sequence: seq,
        published_us: state.now(),
        age_high_us: 200_000,
        compute_us: 150_000,
    });
}
#[test]
fn eco_tracking_eco_and_measured_window_gate() {
    let (mut trial, mut state, time) = setup();
    for i in 1..=3 {
        time.store(i * 5_000_000, Ordering::SeqCst);
        publish(&mut state, i);
        trial.poll(&mut state, i * 5_000_000, true).unwrap();
    }
    assert_eq!(state.mode.requested(), Mode::Tracking);
    let generation = state.mode.generation();
    state
        .mode
        .acknowledge(1, generation, Mode::Tracking, 1)
        .unwrap();
    let mut samples = vec![];
    for i in 1..=115 {
        let now = 15_000_000 + i * 190_000;
        time.store(now, Ordering::SeqCst);
        publish(&mut state, i);
        samples.push(Sample {
            boot: 1,
            generation,
            sequence: i,
            published_us: now,
            age_high_us: 200_000,
            compute_us: 150_000,
        });
        trial.poll(&mut state, now, true).unwrap();
        if state.mode.requested() == Mode::Eco {
            break;
        }
    }
    assert_eq!(state.mode.requested(), Mode::Eco);
    state
        .mode
        .acknowledge(1, state.mode.generation(), Mode::Eco, 1)
        .unwrap();
    let base = time.load(Ordering::SeqCst);
    for i in 1..=3 {
        let now = base + i * 5_000_000;
        time.store(now, Ordering::SeqCst);
        publish(&mut state, i);
        let more = trial.poll(&mut state, now, true).unwrap();
        assert_eq!(more, i != 3);
    }
    let report = trial.evaluate(&samples).unwrap();
    assert!(report["recognition_hz"].as_f64().unwrap() >= 5.);
    let positions: Vec<_> = samples
        .iter()
        .enumerate()
        .filter(|(_, s)| {
            trial.start.unwrap() <= s.published_us && s.published_us < trial.end.unwrap()
        })
        .map(|(i, _)| i)
        .collect();
    let gap: Vec<_> = samples
        .iter()
        .enumerate()
        .filter(|(i, _)| !positions[3..6].contains(i))
        .map(|(_, s)| *s)
        .collect();
    assert!(trial.evaluate(&gap).is_err()); // Still 50 results, but a gap exceeds 500ms.
    let mut wrong_gen = samples.clone();
    wrong_gen[positions[1]].generation = 999;
    wrong_gen[positions[1]].boot = 2;
    wrong_gen[positions[1]].age_high_us = 500_001;
    assert!(trial.evaluate(&wrong_gen).is_err());
    let mut wrong_boot = samples.clone();
    wrong_boot[positions[0]].boot = 2;
    assert!(trial.evaluate(&wrong_boot).is_err());
    let mut reversed = samples.clone();
    reversed[positions[3]].published_us = reversed[positions[2]].published_us - 1;
    assert!(trial.evaluate(&reversed).is_err());
    let mut stale = samples.clone();
    stale.iter_mut().for_each(|s| s.age_high_us = 500_001);
    assert!(trial.evaluate(&stale).is_err());
    assert!(
        trial
            .evaluate(&samples.iter().step_by(2).copied().collect::<Vec<_>>())
            .is_err()
    );
}
#[test]
fn readiness_reboot_and_manual_latches_abort_without_tracking_retry() {
    for failure in 0..3 {
        let (mut trial, mut state, time) = setup();
        time.store(5_000_000, Ordering::SeqCst);
        if failure == 1 {
            state.mode.disconnect().unwrap();
            state.mode.hello(2, 0).unwrap();
        }
        if failure == 2 {
            state.mode.protect("disk_low").unwrap();
        }
        assert!(trial.poll(&mut state, 5_000_000, failure != 0).is_err());
        assert_eq!(state.mode.requested(), Mode::Eco);
        assert!(trial.evaluate(&[]).is_err());
    }
    for (boot, duration) in [
        (0, 10_000_000),
        (1, 0),
        (1, 10_000_001),
        (1, MAX_DURATION_US + 10_000_000),
    ] {
        assert!(Trial::new(boot, duration).is_err());
    }
}
#[test]
fn supervisor_ticket_has_nonce_size_and_age_bounds() {
    let nonce = "0123456789abcdef";
    for at in [8500, 10000, 11500] {
        assert!(heartbeat(b"ready 0123456789abcdef 10000\n", nonce, at));
    }
    for raw in [
        b"ready 0123456789abcdef 10000 extra".as_slice(),
        b"ready bad 10000",
        b"ready 0123456789abcdef -1",
        b"ready 0123456789abcdef NaN",
        &[b'x'; 129],
    ] {
        assert!(!heartbeat(raw, nonce, 10000));
    }
    assert!(!heartbeat(b"ready 0123456789abcdef 10000", nonce, 11501));
    assert!(!heartbeat(b"ready 0123456789abcdef 10000", nonce, 8499));
}

#[test]
fn tracking_fault_cannot_trigger_an_automatic_tracking_retry() {
    let (mut trial, mut state, time) = setup();
    state
        .mode
        .request(Mode::Tracking, state.mode.generation(), true)
        .unwrap();
    trial.phase = Phase::Warmup;
    trial.generation = Some(state.mode.generation());
    state.mode.protect("frames_expired").unwrap();
    let generation = state.mode.generation();
    for now in [1000, 2000] {
        time.store(now, Ordering::SeqCst);
        assert!(trial.poll(&mut state, now, true).is_err());
        assert_eq!(state.mode.generation(), generation);
        assert_eq!(state.mode.requested(), Mode::Eco);
    }
}

#[test]
fn exact_five_hz_anchors_to_a_real_result_after_active_confirmation() {
    let (mut trial, mut state, time) = setup();
    for i in 1..=3 {
        let now = i * 5_000_000;
        time.store(now, Ordering::SeqCst);
        publish(&mut state, i);
        trial.poll(&mut state, now, true).unwrap();
    }
    let generation = state.mode.generation();
    state
        .mode
        .acknowledge(1, generation, Mode::Tracking, 1)
        .unwrap();
    for i in 1..=120 {
        let at = 15_000_000 + i * 200_000;
        time.store(at, Ordering::SeqCst);
        publish(&mut state, i);
        let poll = at + 100_000;
        time.store(poll, Ordering::SeqCst);
        trial.poll(&mut state, poll, true).unwrap();
        if state.mode.requested() == Mode::Eco {
            break;
        }
    }
    assert_eq!(trial.start, Some(25_400_000));
    state
        .mode
        .acknowledge(1, state.mode.generation(), Mode::Eco, 1)
        .unwrap();
    let base = time.load(Ordering::SeqCst);
    for i in 1..=3 {
        let at = base + i * 5_000_000;
        time.store(at, Ordering::SeqCst);
        publish(&mut state, i);
        trial.poll(&mut state, at, true).unwrap();
    }
    let report = trial.evaluate(state.samples()).unwrap();
    assert_eq!(report["window_results"], json!([50]));
    assert_eq!(report["recognition_hz"], json!(5.0));
}

#[test]
fn duration_is_explicit_bounded_and_defaults_to_sixty_seconds() {
    assert_eq!(duration_us(None), Ok(60_000_000));
    for seconds in ["10", "60", "600", "3600"] {
        let value = duration_us(Some(seconds)).unwrap();
        assert!(Trial::new(1, value).is_ok());
    }
    for value in [
        "",
        "0",
        "1",
        "9",
        "61",
        "3601",
        "3610",
        "-60",
        "+60",
        " 60",
        "60 ",
        "060",
        "99999999999999999",
    ] {
        assert!(duration_us(Some(value)).is_err(), "{value}");
    }
}

#[test]
fn hour_evidence_keeps_every_window_and_freshness_gate() {
    let mut trial = Trial::new(1, MAX_DURATION_US).unwrap();
    trial.phase = Phase::Done;
    trial.start = Some(1);
    trial.end = Some(1 + MAX_DURATION_US);
    trial.generation = Some(2);
    let mut samples: Vec<_> = (0..18_000)
        .map(|i| Sample {
            boot: 1,
            generation: 2,
            sequence: i + 1,
            published_us: 1 + i * 200_000,
            age_high_us: 500_000,
            compute_us: 150_000,
        })
        .collect();
    let report = trial.evaluate(&samples).unwrap();
    assert_eq!(report["results"], 18_000);
    assert_eq!(report["window_results"], json!(vec![50; 360]));
    assert_eq!(report["max_gap_us"], 200_000);
    samples[10_000].age_high_us = 500_001;
    assert!(trial.evaluate(&samples).is_err());
    samples[10_000].age_high_us = 500_000;
    samples.drain(10_000..10_003);
    assert_eq!(trial.evaluate(&samples), Err("tracking_below_target"));
}

#[test]
fn continuous_run_exercises_new_ack_recovery_and_retains_strict_metrics() {
    let (mut trial, mut state, time) = setup_clock_policy(true, 60_000_000, true, true);
    for i in 1..=3 {
        time.store(i * 5_000_000, Ordering::SeqCst);
        publish(&mut state, i);
        let now = state.now();
        trial.poll(&mut state, now, true).unwrap();
    }
    state
        .mode
        .acknowledge(1, state.mode.generation(), Mode::Tracking, 1)
        .unwrap();
    for seq in 4..600 {
        time.fetch_add(190_000, Ordering::SeqCst);
        publish(&mut state, seq);
        state.mode.set_recovery_ready(true);
        let now = state.now();
        trial.poll(&mut state, now, true).unwrap();
        state.mode.set_recovery_ready(false);
        if state.mode.requested() == Mode::Eco {
            break;
        }
        if state.mode.command().unwrap().is_some() {
            // Receiver polls the controller again before receiving the new ACK.
            let now = state.now();
            trial.poll(&mut state, now, true).unwrap();
            state
                .mode
                .acknowledge(1, state.mode.generation(), Mode::Tracking, 1)
                .unwrap();
        }
    }
    assert_eq!(state.mode.requested(), Mode::Eco);
    state
        .mode
        .acknowledge(1, state.mode.generation(), Mode::Eco, 1)
        .unwrap();
    let base = state.now();
    for i in 1..=3 {
        let now = base + i * 5_000_000;
        time.store(now, Ordering::SeqCst);
        publish(&mut state, 1000 + i);
        assert_eq!(trial.poll(&mut state, now, true).unwrap(), i != 3);
    }
    let report = trial.evaluate(state.samples()).unwrap();
    assert_eq!(report["policy"], "continuous-recovery-v1");
    assert_eq!(report["injected_hold"], true);
    assert_eq!(report["tracking_hold_events"], 1);
    assert_eq!(report["tracking_automatic_recoveries"], 1);
    assert_eq!(report["strict_single_generation_gate_passed"], false);
    assert_eq!(report["strict_rate_gate_passed"], true);
    assert_eq!(report["generation_epochs"].as_array().unwrap().len(), 2);
    assert!(report["tracking_hold_seconds"].as_f64().unwrap() >= 10.);
    let holds = report["tracking_holds"].as_array().unwrap();
    assert_eq!(holds.len(), 1);
    assert_eq!(holds[0][0], report["injected_hold_at"][1]);
    assert_eq!(holds[0][1], report["injected_hold_at"][0]);
    assert!(
        (10_000_000..=30_000_000)
            .contains(&(holds[0][2].as_u64().unwrap() - holds[0][1].as_u64().unwrap()))
    );
    let injected = trial.injected_at;
    trial.injected_at = Some((trial.end.unwrap(), 999));
    assert_eq!(
        trial.evaluate(state.samples()),
        Err("missing_in_window_recovery")
    );
    trial.injected_at = injected;
    let recovered = trial.holds[0].2;
    trial.holds[0].2 = trial.end.unwrap() + 1;
    assert_eq!(
        trial.evaluate(state.samples()),
        Err("missing_in_window_recovery")
    );
    trial.holds[0].2 = state
        .samples()
        .iter()
        .rev()
        .find(|s| s.published_us < trial.end.unwrap())
        .unwrap()
        .published_us
        + 1;
    assert_eq!(
        trial.evaluate(state.samples()),
        Err("missing_in_window_recovery")
    );
    trial.holds[0].2 = recovered;
    let mut stale = state.samples().to_vec();
    stale.iter_mut().for_each(|s| s.age_high_us = 500_001);
    assert_eq!(trial.evaluate(&stale), Err("invalid_samples"));
    assert_eq!(trial.evaluate(&[]), Err("invalid_samples"));
    assert!(
        trial
            .evaluate(
                &state
                    .samples()
                    .iter()
                    .step_by(2)
                    .copied()
                    .collect::<Vec<_>>()
            )
            .is_err()
    );
}

#[test]
fn continuous_policy_has_bounded_hold_and_terminal_faults_never_retry() {
    assert!(Trial::new_continuous(1, 10_000_000, true).is_err());
    for reason in ["frames_expired", "disk_low", "manual"] {
        let (mut trial, mut state, time) = setup_policy(true, 10_000_000, false);
        state
            .mode
            .request(Mode::Tracking, state.mode.generation(), true)
            .unwrap();
        trial.phase = Phase::Warmup;
        trial.generation = Some(state.mode.generation());
        trial.epochs.push((1, state.mode.generation()));
        state.mode.protect(reason).unwrap();
        if reason == "frames_expired" {
            assert!(trial.poll(&mut state, 1, true).unwrap());
            time.store(30_000_002, Ordering::SeqCst);
            assert_eq!(
                trial.poll(&mut state, 30_000_002, true),
                Err("mode_degraded")
            ); // ACK timeout wins.
        } else {
            let generation = state.mode.generation();
            assert_eq!(trial.poll(&mut state, 1, true), Err("mode_degraded"));
            assert_eq!(state.mode.generation(), generation);
            assert_eq!(state.mode.requested(), Mode::Eco);
        }
    }
}

#[test]
fn repeated_low_cadence_cannot_extend_a_continuous_hold_forever() {
    let (mut trial, mut state, time) = setup_policy(true, 10_000_000, false);
    state
        .mode
        .request(Mode::Tracking, state.mode.generation(), true)
        .unwrap();
    trial.phase = Phase::Warmup;
    trial.generation = Some(state.mode.generation());
    trial.epochs.push((1, state.mode.generation()));
    state.mode.protect("frames_expired").unwrap();
    state
        .mode
        .acknowledge(1, state.mode.generation(), Mode::Tracking, 1)
        .unwrap();
    assert!(trial.poll(&mut state, 1, true).unwrap());
    for seq in 1..=100 {
        time.fetch_add(300_000, Ordering::SeqCst);
        publish(&mut state, seq);
        let now = state.now();
        assert!(trial.poll(&mut state, now, true).unwrap());
    }
    time.fetch_add(1, Ordering::SeqCst);
    let now = state.now();
    assert_eq!(
        trial.poll(&mut state, now, true),
        Err("tracking_recovery_timeout")
    );
}

#[test]
fn recovery_epoch_limit_accepts_128_and_rejects_the_129th_or_skipped_generation() {
    let mut trial = Trial::new_continuous(1, 60_000_000, false).unwrap();
    trial.generation = Some(10);
    trial.epochs.push((1, 10));
    assert_eq!(
        trial.recovery_epoch(2, 12, Some(1)),
        Err("invalid_recovery_epoch")
    );
    assert_eq!(
        trial.recovery_epoch(2, 11, Some(2)),
        Err("invalid_recovery_epoch")
    );
    for count in 1..=128 {
        trial
            .recovery_epoch(count + 1, count + 10, Some(count))
            .unwrap();
    }
    assert_eq!(trial.epochs.len(), 129);
    assert_eq!(
        trial.recovery_epoch(130, 139, Some(129)),
        Err("invalid_recovery_epoch")
    );
}

#[test]
fn single_generation_metric_excludes_a_recovery_before_the_measured_window() {
    let mut trial = Trial::new_continuous(1, 10_000_000, false).unwrap();
    trial.phase = Phase::Done;
    trial.start = Some(30_000_000);
    trial.end = Some(40_000_000);
    trial.generation = Some(2);
    trial.epochs = vec![(1, 1), (10_000_000, 2)];
    trial.holds = vec![(2, 10_000_000, 20_000_000)];
    trial.tracking = json!({"tracking_hold_events":1,"tracking_automatic_recoveries":1,"tracking_hold_seconds":10.});
    let samples: Vec<_> = (0..50)
        .map(|i| Sample {
            boot: 1,
            generation: 2,
            sequence: i + 1,
            published_us: 30_000_000 + i * 200_000,
            age_high_us: 100_000,
            compute_us: 50_000,
        })
        .collect();
    let report = trial.evaluate(&samples).unwrap();
    assert_eq!(report["generation_epochs"].as_array().unwrap().len(), 2);
    assert_eq!(report["strict_single_generation_gate_passed"], true);
    trial.inject_hold = true;
    trial.injected = true;
    trial.injected_at = Some((10_000_000, 2));
    assert_eq!(trial.evaluate(&samples), Err("missing_in_window_recovery"));
}
