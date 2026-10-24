use super::*;
use crate::{control::SessionClock, operating_mode::Mode};
use std::sync::{atomic::AtomicU64, mpsc};
fn setup() -> (Arc<Mutex<State>>, Clock, Arc<AtomicU64>) {
    let time = Arc::new(AtomicU64::new(100_000));
    let copy = time.clone();
    let clock: Clock = Arc::new(move || copy.load(Ordering::SeqCst));
    let mut state = State::new(10, clock.clone()).unwrap();
    state.mode.hello(1, 0).unwrap();
    let generation = state.mode.generation();
    state.mode.acknowledge(1, generation, Mode::Eco, 1).unwrap();
    (Arc::new(Mutex::new(state)), clock, time)
}
fn job(state: &Arc<Mutex<State>>, seq: u64) -> Job {
    let frame = Frame {
        boot: 1,
        sequence: seq,
        captured_us: seq + 1,
        sent_us: seq + 1,
        generation: Some(state.lock().unwrap().mode.generation()),
        jpeg: vec![seq as u8],
    };
    let mut clock = SessionClock::default();
    clock
        .poll(100_000, false, || Ok("0123456789abcdef".into()))
        .unwrap();
    clock.pong(1, "0123456789abcdef", seq + 1, 100_000).unwrap();
    let arrival = clock.admit(&frame, 100_000).unwrap();
    Job {
        frame,
        arrival,
        cancelled: Arc::new(AtomicBool::new(false)),
    }
}
fn wait(state: &Arc<Mutex<State>>, inferred: u64) {
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    while state.lock().unwrap().counters.inferred < inferred {
        assert!(std::time::Instant::now() < deadline);
        thread::sleep(Duration::from_millis(1));
    }
}
#[test]
fn running_one_and_latest_waiting_one_without_io_blocking() {
    let (state, _clock, _) = setup();
    let (begun, started) = mpsc::channel();
    let (release, blocked) = mpsc::channel();
    let mut first = true;
    let worker = Worker::start(state.clone(), move |_| {
        if first {
            first = false;
            begun.send(()).unwrap();
            blocked.recv().unwrap();
        }
        Ok(vec![])
    })
    .unwrap();
    assert_eq!(worker.submit(job(&state, 1)), Submit::Queued);
    started.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(worker.submit(job(&state, 2)), Submit::Queued);
    assert_eq!(worker.submit(job(&state, 3)), Submit::Overwritten);
    release.send(()).unwrap();
    wait(&state, 2);
    worker.stop().unwrap();
    let s = state.lock().unwrap();
    assert_eq!(
        s.samples().iter().map(|s| s.sequence).collect::<Vec<_>>(),
        vec![1, 3]
    );
}
#[test]
fn completed_stale_or_old_generation_never_publishes() {
    for stale in [true, false] {
        let (state, _clock, time) = setup();
        let (begun, started) = mpsc::channel();
        let (release, blocked) = mpsc::channel();
        let worker = Worker::start(state.clone(), move |_| {
            begun.send(()).unwrap();
            blocked.recv().unwrap();
            Ok(vec![])
        })
        .unwrap();
        worker.submit(job(&state, 1));
        started.recv_timeout(Duration::from_secs(2)).unwrap();
        if stale {
            time.store(600_001, Ordering::SeqCst);
        } else {
            let mut s = state.lock().unwrap();
            let version = s.mode.generation();
            s.mode.request(Mode::Tracking, version, true).unwrap();
        }
        release.send(()).unwrap();
        wait(&state, 1);
        worker.stop().unwrap();
        let s = state.lock().unwrap();
        assert_eq!(s.counters.published, 0);
        assert_eq!(s.counters.dropped, 1);
        assert!(s.samples().is_empty());
        assert_eq!(s.drops().len(), 1);
        assert_eq!(
            s.drops()[0].stage,
            if stale {
                "publication_gate"
            } else {
                "mode_after"
            }
        );
        assert_eq!(s.drops()[0].sequence, 1);
    }
}
#[test]
fn inference_errors_and_panics_latch_hold_and_latest_is_fenced() {
    let (state, _clock, time) = setup();
    let worker = Worker::start(state.clone(), |_| Ok(vec![])).unwrap();
    worker.submit(job(&state, 1));
    wait(&state, 1);
    worker.stop().unwrap();
    assert!(state.lock().unwrap().latest().unwrap().is_some());
    time.store(600_001, Ordering::SeqCst);
    assert!(state.lock().unwrap().latest().unwrap().is_none());
    for panic in [false, true] {
        let (state, _clock, _) = setup();
        let worker = Worker::start(state.clone(), move |_| {
            if panic {
                panic!("inference bug");
            }
            Err("bad model")
        })
        .unwrap();
        worker.submit(job(&state, 1));
        wait(&state, 1);
        worker.stop().unwrap();
        let mut s = state.lock().unwrap();
        assert_eq!(s.counters.failures, 1);
        assert_eq!(s.mode.status().unwrap()["actuation_state"], "hold");
        assert_eq!(s.mode.status().unwrap()["reason"], "inference_failed");
    }
}

