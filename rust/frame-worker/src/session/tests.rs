use super::*;
use crate::{pipeline::Clock, transport::Auth};
use serde_json::json;
use std::{
    net::{TcpListener, TcpStream},
    thread,
    time::{Duration, Instant},
};
use tungstenite::client::IntoClientRequest;

#[test]
fn expiry_crossed_while_waiting_for_state_is_fenced_before_controller() {
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    let time = Arc::new(AtomicU64::new(100_000));
    let copy = time.clone();
    let clock: Clock = Arc::new(move || copy.load(Ordering::SeqCst));
    let state = Arc::new(Mutex::new(
        State::new_with_tracking_recovery(10, clock, true).unwrap(),
    ));
    let worker = Worker::start(state.clone(), |_| Ok(vec![])).unwrap();
    let (mut client, mut server) = pair();
    let camera = thread::spawn(move || {
        client.send(Message::Text(json!({"type":"profile.hello","schema":2,"boot_id":1,"generation_floor":0,"profiles":["eco","tracking"]}).to_string().into())).unwrap();
        while let Ok(Message::Text(raw)) = client.read() {
            let v: Value = serde_json::from_str(&raw).unwrap();
            if v["type"]=="profile.set" && client.send(Message::Text(json!({"type":"profile.ack","boot_id":1,"generation":v["generation"],"mode":v["mode"],"applied_us":1}).to_string().into())).is_err() {break;}
            // Leave the initial clock exchange pending.
        }
    });
    let ready = AtomicBool::new(false);
    let advanced = AtomicBool::new(false);
    let outcome = thread::scope(|scope| {
        let owner = scope.spawn(|| {
            let receiver = Receiver::new(
                &worker,
                Limits {
                    duration_us: 4_000_000,
                    ..Limits::default()
                },
            )
            .unwrap()
            .with_clock_recovery();
            let mut generation = None;
            receiver.run(&mut server, |s, _| {
                if advanced.load(Ordering::SeqCst) {
                    return if s.mode.generation() > generation.unwrap() {
                        Ok(false)
                    } else {
                        Err("unfenced_controller")
                    };
                }
                if s.mode.status().unwrap()["acknowledged"] == true {
                    if generation.is_none() {
                        s.mode
                            .request(Mode::Tracking, s.mode.generation(), true)
                            .unwrap();
                        generation = Some(s.mode.generation());
                    } else {
                        ready.store(true, Ordering::SeqCst);
                    }
                }
                Ok(true)
            })
        });
        let deadline = Instant::now() + Duration::from_secs(2);
        while !ready.load(Ordering::SeqCst) {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(1));
        }
        let held = state.lock().unwrap();
        time.store(1_100_000, Ordering::SeqCst); // RTT exactly 1s is still valid.
        thread::sleep(Duration::from_millis(150)); // Receiver waits for State across expiry.
        time.store(1_100_001, Ordering::SeqCst);
        advanced.store(true, Ordering::SeqCst);
        drop(held);
        owner.join().unwrap()
    });
    assert_eq!(outcome.reason, "completed");
    assert_eq!(outcome.stats.clock_restarts, 1);
    drop(server);
    camera.join().unwrap();
    worker.stop().unwrap();
}

