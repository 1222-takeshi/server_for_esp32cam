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
fn reused_state_does_not_count_previous_sessions_as_lost_diagnostics() {
    let mut state = State::new_with_tracking_recovery(10, Arc::new(|| 100_000), true).unwrap();
    state.mode.hello(1, 0).unwrap();
    state
        .mode
        .request(Mode::Tracking, state.mode.generation(), true)
        .unwrap();
    state.mode.protect("frames_expired").unwrap();
    state
        .mode
        .request(Mode::Eco, state.mode.generation(), true)
        .unwrap();
    let mut stats = Stats {
        initial_hold_events: state.mode.tracking_hold_events(),
        ..Stats::default()
    };
    state
        .mode
        .request(Mode::Tracking, state.mode.generation(), true)
        .unwrap();
    state.mode.protect("frames_expired").unwrap();
    stats.note_hold(&state, &VecDeque::new());
    stats.finish_diagnostics(&state);
    assert_eq!(stats.hold_diagnostics_lost, 0);
    state
        .mode
        .request(Mode::Eco, state.mode.generation(), true)
        .unwrap();
    state
        .mode
        .request(Mode::Tracking, state.mode.generation(), true)
        .unwrap();
    state.mode.protect("frames_expired").unwrap();
    state
        .mode
        .request(Mode::Eco, state.mode.generation(), true)
        .unwrap();
    stats.finish_diagnostics(&state);
    assert_eq!(stats.hold_diagnostics_lost, 1);
}

#[test]
fn exit_snapshot_and_worker_shutdown_reconcile_diagnostics() {
    let (state, worker, clock) = setup();
    {
        let mut s = state.lock().unwrap();
        s.mode = crate::operating_mode::OperatingMode::new_with_tracking_recovery(
            10,
            move || clock(),
            true,
        )
        .unwrap();
        s.mode.hello(1, 0).unwrap();
        let g = s.mode.generation();
        s.mode.request(Mode::Tracking, g, true).unwrap();
        s.mode.protect("frames_expired").unwrap();
    }
    let mut stats = Stats::default();
    let receiver = Receiver::new(&worker, Limits::default()).unwrap();
    let token = AtomicBool::new(false);
    receiver.snapshot_and_cancel(&token, &mut stats, &VecDeque::new());
    assert!(token.load(std::sync::atomic::Ordering::Acquire));
    assert_eq!(stats.hold_diagnostics.0.len(), 1);
    assert!(stats.hold_diagnostics.report()[0]["after"].is_null());
    worker.stop().unwrap();
    let mut s = state.lock().unwrap();
    // A transient hold closed entirely inside an aborting controller cannot
    // be reconstructed, but it must never be silently omitted from loss count.
    let g = s.mode.generation();
    s.mode.request(Mode::Eco, g, true).unwrap();
    let g = s.mode.generation();
    s.mode.request(Mode::Tracking, g, true).unwrap();
    s.mode.protect("frames_expired").unwrap();
    let g = s.mode.generation();
    s.mode.request(Mode::Eco, g, true).unwrap();
    stats.finish_diagnostics(&s);
    assert_eq!(stats.hold_diagnostics_lost, 1);
    assert_eq!(
        stats.hold_diagnostics.report()[0]["after_source"],
        "worker_shutdown"
    );
    assert!(std::mem::size_of::<HoldDiagnostic>() * 128 < 2 * 1024 * 1024);
}