#[test]
fn reserve_and_cancelled_jobs_skip_native_work_and_invalid_payloads_are_bounded() {
    for cancelled in [false, true] {
        let (state, _clock, time) = setup();
        let worker = Worker::start(state.clone(), |_| panic!("must skip native work")).unwrap();
        let pending = job(&state, 1);
        if cancelled {
            pending.cancelled.store(true, Ordering::Release);
        } else {
            time.store(435_001, Ordering::SeqCst);
        }
        worker.submit(pending);
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while state.lock().unwrap().counters.dropped == 0 {
            assert!(std::time::Instant::now() < deadline);
            thread::sleep(Duration::from_millis(1));
        }
        let mut invalid = job(&state, 2);
        invalid.frame.jpeg = vec![0; MAX_JPEG + 1];
        assert_eq!(worker.submit(invalid), Submit::Invalid);
        worker.stop().unwrap();
        let s = state.lock().unwrap();
        assert_eq!(s.counters.inferred, 0);
        assert_eq!(
            s.drops()[0].stage,
            if cancelled {
                "cancelled_before"
            } else {
                "age_before"
            }
        );
        assert_eq!(s.drops()[0].compute_us, 0);
    }
}

#[test]
fn old_session_failure_cannot_degrade_a_new_acknowledged_mode() {
    let (state, _clock, _) = setup();
    let (begun, started) = mpsc::channel();
    let (release, blocked) = mpsc::channel();
    let worker = Worker::start(state.clone(), move |_| {
        begun.send(()).unwrap();
        blocked.recv().unwrap();
        Err("old session failure")
    })
    .unwrap();
    let job = job(&state, 1);
    let token = job.cancelled.clone();
    worker.submit(job);
    started.recv_timeout(Duration::from_secs(2)).unwrap();
    {
        let mut s = state.lock().unwrap();
        s.disconnect(&token).unwrap();
        s.mode.hello(1, 20).unwrap();
        let generation = s.mode.generation();
        s.mode.request(Mode::Tracking, generation, true).unwrap();
        let generation = s.mode.generation();
        s.mode
            .acknowledge(1, generation, Mode::Tracking, 1)
            .unwrap();
    }
    release.send(()).unwrap();
    wait(&state, 1);
    worker.stop().unwrap();
    let mut s = state.lock().unwrap();
    assert_eq!(s.counters.failures, 0);
    assert_eq!(s.counters.dropped, 1);
    assert_eq!(s.mode.requested(), Mode::Tracking);
    assert!(s.mode.status().unwrap()["reason"].is_null());
}
#[test]
fn invalid_output_is_never_published() {
    for kind in 0..3 {
        let (state, _clock, _) = setup();
        let worker = Worker::start(state.clone(), move |_| {
            Ok((0..if kind == 2 { 301 } else { 1 })
                .map(|_| Detection {
                    score: if kind == 0 { f32::NAN } else { 0.9 },
                    bbox: if kind == 1 {
                        [0.5, 0.2, 0.1, 0.4]
                    } else {
                        [0.1, 0.2, 0.3, 0.4]
                    },
                })
                .collect())
        })
        .unwrap();
        worker.submit(job(&state, 1));
        wait(&state, 1);
        worker.stop().unwrap();
        let s = state.lock().unwrap();
        assert_eq!(s.counters.failures, 1);
        assert_eq!(s.counters.published, 0);
    }
}
#[test]
fn fresh_evidence_cannot_be_paired_with_a_different_frame() {
    let (state, _, _) = setup();
    let worker = Worker::start(state.clone(), |_| {
        panic!("mismatched evidence must skip inference")
    })
    .unwrap();
    let original = job(&state, 2);
    for field in 0..5 {
        let mut mixed = job(&state, 2);
        match field {
            0 => mixed.frame.boot += 1,
            1 => mixed.frame.sequence -= 1,
            2 => mixed.frame.captured_us -= 1,
            3 => mixed.frame.sent_us += 1,
            _ => mixed.frame.generation = Some(99),
        }
        mixed.arrival = original.arrival;
        assert!(!mixed.arrival.fits(&mixed.frame, 100_000, 0));
        assert_eq!(worker.submit(mixed), Submit::Invalid);
    }
    worker.stop().unwrap();
    assert_eq!(state.lock().unwrap().counters.inferred, 0);
}
#[test]
fn sample_capacity_retains_first_evidence_and_counts_subsequent_loss() {
    let (state, _, _) = setup();
    state.lock().unwrap().samples = vec![
        Sample {
            boot: 1,
            generation: 11,
            sequence: 0,
            published_us: 0,
            age_high_us: 0,
            compute_us: 0
        };
        MAX_SAMPLES - 1
    ];
    let worker = Worker::start(state.clone(), |_| Ok(vec![])).unwrap();
    worker.submit(job(&state, 1));
    wait(&state, 1);
    {
        let s = state.lock().unwrap();
        assert_eq!(s.samples().len(), MAX_SAMPLES);
        assert_eq!(s.counters.evidence_lost, 0);
    }
    worker.submit(job(&state, 2));
    wait(&state, 2);
    worker.stop().unwrap();
    let mut s = state.lock().unwrap();
    assert_eq!(s.samples().len(), MAX_SAMPLES);
    assert_eq!(s.counters.evidence_lost, 1);
    assert_eq!(s.samples().last().unwrap().sequence, 1);
    assert_eq!(s.latest().unwrap().unwrap().frame.sequence, 2);
    assert_eq!(s.counters.published, 2);
}