#[test]
fn late_pong_is_replaced_only_for_the_opt_in_session_over_a_real_socket() {
    for recover in [false, true] {
        let (state, worker, clock) = setup();
        let copy = clock.clone();
        state.lock().unwrap().mode =
            crate::operating_mode::OperatingMode::new_with_tracking_recovery(
                10,
                move || copy(),
                true,
            )
            .unwrap();
        let (mut client, mut server) = pair();
        let camera = thread::spawn(move || {
            client.send(Message::Text(json!({"type":"profile.hello","schema":2,"boot_id":1,"generation_floor":0,"profiles":["eco","tracking"]}).to_string().into())).unwrap();
            let (mut ack, mut tracking, mut synced, mut delayed, mut sequence) =
                (None, false, false, false, 0);
            while let Ok(Message::Text(raw)) = client.read() {
                let v: Value = serde_json::from_str(&raw).unwrap();
                let response = match v["type"].as_str().unwrap() {
                    "profile.set" => {
                        let applied = clock() + 1_000_000;
                        let generation = v["generation"].as_u64().unwrap();
                        ack = Some((generation, applied));
                        tracking = v["mode"] == "tracking";
                        json!({"type":"profile.ack","boot_id":1,"generation":generation,"mode":v["mode"],"applied_us":applied})
                    }
                    "clock.ping" => {
                        if tracking && sequence > 0 && !delayed {
                            delayed = true;
                            thread::sleep(Duration::from_millis(1150));
                        }
                        synced = true;
                        json!({"type":"clock.pong","boot_id":1,"id":v["id"],"device_us":clock()+1_000_000})
                    }
                    _ => panic!(),
                };
                if client
                    .send(Message::Text(response.to_string().into()))
                    .is_err()
                {
                    break;
                }
                if tracking
                    && synced
                    && let Some((generation, applied)) = ack
                {
                    sequence += 1;
                    let capture = (clock() + 1_000_000).max(applied + 1);
                    if client
                        .send(Message::Binary(
                            envelope(1, sequence, capture, generation).into(),
                        ))
                        .is_err()
                    {
                        break;
                    }
                }
            }
            delayed
        });
        let receiver = Receiver::new(
            &worker,
            Limits {
                duration_us: 4_000_000,
                ..Limits::default()
            },
        )
        .unwrap();
        let receiver = if recover {
            receiver.with_clock_recovery()
        } else {
            receiver
        };
        let mut tracking_generation = None;
        let outcome = receiver.run(&mut server, |s, _| {
            if tracking_generation.is_none() && s.mode.status().unwrap()["acknowledged"] == true {
                s.mode
                    .request(Mode::Tracking, s.mode.generation(), true)
                    .unwrap();
                tracking_generation = Some(s.mode.generation());
            }
            Ok(!s
                .samples()
                .iter()
                .any(|sample| Some(sample.generation) != tracking_generation))
        });
        assert_eq!(
            outcome.reason,
            if recover {
                "completed"
            } else {
                "clock_timeout"
            }
        );
        assert_eq!(outcome.stats.clock_restarts, u64::from(recover));
        assert_eq!(outcome.stats.retired_pongs, u64::from(recover));
        assert_eq!(outcome.final_status["actuation_state"], "hold"); // Full new cadence is still mandatory.
        assert!(!state.lock().unwrap().mode.connected());
        drop(server);
        assert!(camera.join().unwrap());
        worker.stop().unwrap();
    }
}

#[test]
fn clock_restart_budget_and_diagnostics_are_bounded() {
    use std::sync::atomic::{AtomicU64, Ordering};
    let time = Arc::new(AtomicU64::new(1));
    let copy = time.clone();
    let clock: Clock = Arc::new(move || copy.load(Ordering::SeqCst));
    let state = Arc::new(Mutex::new(
        State::new_with_tracking_recovery(10, clock, true).unwrap(),
    ));
    {
        let mut s = state.lock().unwrap();
        s.mode.hello(1, 0).unwrap();
        let g = s.mode.generation();
        s.mode.request(Mode::Tracking, g, true).unwrap();
    }
    let worker = Worker::start(state, |_| panic!("no inference")).unwrap();
    let receiver = Receiver::new(&worker, Limits::default())
        .unwrap()
        .with_clock_recovery();
    let mut clock = SessionClock::default();
    clock.bind(1).unwrap();
    let mut stats = Stats::default();
    for count in 0..129u64 {
        let now = 1 + count * 1_000_002;
        clock
            .poll(now, true, || Ok(format!("{count:016x}")))
            .unwrap();
        time.store(now + 1_000_001, Ordering::SeqCst);
        let mut s = receiver.state.lock().unwrap();
        let result = receiver.restart_clock(&mut s, &mut clock, now + 1_000_001, &mut stats);
        assert_eq!(
            result,
            if count < 128 {
                Ok(())
            } else {
                Err("clock_recovery_limit")
            }
        );
        if count == 128 {
            assert_eq!(s.mode.requested(), Mode::Eco);
        }
    }
    assert_eq!(stats.clock_restarts, 128);
    assert_eq!(stats.recent_clock_restarts.len(), 8);
    worker.stop().unwrap();
}

