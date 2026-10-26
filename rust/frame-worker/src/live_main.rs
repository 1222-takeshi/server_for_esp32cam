//! Finite, supervised Raspberry Pi live experiment; no storage/UI/motor control.
#![forbid(unsafe_code)]
use esp32cam_frame_worker::{
    detector::{self, Detector, Result},
    jpeg,
    pipeline::{Clock, State, Worker},
    replay,
    session::{Limits, Receiver},
    transport::{self, Auth, Connection},
    trial::{self, Trial},
};
use serde_json::{Value, json};
use std::{
    io::{self, BufRead, Read, Write},
    net::{SocketAddr, TcpListener},
    path::Path,
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Copy, Default)]
struct Beat {
    alive: bool,
    mono: u64,
    wall: u64,
}
impl Beat {
    fn ready(self, mono: u64, wall: u64) -> bool {
        self.alive
            && self.mono > 0
            && self.wall > 0
            && wall > 0
            && mono
                .checked_sub(self.mono)
                .is_some_and(|age| age <= 2_000_000)
            && wall.abs_diff(self.wall) <= 1500
    }
}
fn wall_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|d| u64::try_from(d.as_millis()).ok())
        .unwrap_or(0)
}
fn key(camera: &str, global: &str, mapping: &str, shared_id: &str) -> Result<String> {
    if mapping.len() > 64 * 1024 {
        return Err("invalid key configuration".into());
    }
    if !mapping.trim().is_empty() {
        let value: Value = serde_json::from_str(mapping)?;
        let map = value.as_object().ok_or("invalid key map")?;
        if map.len() > 500
            || map.iter().any(|(id, v)| {
                id.trim().is_empty() || !v.is_string() || v.as_str().unwrap().trim().is_empty()
            })
        {
            return Err("invalid key map".into());
        }
        if !map.is_empty() {
            return map
                .get(camera)
                .and_then(Value::as_str)
                .map(|v| v.trim().to_owned())
                .ok_or_else(|| "camera key missing".into());
        }
    }
    if shared_id != camera || global.is_empty() {
        return Err("shared key identity missing".into());
    }
    Ok(global.into()) // Auth::new validates the bounded printable key.
}
fn monitor(clock: Clock, nonce: String) -> Arc<Mutex<Beat>> {
    let beat = Arc::new(Mutex::new(Beat::default()));
    let shared = beat.clone();
    thread::spawn(move || {
        let stdin = io::stdin();
        let mut reader = stdin.lock();
        loop {
            let mut raw = vec![];
            let count = (&mut reader).take(128).read_until(b'\n', &mut raw);
            if count.is_err()
                || raw.is_empty()
                || !raw.ends_with(b"\n")
                || !trial::heartbeat(&raw, &nonce, wall_ms())
            {
                break;
            }
            let ticket = std::str::from_utf8(&raw)
                .unwrap()
                .split_ascii_whitespace()
                .nth(2)
                .unwrap()
                .parse()
                .unwrap();
            let Ok(mut current) = shared.lock() else {
                return;
            };
            *current = Beat {
                alive: true,
                mono: clock(),
                wall: ticket,
            };
        }
        if let Ok(mut current) = shared.lock() {
            current.alive = false;
        }
    });
    beat
}
fn refresh_ready(
    shared: &Mutex<Beat>,
    cached: &mut Beat,
    mono: impl FnOnce() -> u64,
    wall: impl FnOnce() -> u64,
) -> bool {
    if let Ok(current) = shared.try_lock() {
        *cached = *current;
    }
    cached.ready(mono(), wall())
}
fn adaptive_reserve(value: std::result::Result<String, std::env::VarError>) -> Result<bool> {
    match value {
        Err(std::env::VarError::NotPresent) => Ok(false),
        Ok(value) if value == "0" => Ok(false),
        Ok(value) if value == "1" => Ok(true),
        _ => Err("invalid adaptive reservation".into()),
    }
}