#[test]
fn drop_diagnostics_are_bounded_metadata_and_do_not_alter_publication() {
    let (state, _, _) = setup();
    for sequence in 1..=40 {
        let pending = job(&state, sequence);
        state
            .lock()
            .unwrap()
            .dropped(&pending, "age_before", 435_001, 0);
    }
    let s = state.lock().unwrap();
    assert_eq!(s.counters.dropped, 40);
    assert_eq!(s.drops().len(), 32);
    assert_eq!(s.drops().front().unwrap().sequence, 9);
    assert_eq!(s.drops().back().unwrap().age_high_us, Some(335_001));
    assert_eq!(s.drops().back().unwrap().arrival_age_high_us, Some(0));
    assert_eq!(s.drops().back().unwrap().wait_us, Some(335_001));
    assert_eq!(s.drops().back().unwrap().arrival_age_low_us, Some(0));
    assert_eq!(s.drops().back().unwrap().capture_to_send_us, 0);
    assert!(s.samples().is_empty());
    assert_eq!(s.counters.published, 0);
}

#[test]
fn drop_metadata_separates_device_staging_from_arrival_uncertainty() {
    let (state, _, _) = setup();
    let mut pending = job(&state, 1);
    pending.frame.sent_us += 300_000;
    let mut clock = SessionClock::default();
    clock
        .poll(100_000, false, || Ok("0123456789abcdef".into()))
        .unwrap();
    clock.pong(1, "0123456789abcdef", 2, 100_000).unwrap();
    pending.arrival = clock.admit(&pending.frame, 400_000).unwrap();
    state
        .lock()
        .unwrap()
        .dropped(&pending, "age_before", 435_001, 0);
    let s = state.lock().unwrap();
    let event = &s.drops()[0];
    assert_eq!(event.capture_to_send_us, 300_000);
    assert_eq!(event.arrival_age_low_us, Some(300_000));
    assert_eq!(event.arrival_age_high_us, Some(300_000));
    assert_eq!(event.wait_us, Some(35_001));
}