fn pair() -> (tungstenite::WebSocket<TcpStream>, Connection) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let owner = thread::spawn(move || {
        Connection::accept(
            listener.accept().unwrap().0,
            &Auth::new("cam", b"test-key").unwrap(),
        )
        .unwrap()
    });
    let socket = TcpStream::connect(addr).unwrap();
    socket
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let mut request = format!("ws://{addr}/ws/cameras/cam/live")
        .into_client_request()
        .unwrap();
    request
        .headers_mut()
        .insert("X-API-Key", "test-key".parse().unwrap());
    let client = tungstenite::client(request, socket).unwrap().0;
    (client, owner.join().unwrap())
}
fn setup() -> (Arc<Mutex<State>>, Worker, Clock) {
    setup_with(|_| Ok(vec![]))
}
fn setup_with(
    infer: impl FnMut(&[u8]) -> Result<Vec<crate::detector::Detection>> + Send + 'static,
) -> (Arc<Mutex<State>>, Worker, Clock) {
    let epoch = Instant::now();
    let clock: Clock = Arc::new(move || epoch.elapsed().as_micros() as u64 + 100_000);
    let state = Arc::new(Mutex::new(State::new(10, clock.clone()).unwrap()));
    let worker = Worker::start(state.clone(), infer).unwrap();
    (state, worker, clock)
}
fn envelope(boot: u64, seq: u64, capture: u64, generation: u64) -> Vec<u8> {
    let mut bytes = b"ECF2".to_vec();
    for n in [boot, seq, capture, capture, generation] {
        bytes.extend_from_slice(&n.to_be_bytes());
    }
    bytes.extend_from_slice(&1u32.to_be_bytes());
    bytes.push(1);
    bytes
}
#[test]
fn firmware_clock_hello_ack_and_result_cross_the_real_socket() {
    let (state, worker, clock) = setup();
    let (mut client, mut server) = pair();
    let camera = thread::spawn(move || {
        client.send(Message::Text(json!({"type":"profile.hello","schema":2,"boot_id":1,"generation_floor":0,"profiles":["eco","tracking"]}).to_string().into())).unwrap();
        let mut ack = None;
        let mut synced = false;
        let mut sent = false;
        while let Ok(Message::Text(raw)) = client.read() {
            let v: Value = serde_json::from_str(&raw).unwrap();
            match v["type"].as_str().unwrap() {
                "clock.ping" => {
                    client.send(Message::Text(json!({"type":"clock.pong","id":v["id"],"boot_id":1,"device_us":clock()+1_000_000}).to_string().into())).unwrap();
                    synced = true;
                }
                "profile.set" => {
                    let applied = clock() + 1_000_000;
                    let generation = v["generation"].as_u64().unwrap();
                    client.send(Message::Text(json!({"type":"profile.ack","boot_id":1,"generation":generation,"mode":v["mode"],"applied_us":applied}).to_string().into())).unwrap();
                    ack = Some((generation, applied));
                }
                _ => panic!(),
            }
            if synced
                && !sent
                && let Some((generation, applied)) = ack
            {
                let capture = (clock() + 1_000_000).max(applied + 1);
                client
                    .send(Message::Binary(envelope(1, 1, capture, generation).into()))
                    .unwrap();
                sent = true;
            }
        }
    });
    let receiver = Receiver::new(
        &worker,
        Limits {
            duration_us: 2_000_000,
            ..Limits::default()
        },
    )
    .unwrap();
    let outcome = receiver.run(&mut server, |s, _| Ok(s.counters.published == 0));
    assert_eq!(outcome.reason, "completed");
    assert_eq!(outcome.stats.admitted, 1);
    assert_eq!(outcome.stats.recent_receipts.len(), 1);
    assert_eq!(outcome.stats.recent_receipts[0].sequence, 1);
    assert!(outcome.stats.recent_receipts[0].received_us >= 100_000);
    assert!(
        outcome.stats.recent_receipts[0]
            .ws_complete_unix_us
            .is_some()
    );
    assert!(outcome.final_status["acknowledged"].as_bool().unwrap());
    assert!(!state.lock().unwrap().mode.connected());
    drop(server);
    camera.join().unwrap();
    worker.stop().unwrap();
}
#[test]
fn concurrent_session_cannot_disconnect_the_existing_owner() {
    let (state, worker, _) = setup();
    let (client, mut server) = pair();
    let token = state.lock().unwrap().claim_session().unwrap();
    let receiver = Receiver::new(&worker, Limits::default()).unwrap();
    let outcome = receiver.run(&mut server, |_, _| panic!("busy controller"));
    assert_eq!(outcome.reason, "camera_busy");
    assert!(!token.load(std::sync::atomic::Ordering::Acquire));
    state.lock().unwrap().release_session(&token);
    assert!(token.load(std::sync::atomic::Ordering::Acquire));
    let next = state.lock().unwrap().claim_session().unwrap();
    state.lock().unwrap().release_session(&token);
    assert!(!next.load(std::sync::atomic::Ordering::Acquire));
    state.lock().unwrap().release_session(&next);
    drop(client);
    drop(server);
    worker.stop().unwrap();
}
#[test]
fn malformed_nonce_and_message_rate_close_and_release_session() {
    for flood in [false, true] {
        let (state, worker, _) = setup();
        let (mut client, mut server) = pair();
        let camera = thread::spawn(move || {
            if flood {
                for _ in 0..3 {
                    client.send(Message::Ping(vec![].into())).unwrap();
                }
            } else {
                client.send(Message::Text(json!({"type":"clock.pong","id":"GGGGGGGGGGGGGGGG","boot_id":1,"device_us":1}).to_string().into())).unwrap();
            }
            while client.read().is_ok() {}
        });
        let receiver = Receiver::new(
            &worker,
            Limits {
                rate: 2,
                ..Limits::default()
            },
        )
        .unwrap();
        let outcome = receiver.run(&mut server, |_, _| Ok(true));
        assert_eq!(
            outcome.reason,
            if flood { "message_rate" } else { "clock_nonce" }
        );
        let token = state.lock().unwrap().claim_session().unwrap();
        state.lock().unwrap().release_session(&token);
        drop(server);
        camera.join().unwrap();
        worker.stop().unwrap();
    }
}

