use super::*;
use crate::{Frame, pipeline::Clock};
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};
fn setup() -> (Trial, State, Arc<AtomicU64>) {
    let time = Arc::new(AtomicU64::new(1));
    let copy = time.clone();
    let clock: Clock = Arc::new(move || copy.load(Ordering::SeqCst));
    let mut state = State::new(10, clock).unwrap();
    state.mode.hello(1, 0).unwrap();
    state
        .mode
        .acknowledge(1, state.mode.generation(), Mode::Eco, 1)
        .unwrap();
    (Trial::new(1, 10_000_000).unwrap(), state, time)
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
    for (boot, duration) in [(0, 10_000_000), (1, 0), (1, 10_000_001), (1, 130_000_000)] {
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