#[test]
fn hold_diagnostic_retains_first_observation_after_inflight_generation_fence() {
    use crate::pipeline::{Job, Submit};
    use std::sync::{
        atomic::{AtomicU64, Ordering},
        mpsc,
    };
    let time = Arc::new(AtomicU64::new(100_000));
    let copy = time.clone();
    let state = Arc::new(Mutex::new(
        State::new_with_tracking_recovery(10, Arc::new(move || copy.load(Ordering::SeqCst)), true)
            .unwrap(),
    ));
    let generation = {
        let mut s = state.lock().unwrap();
        s.mode.hello(1, 0).unwrap();
        let current = s.mode.generation();
        s.mode.request(Mode::Tracking, current, true).unwrap();
        let g = s.mode.generation();
        s.mode.acknowledge(1, g, Mode::Tracking, 1).unwrap();
        g
    };
    let (begun, started) = mpsc::channel();
    let (release, blocked) = mpsc::channel();
    let worker = Worker::start(state.clone(), move |_| {
        begun.send(()).unwrap();
        blocked.recv().unwrap();
        Ok(vec![])
    })
    .unwrap();
    let frame = decode(&envelope(1, 1, 1000, generation), true).unwrap();
    let mut clock = SessionClock::default();
    clock
        .poll(100_000, true, || Ok("0123456789abcdef".into()))
        .unwrap();
    clock.pong(1, "0123456789abcdef", 1000, 100_000).unwrap();
    let arrival = clock.admit(&frame, 100_000).unwrap();
    // `Worker::submit` is nonblocking (`try_lock`) and can report Busy while the worker
    // thread polls the empty slot; retry until the submission is decided.
    let hang_guard = Instant::now() + Duration::from_secs(30);
    let mut pending = Some(frame);
    loop {
        let frame = pending.take().unwrap();
        let retry = crate::Frame {
            jpeg: frame.jpeg.clone(),
            ..frame
        };
        match worker.submit(Job {
            frame,
            arrival,
            cancelled: Arc::new(AtomicBool::new(false)),
        }) {
            Submit::Queued => break,
            Submit::Busy => {
                assert!(Instant::now() < hang_guard);
                thread::yield_now();
                pending = Some(retry);
            }
            other => panic!("unexpected submission: {other:?}"),
        }
    }
    started.recv_timeout(Duration::from_secs(2)).unwrap();
    let mut stats = Stats::default();
    let io = VecDeque::new();
    {
        let mut s = state.lock().unwrap();
        time.store(150_000, Ordering::SeqCst);
        s.mode.protect("frames_expired").unwrap();
        stats.note_hold(&s, &io);
        stats.note_hold(&s, &io);
        assert!(stats.hold_diagnostics.report()[0]["after"].is_null());
        assert_eq!(stats.hold_diagnostics.report()[0]["watched_sequence"], 1);
    }
    time.store(200_000, Ordering::SeqCst);
    release.send(()).unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        let s = state.lock().unwrap();
        if s.counters.inferred == 1 {
            stats.note_hold(&s, &io);
            break;
        }
        drop(s);
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(1));
    }
    worker.stop().unwrap();
    let after = stats.hold_diagnostics.report()[0]["after"].clone();
    assert_eq!(after["recent_work"][0]["sequence"], 1);
    assert_eq!(after["recent_work"][0]["outcome"], "mode_after");
    assert_eq!(after["recent_work"][0]["finished_us"], 200_000);
    time.store(250_000, Ordering::SeqCst);
    stats.note_hold(&state.lock().unwrap(), &io);
    assert_eq!(stats.hold_diagnostics.report()[0]["after"], after);
    assert_eq!(stats.hold_diagnostics_lost, 0);
}