#[test]
fn limits_and_legacy_frames_fail_closed_with_a_reusable_lease() {
    for legacy in [false, true] {
        let (state, worker, _) = setup();
        let (mut client, mut server) = pair();
        let camera = thread::spawn(move || {
            if legacy {
                let mut raw = envelope(1, 1, 1, 1);
                raw[..4].copy_from_slice(b"ECF1");
                raw.drain(36..44);
                client.send(Message::Binary(raw.into())).unwrap();
            }
            while client.read().is_ok() {}
        });
        let limits = Limits {
            duration_us: 10_000,
            ..Limits::default()
        };
        for invalid in [
            Limits {
                duration_us: 0,
                ..limits
            },
            Limits {
                duration_us: 3_720_000_001,
                ..limits
            },
            Limits {
                frames: 0,
                ..limits
            },
            Limits {
                frames: 50_001,
                ..limits
            },
            Limits {
                messages: 0,
                ..limits
            },
            Limits {
                messages: 200_001,
                ..limits
            },
            Limits { rate: 0, ..limits },
            Limits {
                rate: 101,
                ..limits
            },
        ] {
            assert!(Receiver::new(&worker, invalid).is_err());
        }
        let receiver = Receiver::new(&worker, limits).unwrap();
        let outcome = receiver.run(&mut server, |_, _| Ok(true));
        assert_eq!(
            outcome.reason,
            if legacy {
                "profile_required"
            } else {
                "duration_limit"
            }
        );
        assert_eq!(state.lock().unwrap().counters.inferred, 0);
        let token = state.lock().unwrap().claim_session().unwrap();
        state.lock().unwrap().release_session(&token);
        drop(server);
        camera.join().unwrap();
        worker.stop().unwrap();
    }
}