fn continuous_poll(
    s: &mut State,
    trial: &mut Trial,
    beats: &Mutex<Beat>,
    cached: &mut Beat,
    mono: impl Fn() -> u64,
    wall: impl Fn() -> u64 + Clone + Send + Sync + 'static,
) -> std::result::Result<bool, &'static str> {
    s.mode.set_recovery_ready(false);
    s.mode.set_recovery_valid_until(None);
    s.mode.set_recovery_guard(None);
    let permit = beats.try_lock().ok(); // Never block the receiver on stdin owner.
    if let Some(current) = permit.as_ref() {
        *cached = **current;
    }
    let (now, unix) = (mono(), wall());
    let ready = cached.ready(now, unix);
    if ready {
        let capture_end = s.tracking_capture_deadline().map_err(|_| "mode_failed")?;
        if permit.is_some() {
            let (ticket, wall_check) = (*cached, wall.clone());
            s.mode.set_recovery_guard(Some(Box::new(move || {
                let current = wall_check();
                current > 0 && current.abs_diff(ticket.wall) <= 1500
            })));
            let resource_end = cached
                .mono
                .saturating_add(2_000_000)
                .min(now.saturating_add((1500 - unix.abs_diff(cached.wall)) * 1000));
            s.mode.set_recovery_valid_until(capture_end.map(|end| {
                end.min(resource_end).min(
                    s.mode
                        .tracking_hold_started()
                        .map_or(u64::MAX, |at| at.saturating_add(30_000_000)),
                )
            }));
            s.mode.set_recovery_ready(capture_end.is_some());
        }
    }
    let result = trial.poll(s, now, ready);
    s.mode.set_recovery_ready(false); // Worker cannot recover using a cached ticket.
    s.mode.set_recovery_valid_until(None);
    s.mode.set_recovery_guard(None);
    drop(permit); // Guard spans the recovery commit under the outer State mutex.
    result
}

