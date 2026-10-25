use super::*;
use crate::{pipeline::Clock, transport::Auth};
use serde_json::json;
use std::{
    net::{TcpListener, TcpStream},
    thread,
    time::{Duration, Instant},
};
use tungstenite::client::IntoClientRequest;

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
                duration_us: 600_000_001,
                ..limits
            },
            Limits {
                frames: 0,
                ..limits
            },
            Limits {
                frames: 7001,
                ..limits
            },
            Limits {
                messages: 0,
                ..limits
            },
            Limits {
                messages: 20_001,
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
fn probe_interval_limits_are_validated_before_socket_use() {
    let (_, worker, _) = setup();
    for interval in [0, 49_999, 2_000_001, u64::MAX] {
        assert!(
            Receiver::new(
                &worker,
                Limits {
                    tracking_ping_interval_us: interval,
                    ..Limits::default()
                }
            )
            .is_err()
        );
    }
    for interval in [50_000, 200_000, 2_000_000] {
        assert!(
            Receiver::new(
                &worker,
                Limits {
                    tracking_ping_interval_us: interval,
                    ..Limits::default()
                }
            )
            .is_ok()
        );
    }
    worker.stop().unwrap();
}