#[test]
fn controller_panic_closes_socket_without_poisoning_or_leaking_the_lease() {
    let (state, worker, _) = setup();
    let (mut client, mut server) = pair();
    let receiver = Receiver::new(&worker, Limits::default()).unwrap();
    let outcome = receiver.run(&mut server, |_, _| panic!("controller bug"));
    assert_eq!(outcome.reason, "controller_failed");
    assert!(client.read().is_err());
    assert_eq!(outcome.final_status["reason"], "controller_failed");
    let token = state.lock().unwrap().claim_session().unwrap();
    state.lock().unwrap().release_session(&token);
    worker.stop().unwrap();
}
#[test]
fn ending_session_fences_inflight_result_before_status_and_lease_release() {
    use std::sync::mpsc;
    let (begun, started) = mpsc::channel();
    let (release, blocked) = mpsc::channel();
    let (state, worker, clock) = setup_with(move |_| {
        begun.send(()).unwrap();
        blocked.recv().unwrap();
        Ok(vec![])
    });
    let token = state.lock().unwrap().claim_session().unwrap();
    let generation = {
        let mut s = state.lock().unwrap();
        s.mode.hello(1, 0).unwrap();
        let generation = s.mode.generation();
        s.mode.acknowledge(1, generation, Mode::Eco, 1).unwrap();
        generation
    };
    let mut evidence = SessionClock::default();
    let at = clock();
    let device = at + 1_000_000;
    evidence
        .poll(at, false, || Ok("0123456789abcdef".into()))
        .unwrap();
    evidence
        .pong(1, "0123456789abcdef", device, clock())
        .unwrap();
    let frame = crate::Frame {
        boot: 1,
        sequence: 1,
        captured_us: device + 1,
        sent_us: device + 1,
        generation: Some(generation),
        jpeg: vec![1],
    };
    let arrival = evidence.admit(&frame, clock()).unwrap();
    // LatestSlot submission is intentionally nonblocking: an empty queue can
    // briefly return Busy while the worker checks it. Do not silently lose the job.
    let submit_deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match worker.submit(Job {
            frame: crate::Frame {
                jpeg: vec![1],
                ..frame
            },
            arrival,
            cancelled: token.clone(),
        }) {
            Submit::Queued => break,
            Submit::Busy => {
                assert!(Instant::now() < submit_deadline);
                thread::sleep(Duration::from_millis(1));
            }
            other => panic!("unexpected submission: {other:?}"),
        }
    }
    started.recv_timeout(Duration::from_secs(2)).unwrap();
    let receiver = Receiver::new(&worker, Limits::default()).unwrap();
    let snapshot = receiver.snapshot_and_cancel(&token);
    assert!(snapshot["acknowledged"].as_bool().unwrap());
    assert!(state.lock().unwrap().claim_session().is_err());
    release.send(()).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    while state.lock().unwrap().counters.inferred == 0 {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(1));
    }
    assert_eq!(state.lock().unwrap().counters.published, 0);
    assert_eq!(state.lock().unwrap().counters.dropped, 1);
    state.lock().unwrap().release_session(&token);
    worker.stop().unwrap();
}

