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
        State::new(transport::initial_generation()?, clock.clone())
            .map_err(|_| "invalid initial mode")?,
    ));
    let worker = Worker::start(state.clone(), move |jpeg_bytes| {
        let (rgb, height) = jpeg::decode(jpeg_bytes).map_err(|_| "jpeg_failed")?;
        let tensor = detector::prepare_rgb(&rgb, height).map_err(|_| "prepare_failed")?;
        let raw = detector.infer(&tensor).map_err(|_| "infer_failed")?;
        detector::postprocess(&raw, height).map_err(|_| "postprocess_failed")
    })?;
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
    let mut trial = Trial::new(boot, 60_000_000).map_err(|_| "invalid trial")?;
    let receiver = Receiver::new(
        &worker,
        Limits {
            duration_us: 180_000_000,
            ..Limits::default()
        },
    )
    .map_err(|_| "invalid limits")?;
    let outcome = receiver.run(&mut socket, |s, now| {
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
    let report = json!({"schema":1,"live_validation_only":true,"physical_motor_control":false,"global_acceptance":false,
        "finite_live_gate_passed":passed,"reason":outcome.reason,"measurement":measurement.ok(),"measurement_error":measurement_error,"model_sha256":detector::MODEL_SHA,
        "runtime_sha256":detector::RUNTIME_SHA,"runtime_build":ort::info(),"input_size":320,"threshold":0.5,"threads":3,
        "initial_eco":trial.initial,"tracking":trial.tracking,"final_eco":trial.final_eco,"session_final":outcome.final_status,
        "receiver":{"messages":outcome.stats.messages,"frames":outcome.stats.frames,"admitted":outcome.stats.admitted,"dropped":outcome.stats.dropped,
            "overwritten":outcome.stats.overwritten,"queue_busy":outcome.stats.queue_busy,"rejected_ack":outcome.stats.rejected_ack,
            "first_device":outcome.stats.first_device,"last_device":outcome.stats.last_device},
        "worker":{"inferred":s.counters.inferred,"published":s.counters.published,"dropped":s.counters.dropped,
            "failures":s.counters.failures,"evidence_lost":s.counters.evidence_lost},"process_before_worker_shutdown":usage,"samples":samples});
    let raw = serde_json::to_vec(&report)?;
    if raw.len() > 2 * 1024 * 1024 {
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