fn execute() -> Result<bool> {
    let args: Vec<_> = std::env::args_os().collect();
    if args.len() != 5 {
        return Err("usage: frame-live MODEL ORT ADDRESS CAMERA".into());
    }
    let camera = args[4].to_str().ok_or("invalid camera")?;
    let address: SocketAddr = args[3].to_str().ok_or("invalid address")?.parse()?;
    let boot = std::env::var("ESP32CAM_RUST_TRIAL_EXPECTED_BOOT")?.parse()?;
    let nonce = std::env::var("ESP32CAM_RUST_TRIAL_NONCE")?;
    if nonce.len() != 16 || !nonce.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("invalid supervisor nonce".into());
    }
    let duration = std::env::var("ESP32CAM_RUST_TRIAL_SECONDS")
        .map(Some)
        .or_else(|e| match e {
            std::env::VarError::NotPresent => Ok(None),
            _ => Err("invalid duration"),
        })?;
    let duration_us = trial::duration_us(duration.as_deref()).map_err(|_| "invalid duration")?;
    let budget_seconds = duration_us / 1_000_000 + 120;
    let adaptive = adaptive_reserve(std::env::var("ESP32CAM_RUST_TRIAL_ADAPTIVE_RESERVE"))?;
    let continuous = adaptive_reserve(std::env::var("ESP32CAM_RUST_TRIAL_AUTO_RECOVERY"))?;
    let inject_hold = adaptive_reserve(std::env::var("ESP32CAM_RUST_TRIAL_INJECT_HOLD"))?;
    if inject_hold && !continuous {
        return Err("hold injection requires continuous policy".into());
    }
    let selected = key(
        camera,
        &std::env::var("ESP32CAM_DEVICE_API_KEY").unwrap_or_default(),
        &std::env::var("ESP32CAM_DEVICE_API_KEYS").unwrap_or_default(),
        &std::env::var("ESP32CAM_SHARED_KEY_DEVICE_ID").unwrap_or_default(),
    )?;
    let auth = Auth::new(camera, selected.as_bytes())?;
    let epoch = Instant::now();
    let clock: Clock = Arc::new(move || epoch.elapsed().as_micros() as u64 + 1);
    let beats = monitor(clock.clone(), nonce);
    let mut detector = Detector::new(Path::new(&args[1]), Path::new(&args[2]))?;
    let warmup = vec![0.; detector::INPUT_LEN];
    for _ in 0..5 {
        detector.infer(&warmup)?;
    }
    drop(warmup);
    let mut cached = Beat::default();
    let waiting = clock();
    loop {
        if let Ok(current) = beats.try_lock() {
            cached = *current;
        }
        if cached.ready(clock(), wall_ms()) {
            break;
        }
        if clock() - waiting > 2_000_000 {
            return Err("supervisor unavailable".into());
        }
        thread::sleep(Duration::from_millis(10));
    }
    let state = Arc::new(Mutex::new(
        State::new_with_tracking_recovery(
            transport::initial_generation()?,
            clock.clone(),
            continuous,
        )
        .map_err(|_| "invalid initial mode")?,
    ));
    let worker = Worker::start_with_adaptive_reserve(
        state.clone(),
        move |jpeg_bytes| {
            let (rgb, height) = jpeg::decode(jpeg_bytes).map_err(|_| "jpeg_failed")?;
            let tensor = detector::prepare_rgb(&rgb, height).map_err(|_| "prepare_failed")?;
            let raw = detector.infer(&tensor).map_err(|_| "infer_failed")?;
            detector::postprocess(&raw, height).map_err(|_| "postprocess_failed")
        },
        adaptive,
    )?;
    let listener = TcpListener::bind(address)?;
    listener.set_nonblocking(true)?;
    let begun = clock();
    let mut rejected = 0;
    let mut socket = loop {
        if let Ok(current) = beats.try_lock() {
            cached = *current;
        }
        if !cached.ready(clock(), wall_ms()) {
            return Err("supervisor expired".into());
        }
        if clock() - begun > 20_000_000 {
            return Err("camera connection timeout".into());
        }
        match listener.accept() {
            Ok((tcp, _)) => match Connection::accept(tcp, &auth) {
                Ok(socket) => break socket,
                Err(_) => {
                    rejected += 1;
                    if rejected >= 4 {
                        return Err("camera authentication failed".into());
                    }
                }
            },
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(10))
            }
            Err(e) => return Err(e.into()),
        }
    };
    let mut trial = if continuous {
        Trial::new_continuous(boot, duration_us, inject_hold)
    } else {
        Trial::new(boot, duration_us)
    }
    .map_err(|_| "invalid trial")?;
    let receiver = Receiver::new(
        &worker,
        Limits {
            duration_us: budget_seconds * 1_000_000,
            frames: budget_seconds * 12,
            messages: budget_seconds * 50,
            ..Limits::default()
        },
    )
    .map_err(|_| "invalid limits")?;
    let outcome = receiver.run(&mut socket, |s, now| {
        if continuous {
            return continuous_poll(s, &mut trial, &beats, &mut cached, || clock(), wall_ms);
        }
        let ready = refresh_ready(&beats, &mut cached, || clock(), wall_ms);
        trial.poll(s, now, ready)
    });
    let usage = replay::usage();
    worker.stop().map_err(|_| "worker failed")?;
    let s = state.lock().map_err(|_| "state unavailable")?;
    let measurement = trial.evaluate(s.samples());
    let passed = outcome.reason == "completed" && measurement.is_ok();
    let measurement_error = measurement.as_ref().err().copied();
    let samples: Vec<_> = s
        .samples()
        .iter()
        .map(|v| {
            json!({"boot_id":v.boot,"generation":v.generation,"sequence":v.sequence,
        "published_us":v.published_us,"age_high_us":v.age_high_us,"compute_us":v.compute_us})
        })
        .collect();
    let drops: Vec<_> = s
        .drops()
        .iter()
        .map(|d| {
            json!({
                "boot_id":d.boot,"generation":d.generation,"sequence":d.sequence,"at_us":d.at_us,
                "stage":d.stage,"age_high_us":d.age_high_us,"arrival_age_high_us":d.arrival_age_high_us,
        "arrival_age_low_us":d.arrival_age_low_us,"capture_to_send_us":d.capture_to_send_us,
        "elapsed_since_arrival_us":d.wait_us,"compute_us":d.compute_us,"reserve_us":d.reserve_us
            })
        })
        .collect();
    let io_trace: Vec<_> = socket.io_trace().iter().map(|e| json!({
        "kind":e.kind,"status":e.status,"observed_unix_us":e.observed_unix_us,"elapsed_us":e.elapsed_us,"bytes":e.bytes,
    })).collect();
    let receipts: Vec<_> = outcome.stats.recent_receipts.iter().map(|r| json!({
        "sequence":r.sequence,"received_us":r.received_us,"ws_complete_unix_us":r.ws_complete_unix_us,
    })).collect();
    let rejections: Vec<_> = outcome
        .stats
        .recent_rejections
        .iter()
        .map(|r| {
            json!({
                "boot_id":r.boot,"generation":r.generation,"sequence":r.sequence,
                "captured_us":r.captured_us,"sent_us":r.sent_us,"received_us":r.received_us,
                "stage":r.stage,"age_bounds_us":r.age_bounds.map(|(low,high)| [low.to_string(),high.to_string()]),"clock_exchanges":r.clock_exchanges,
            })
        })
        .collect();
    let report = json!({"schema":1,"live_validation_only":true,"physical_motor_control":false,"global_acceptance":false,
        "adaptive_inference_reserve":adaptive,"finite_live_gate_passed":passed && !continuous,
        "continuous_tracking_policy":continuous,"continuous_tracking_gate_passed":passed && continuous,
        "hold_injection_requested":inject_hold,"tracking_ping_interval_us":50_000,"reason":outcome.reason,"measurement":measurement.ok(),"measurement_error":measurement_error,"model_sha256":detector::MODEL_SHA,
        "runtime_sha256":detector::RUNTIME_SHA,"runtime_build":ort::info(),"input_size":320,"threshold":0.5,"threads":3,
        "initial_eco":trial.initial,"tracking":trial.tracking,"final_eco":trial.final_eco,"session_final":outcome.final_status,
        "receiver":{"messages":outcome.stats.messages,"frames":outcome.stats.frames,"admitted":outcome.stats.admitted,"dropped":outcome.stats.dropped,
            "overwritten":outcome.stats.overwritten,"queue_busy":outcome.stats.queue_busy,"rejected_ack":outcome.stats.rejected_ack,
            "first_device":outcome.stats.first_device,"last_device":outcome.stats.last_device,
            "max_receive_call_us":outcome.stats.max_receive_call_us,"max_control_send_us":outcome.stats.max_control_send_us,
            "max_admission_guard_us":outcome.stats.max_admission_guard_us,"recent_receipts":receipts,"recent_rejections":rejections},
        "worker":{"inferred":s.counters.inferred,"published":s.counters.published,"dropped":s.counters.dropped,
            "failures":s.counters.failures,"evidence_lost":s.counters.evidence_lost},"process_before_worker_shutdown":usage,"samples":samples,"recent_worker_drops":drops,"transport_io":io_trace});
    let raw = serde_json::to_vec(&report)?;
    // The on-disk cap includes the trailing newline.
    if raw.len() >= 16 * 1024 * 1024 {
        return Err("report limit".into());
    }
    io::stdout().lock().write_all(&raw)?;
    io::stdout().lock().write_all(b"\n")?;
    Ok(passed)
}
fn main() {
    match execute() {
        Ok(true) => {}
        Ok(false) => std::process::exit(3),
        Err(_) => {
            eprintln!(
                "Live trial failed; verify private configuration, fixed model/runtime and supervisor"
            );
            std::process::exit(2);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

    fn held_latest(time: &Arc<AtomicU64>, clock: Clock) -> Arc<Mutex<State>> {
        use esp32cam_frame_worker::{
            Frame,
            control::SessionClock,
            pipeline::{Job, Submit},
        };
        let mut s = State::new_with_tracking_recovery(10, clock, true).unwrap();
        s.mode.hello(1, 0).unwrap();
        s.mode
            .request(
                esp32cam_frame_worker::operating_mode::Mode::Tracking,
                s.mode.generation(),
                true,
            )
            .unwrap();
        s.mode.protect("frames_expired").unwrap();
        s.mode
            .acknowledge(
                1,
                s.mode.generation(),
                esp32cam_frame_worker::operating_mode::Mode::Tracking,
                1,
            )
            .unwrap();
        let mut frame = Frame {
            boot: 1,
            sequence: 0,
            captured_us: 100,
            sent_us: 100,
            generation: Some(s.mode.generation()),
            jpeg: vec![0],
        };
        for seq in 1..=53 {
            time.fetch_add(190_000, Ordering::SeqCst);
            frame.sequence = seq;
            frame.captured_us = seq + 100;
            assert!(s.mode.publish_result(&frame, || true).unwrap());
        }
        let now = time.fetch_add(190_000, Ordering::SeqCst) + 190_000;
        frame.sequence = 54;
        frame.captured_us = now;
        frame.sent_us = now;
        let mut sync = SessionClock::default();
        sync.poll(now, true, || Ok("0123456789abcdef".into()))
            .unwrap();
        sync.pong(1, "0123456789abcdef", now, now).unwrap();
        let arrival = sync.admit(&frame, now).unwrap();
        let state = Arc::new(Mutex::new(s));
        let worker = Worker::start(state.clone(), |_| Ok(vec![])).unwrap();
        assert_eq!(
            worker.submit(Job {
                frame,
                arrival,
                cancelled: Arc::new(AtomicBool::new(false))
            }),
            Submit::Queued
        );
        let deadline = Instant::now() + Duration::from_secs(2);
        while state.lock().unwrap().counters.published == 0 {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(1));
        }
        worker.stop().unwrap();
        state
    }

    #[test]
    fn continuous_permit_contention_never_uses_cached_ready_to_recover() {
        let time = Arc::new(AtomicU64::new(1));
        let copy = time.clone();
        let state = held_latest(&time, Arc::new(move || copy.load(Ordering::SeqCst)));
        let now = time.load(Ordering::SeqCst);
        let shared = Mutex::new(Beat {
            alive: false,
            mono: now,
            wall: 10000,
        });
        let mut cached = Beat {
            alive: true,
            mono: now,
            wall: 10000,
        };
        let mut trial = Trial::new_continuous(1, 60_000_000, false).unwrap();
        let occupied = shared.lock().unwrap();
        let mut s = state.lock().unwrap();
        s.mode.set_recovery_ready(true); // Prior caller must not leak permission.
        assert!(
            continuous_poll(&mut s, &mut trial, &shared, &mut cached, || now, || 10000).unwrap()
        );
        assert_eq!(s.mode.status().unwrap()["tracking_automatic_recoveries"], 0);
        drop(occupied);
        assert_eq!(
            continuous_poll(&mut s, &mut trial, &shared, &mut cached, || now, || 10000),
            Err("resources_unavailable")
        );
        assert_eq!(s.mode.status().unwrap()["requested_mode"], "eco");
    }

    #[test]
    fn wall_clock_step_during_poll_cannot_commit_tracking_recovery() {
        let time = Arc::new(AtomicU64::new(1));
        let wall = Arc::new(AtomicU64::new(10000));
        let armed = Arc::new(AtomicBool::new(false));
        let (t, w, a) = (time.clone(), wall.clone(), armed.clone());
        let state = held_latest(
            &time,
            Arc::new(move || {
                if a.swap(false, Ordering::SeqCst) {
                    w.store(12001, Ordering::SeqCst);
                }
                t.load(Ordering::SeqCst)
            }),
        );
        let now = time.load(Ordering::SeqCst);
        let shared = Mutex::new(Beat {
            alive: true,
            mono: now,
            wall: 10000,
        });
        let mut cached = Beat::default();
        let mut trial = Trial::new_continuous(1, 60_000_000, false).unwrap();
        armed.store(true, Ordering::SeqCst);
        let mut s = state.lock().unwrap();
        assert!(
            continuous_poll(
                &mut s,
                &mut trial,
                &shared,
                &mut cached,
                || now,
                move || wall.load(Ordering::SeqCst)
            )
            .unwrap()
        );
        assert_eq!(s.mode.status().unwrap()["tracking_automatic_recoveries"], 0);
        assert_eq!(
            s.mode.status().unwrap()["tracking_auto_recovery_pending"],
            true
        );
    }
    #[test]
    fn adaptive_policy_is_explicit_and_invalid_values_fail() {
        assert!(!adaptive_reserve(Err(std::env::VarError::NotPresent)).unwrap());
        assert!(!adaptive_reserve(Ok("0".into())).unwrap());
        assert!(adaptive_reserve(Ok("1".into())).unwrap());
        for value in ["", "true", "01", " 1", "2"] {
            assert!(adaptive_reserve(Ok(value.into())).is_err());
        }
        assert!(adaptive_reserve(Err(std::env::VarError::NotUnicode("bad".into()))).is_err());
    }
    #[test]
    fn key_mapping_and_supervisor_cache_fail_closed() {
        assert_eq!(
            key("cam", "global", r#"{"cam":"local"}"#, "").unwrap(),
            "local"
        );
        assert!(key("other", "global", r#"{"cam":"local"}"#, "other").is_err());
        assert!(key("cam", "global", "", "other").is_err());
        assert!(key("cam", "global", "", " cam ").is_err());
        assert!(key("cam", "global", "", "").is_err());
        assert_eq!(key("cam", "global", "{}", "cam").unwrap(), "global");
        for invalid in ["[]", "{", r#"{"cam":false}"#, r#"{"cam":""}"#] {
            assert!(key("cam", "", invalid, "cam").is_err());
        }
        let shared = Mutex::new(Beat {
            alive: true,
            mono: 101,
            wall: 10000,
        });
        let mut cached = Beat::default();
        let receiver_now = 100;
        assert!(!shared.lock().unwrap().ready(receiver_now, 10000));
        assert!(refresh_ready(&shared, &mut cached, || 102, || 10000));
        let good = Beat {
            alive: true,
            mono: 100,
            wall: 10000,
        };
        assert!(good.ready(2_000_100, 11500));
        assert!(!good.ready(2_000_101, 10000));
        assert!(!good.ready(99, 10000));
        assert!(!good.ready(100, 11501));
        assert!(
            !Beat {
                alive: false,
                ..good
            }
            .ready(100, 10000)
        );
    }
}