#[test]
fn receive_stamp_does_not_wait_for_the_mode_mutex() {
    let (state, worker, _) = setup();
    let (mut client, mut server) = pair();
    let receiver = Receiver::new(&worker, Limits::default()).unwrap();
    let guard = state.lock().unwrap();
    client.send(Message::Text("payload".into())).unwrap();
    let (sent, received) = std::sync::mpsc::channel();
    thread::scope(|scope| {
        scope.spawn(|| {
            let mut stats = Stats::default();
            let message = receiver.receive(&mut server, &mut stats).unwrap();
            sent.send((message, stats.max_receive_call_us)).unwrap();
        });
        let result = received.recv_timeout(Duration::from_secs(1));
        drop(guard); // Release before asserting, so a regression cannot deadlock the test.
        let (message, elapsed) = result.expect("read receipt must not acquire the state lock");
        assert!(
            matches!(message, Some((Message::Text(text), stamp)) if text=="payload" && stamp>=100_000)
        );
        assert!(elapsed < 1_000_000);
    });
    worker.stop().unwrap();
}

#[test]
fn receipt_metadata_is_bounded_and_does_not_require_wall_time() {
    let mut stats = Stats::default();
    for sequence in 1..=40 {
        stats.receipt(sequence, sequence * 1000, None);
    }
    assert_eq!(stats.recent_receipts.len(), 32);
    assert_eq!(stats.recent_receipts.front().unwrap().sequence, 9);
    assert_eq!(stats.recent_receipts.back().unwrap().received_us, 40_000);
    assert_eq!(
        stats.recent_receipts.back().unwrap().ws_complete_unix_us,
        None
    );
    assert_eq!(stats.admitted, 0);
}

#[test]
fn rejection_diagnostics_are_bounded_and_preserve_clock_gate() {
    let mut clock = SessionClock::default();
    let mut stats = Stats::default();
    let mut frame = crate::decode(&envelope(1, 1, 1, 1), true).unwrap();
    stats.reject(&frame, 2, "clock", &clock);
    assert_eq!(
        stats.recent_rejections.back().unwrap().stage,
        "clock_unavailable"
    );
    for i in 1..=12 {
        let at = i * 2_000_000;
        clock
            .poll(at, false, || Ok("0123456789abcdef".into()))
            .unwrap();
        clock.pong(1, "0123456789abcdef", at, at + 10).unwrap();
    }
    frame.captured_us = 23_999_990;
    frame.sent_us = 24_000_000;
    let before = clock
        .admit(&frame, 24_000_020)
        .unwrap()
        .age_bounds(&frame, 24_000_020);
    for seq in 1..=40 {
        frame.sequence = seq;
        stats.reject(&frame, 24_000_020, "order", &clock);
    }
    assert_eq!(stats.dropped, 41);
    assert_eq!(stats.recent_rejections.len(), 32);
    let first = stats.recent_rejections.front().unwrap();
    assert_eq!(first.sequence, 9);
    assert_eq!(first.clock_exchanges.len(), 8);
    assert_eq!(
        first.clock_exchanges[0],
        [1, 10_000_000, 10_000_000, 10_000_010]
    );
    assert_eq!(first.age_bounds, before);
    assert_eq!(
        clock
            .admit(&frame, 24_000_020)
            .unwrap()
            .age_bounds(&frame, 24_000_020),
        before
    );
    frame.captured_us = 23_000_000;
    frame.sent_us = 23_000_000;
    assert!(clock.admit(&frame, 24_000_020).is_none());
    stats.reject(&frame, 24_000_020, "clock", &clock);
    assert_eq!(stats.recent_rejections.back().unwrap().stage, "age_limit");
    assert!(clock.admit(&frame, 24_000_020).is_none());
    stats.reject(&frame, 24_000_020, "mode", &clock);
    assert_eq!(stats.recent_rejections.back().unwrap().stage, "mode");
}