#[test]
fn hold_diagnostics_are_once_per_hold_bounded_and_metadata_only() {
    let mut state = State::new_with_tracking_recovery(10, Arc::new(|| 100_000), true).unwrap();
    state.mode.hello(1, 0).unwrap();
    let mut stats = Stats::default();
    let io: VecDeque<_> = (0..40)
        .map(|i| transport::IoSample {
            kind: "read",
            status: "ok",
            bytes: i,
            elapsed_us: Some(i as u64),
            observed_unix_us: Some(i as u64),
        })
        .collect();
    for i in 0..40 {
        stats.receipt(i, i, Some(i));
    }
    stats.note_hold(&state, &io);
    assert!(stats.hold_diagnostics.0.is_empty()); // Eco is never a tracking hold.
    for i in 0..130 {
        state
            .mode
            .request(Mode::Tracking, state.mode.generation(), true)
            .unwrap();
        state.mode.protect("frames_expired").unwrap();
        let generation = state.mode.generation();
        stats.note_hold(&state, &io);
        stats.note_hold(&state, &io); // Polling must not multiply records or loss count.
        assert_eq!(stats.hold_diagnostics.0.len(), (i + 1).min(128));
        assert_eq!(
            stats.hold_diagnostics_lost,
            (i + 1).saturating_sub(128) as u64
        );
        assert_eq!(state.mode.generation(), generation);
        state.mode.request(Mode::Eco, generation, true).unwrap();
    }
    let first = &stats.hold_diagnostics.report()[0];
    assert_eq!(first["receipts"].as_array().unwrap().len(), 8);
    assert_eq!(first["receipts"][0]["sequence"], 32);
    assert_eq!(first["io"].as_array().unwrap().len(), 8);
    assert_eq!(first["io"][0]["bytes"], 32);
    assert!(first["pipeline"]["in_flight"].is_null());
    assert_eq!(first["reason"], "frames_expired");
    assert!(!first["after"].is_null());
    let raw = serde_json::to_string(&stats.hold_diagnostics.report()).unwrap();
    assert!(raw.len() < 512 * 1024);
    for forbidden in ["test-key", "x-api-key", "jpeg", "nonce"] {
        assert!(!raw.contains(forbidden));
    }
}

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
/// The receiver stores the clock's protection view on every pass and notes each
/// accepted input; removing either leaves the online checks blind.
#[test]
fn every_receiver_pass_refreshes_the_protection_view_and_accepted_input_is_noted() {
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
    // What each controller pass finds in the stored view: (last sync, input).
    let mut passes: Vec<(Option<u64>, Option<u64>)> = vec![];
    let outcome = receiver.run(&mut server, |s, _| {
        passes.push((
            s.protection.last_sync_us,
            s.protection.input.map(|i| i.received_us),
        ));
        Ok(s.counters.published == 0)
    });
    assert_eq!(outcome.reason, "completed");
    // Before the first reply nothing is known; later passes see the sync time
    // without any other trigger than the pass itself.
    assert_eq!(passes[0], (None, None));
    assert!(passes.iter().any(|p| p.0.is_some() && p.1.is_none()));
    // The accepted frame is noted at arrival and seen by the next pass.
    let noted = passes.last().unwrap().1;
    assert!(noted.is_some_and(|at| at >= 100_000), "{passes:?}");
    assert_eq!(
        passes.last().unwrap().1,
        state
            .lock()
            .unwrap()
            .protection
            .input
            .map(|i| i.received_us)
    );
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
    let snapshot = receiver.snapshot_and_cancel(&token, &mut Stats::default(), &VecDeque::new());
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

// ---- R3 frame recording (v2 spec D7): recording must not change a decision ----

#[derive(Clone, Copy)]
enum Kind {
    Normal,
    Duplicate,
    WrongGeneration,
    Stale,
    Late,
}
struct Step {
    at_us: u64,
    frames: &'static [Kind],
    compute_us: u64,
}
#[derive(Debug, PartialEq)]
struct Decisions {
    reason: &'static str,
    exit_code: i32,
    abort: Option<(&'static str, u64)>,
    counts: [u64; 6],
    rejections: Vec<(u64, &'static str, u64)>,
    published: Vec<(u64, u64, u64)>,
    drops: Vec<(u64, &'static str, u64)>,
    worker: [u64; 3],
    holds: (u64, usize, u64),
    status: [String; 4],
}
struct Run {
    decisions: Decisions,
    stats: Stats,
    work: Option<crate::frame_log::Ring<crate::frame_log::WorkRecord>>,
}

/// Deterministic camera: it speaks only when the receiver's controller says so,
/// and every time it reads is the injected clock, which stands still while a
/// step is being decided.
fn lockstep_camera(
    mut client: tungstenite::WebSocket<TcpStream>,
    time: Arc<std::sync::atomic::AtomicU64>,
    go: std::sync::mpsc::Receiver<&'static [Kind]>,
    acked: Arc<std::sync::atomic::AtomicU64>,
) {
    use std::sync::atomic::Ordering;
    client
        .get_mut()
        .set_read_timeout(Some(Duration::from_millis(10)))
        .unwrap();
    client.send(Message::Text(json!({"type":"profile.hello","schema":2,"boot_id":1,"generation_floor":0,"profiles":["eco","tracking"]}).to_string().into())).unwrap();
    let (mut generation, mut applied, mut sequence, mut last) = (0u64, 0u64, 0u64, 0u64);
    loop {
        if let Ok(kinds) = go.try_recv() {
            let device = time.load(Ordering::SeqCst) + 1_000_000;
            for kind in kinds {
                let fresh = (device - 20_000).max(applied + 1).max(last + 1);
                let (seq, capture, gen_) = match kind {
                    Kind::Normal => {
                        sequence += 1;
                        (sequence, fresh, generation)
                    }
                    Kind::Duplicate => (sequence, fresh, generation),
                    Kind::WrongGeneration => {
                        sequence += 1;
                        (sequence, fresh, generation + 5)
                    }
                    // 300 ms old: accepted and started, but too old once computed.
                    Kind::Late => {
                        sequence += 1;
                        (
                            sequence,
                            (device - 300_000).max(applied + 1).max(last + 1),
                            generation,
                        )
                    }
                    // Newer than everything before it, but 700 ms old.
                    Kind::Stale => {
                        sequence += 1;
                        (sequence, (device - 700_000).max(applied + 1), generation)
                    }
                };
                if !matches!(kind, Kind::Duplicate) {
                    last = capture;
                }
                if client
                    .send(Message::Binary(envelope(1, seq, capture, gen_).into()))
                    .is_err()
                {
                    return;
                }
            }
            continue;
        }
        match client.read() {
            Ok(Message::Text(raw)) => {
                let v: Value = serde_json::from_str(&raw).unwrap();
                let reply = match v["type"].as_str().unwrap() {
                    "profile.set" => {
                        generation = v["generation"].as_u64().unwrap();
                        applied = time.load(Ordering::SeqCst) + 1_000_000;
                        json!({"type":"profile.ack","boot_id":1,"generation":generation,"mode":v["mode"],"applied_us":applied})
                    }
                    "clock.ping" => {
                        json!({"type":"clock.pong","boot_id":1,"id":v["id"],"device_us":time.load(Ordering::SeqCst)+1_000_000})
                    }
                    _ => panic!(),
                };
                if client
                    .send(Message::Text(reply.to_string().into()))
                    .is_err()
                {
                    return;
                }
                if v["type"] == "profile.set" {
                    acked.store(generation, Ordering::SeqCst);
                }
            }
            Ok(_) => {}
            Err(tungstenite::Error::Io(e))
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) => {}
            Err(_) => return,
        }
    }
}

/// Runs the same synthetic time series through a real socket. The controller
/// advances the clock only after the previous step is decided and its clock
/// exchange has settled, so every decision is a function of the series alone.
fn run_lockstep(
    steps: &'static [Step],
    interval: Option<(u64, u64)>,
    record: Option<usize>,
) -> Run {
    use crate::online_abort::{self, OnlineGuard};
    use std::sync::{
        atomic::{AtomicU64, Ordering},
        mpsc,
    };
    #[derive(Clone, Copy)]
    enum Phase {
        Init,
        Ack(u64),
        Advance(usize),
        Settle(usize, u32),
        Resolve(usize, u64),
    }
    let time = Arc::new(AtomicU64::new(100_000));
    let compute = Arc::new(AtomicU64::new(0));
    let clock: Clock = {
        let t = time.clone();
        Arc::new(move || t.load(Ordering::SeqCst))
    };
    let state = Arc::new(Mutex::new(
        State::new_with_tracking_recovery(10, clock, true).unwrap(),
    ));
    let (t, c) = (time.clone(), compute.clone());
    let worker = Worker::start(state.clone(), move |_| {
        t.fetch_add(c.load(Ordering::SeqCst), Ordering::SeqCst);
        Ok(vec![])
    })
    .unwrap();
    let (client, mut server) = pair();
    let (go_tx, go_rx) = mpsc::channel();
    let acked = Arc::new(AtomicU64::new(0));
    let camera = {
        let (t, a) = (time.clone(), acked.clone());
        thread::spawn(move || lockstep_camera(client, t, go_rx, a))
    };
    let mut receiver = Receiver::new(
        &worker,
        Limits {
            duration_us: 600_000_000,
            ..Limits::default()
        },
    )
    .unwrap()
    .with_clock_recovery();
    if let Some(capacity) = record {
        receiver = receiver.with_frame_log(capacity).unwrap();
    }
    let (mut phase, mut guard, mut abort) = (Phase::Init, OnlineGuard::new(600_000_000), None);
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut outcome = receiver.run(&mut server, |s, _| {
        if Instant::now() > deadline {
            return Err("test_timeout");
        }
        let mut more = true;
        phase = match phase {
            Phase::Init if s.mode.status().unwrap()["acknowledged"] == true => {
                s.mode
                    .request(Mode::Tracking, s.mode.generation(), true)
                    .unwrap();
                Phase::Ack(s.mode.generation())
            }
            Phase::Ack(g) if acked.load(Ordering::SeqCst) == g => Phase::Advance(0),
            Phase::Advance(k) => {
                time.store(steps[k].at_us, Ordering::SeqCst);
                compute.store(steps[k].compute_us, Ordering::SeqCst);
                Phase::Settle(k, 0)
            }
            // One pass at the new time polls the clock; the next one sees
            // whether that exchange is still open.
            Phase::Settle(k, n) if n >= 2 && s.protection.pending_since_us.is_none() => {
                let target = s.counters.published + s.counters.dropped + 1;
                go_tx.send(steps[k].frames).unwrap();
                Phase::Resolve(k, target)
            }
            Phase::Settle(k, n) => Phase::Settle(k, n + 1),
            Phase::Resolve(k, target) if s.counters.published + s.counters.dropped >= target => {
                if k + 1 == steps.len() {
                    more = false;
                    Phase::Resolve(k, target)
                } else {
                    Phase::Advance(k + 1)
                }
            }
            other => other,
        };
        let now = s.now();
        match guard.observe(&online_abort::observation(
            now,
            interval.filter(|i| now >= i.0),
            s,
        )) {
            Some(code) => {
                abort = Some((code, now));
                Err(code)
            }
            None => Ok(more),
        }
    });
    worker.stop().unwrap();
    let mut s = state.lock().unwrap();
    outcome.stats.finish_diagnostics(&s);
    let st = &outcome.final_status;
    let decisions = Decisions {
        reason: outcome.reason,
        exit_code: online_abort::exit_code(outcome.reason == "completed", outcome.reason),
        abort,
        counts: [
            outcome.stats.frames,
            outcome.stats.admitted,
            outcome.stats.dropped,
            outcome.stats.overwritten,
            outcome.stats.queue_busy,
            outcome.stats.rejected_ack,
        ],
        rejections: outcome
            .stats
            .recent_rejections
            .iter()
            .map(|r| (r.sequence, r.stage, r.received_us))
            .collect(),
        // The age bound can differ by microseconds when a clock exchange lands
        // between the clock step and the publication; it is bounded instead.
        published: s
            .samples()
            .iter()
            .map(|v| {
                assert!(v.age_high_us <= 500_000);
                (v.sequence, v.published_us, v.compute_us)
            })
            .collect(),
        drops: s
            .drops()
            .iter()
            .map(|d| (d.sequence, d.stage, d.at_us))
            .collect(),
        worker: [
            s.counters.inferred,
            s.counters.published,
            s.counters.dropped,
        ],
        holds: (
            s.mode.tracking_hold_events(),
            outcome.stats.hold_diagnostics.report().len(),
            outcome.stats.hold_diagnostics_lost,
        ),
        status: [
            "acknowledged",
            "requested_mode",
            "actuation_state",
            "reason",
        ]
        .map(|k| st[k].to_string()),
    };
    let work = s.take_work_log();
    drop(s);
    drop(server);
    camera.join().unwrap();
    Run {
        decisions,
        stats: outcome.stats,
        work,
    }
}

static MIXED: [Step; 6] = [
    // The late result would be 550 ms old when it is ready: refused at publication.
    Step {
        at_us: 1_000_000,
        frames: &[Kind::Stale, Kind::Late],
        compute_us: 250_000,
    },
    Step {
        at_us: 1_400_000,
        frames: &[Kind::Normal],
        compute_us: 30_000,
    },
    Step {
        at_us: 1_600_000,
        frames: &[Kind::Duplicate, Kind::Normal],
        compute_us: 20_000,
    },
    Step {
        at_us: 1_800_000,
        frames: &[Kind::WrongGeneration, Kind::Normal],
        compute_us: 20_000,
    },
    Step {
        at_us: 2_000_000,
        frames: &[Kind::Normal],
        compute_us: 20_000,
    },
    // After the gap the previous result is stale: a hold is declared.
    Step {
        at_us: 2_900_000,
        frames: &[Kind::Normal],
        compute_us: 20_000,
    },
];
static STALLED: [Step; 3] = [
    Step {
        at_us: 1_000_000,
        frames: &[Kind::Normal],
        compute_us: 20_000,
    },
    Step {
        at_us: 1_200_000,
        frames: &[Kind::Normal],
        compute_us: 20_000,
    },
    // The clock exchange is 11 s old: A14 (sync age above 10 s).
    Step {
        at_us: 12_300_000,
        frames: &[Kind::Normal],
        compute_us: 20_000,
    },
];

#[test]
fn recording_on_and_off_make_identical_decisions() {
    for (steps, interval) in [
        (&MIXED[..], None),
        (&STALLED[..], Some((12_000_000, 612_000_000))),
    ] {
        // `run_lockstep` takes 'static steps; the statics above outlive the test.
        let steps: &'static [Step] = steps;
        let off = run_lockstep(steps, interval, None);
        let on = run_lockstep(steps, interval, Some(1000));
        assert_eq!(off.decisions, on.decisions);
        assert!(off.stats.frame_log.is_none() && off.work.is_none());
        let log = on.stats.frame_log.as_ref().unwrap();
        assert_eq!(log.items().len() as u64, on.decisions.counts[0]);
        assert_eq!(log.lost(), 0);
        for r in log.items() {
            let read = r.read;
            assert!(read.tcp.is_none());
            // A frame already buffered by the read of the previous one needs no
            // read of its own: its byte times are unknown (null), not guessed.
            assert_eq!(read.reads == 0, read.first_byte_us.is_none());
            assert_eq!(read.first_byte_us.is_none(), read.last_byte_us.is_none());
            assert!(read.first_byte_us <= read.last_byte_us);
            assert!(read.last_byte_us.is_none_or(|t| t <= r.received_us));
            assert!(r.bytes > 0);
        }
        assert_eq!(
            on.work.as_ref().unwrap().items().len() as u64,
            on.decisions.worker[0]
        );
        // A buffer that is too small loses records and changes nothing else.
        let small = run_lockstep(steps, interval, Some(2));
        assert_eq!(off.decisions, small.decisions);
        let lost = small.stats.frame_log.as_ref().unwrap();
        assert_eq!(lost.items().len(), 2.min(off.decisions.counts[0] as usize));
        assert_eq!(lost.lost(), off.decisions.counts[0].saturating_sub(2));
        assert_eq!(
            small.work.as_ref().unwrap().lost(),
            off.decisions.worker[0].saturating_sub(2)
        );
        // The scenarios must exercise what they claim to.
        let d = &off.decisions;
        if interval.is_none() {
            assert_eq!(d.reason, "completed");
            assert_eq!(d.exit_code, 0);
            let stages: Vec<_> = d.rejections.iter().map(|r| r.1).collect();
            assert_eq!(stages, ["age_limit", "order", "mode"]);
            assert_eq!(d.published.len(), 5, "{d:?}");
            assert!(d.drops.iter().any(|x| x.1 == "publication_gate"), "{d:?}");
        } else {
            assert_eq!(d.abort.map(|a| a.0), Some("protection_violation"), "{d:?}");
            assert_eq!(d.exit_code, crate::online_abort::EXIT_CODE);
        }
    }
}
