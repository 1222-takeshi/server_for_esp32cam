//! Finite clock-only observer. Never sends a profile/motor command or runs inference.
#![forbid(unsafe_code)]

#[cfg(target_os = "linux")]
mod linux {
    use esp32cam_frame_worker::{
        clock_probe::{Probe, Result, Stamp},
        control::{self, Control},
        transport::{self, Auth, Connection, Message},
    };
    use serde_json::{Value, json};
    use std::{
        net::{SocketAddr, TcpListener},
        thread,
        time::{Duration, Instant},
    };

    fn stamp() -> Result<Stamp> {
        let [ml, mh, rl, rh] = esp32cam_frame_worker::host_clock::snapshot()?;
        Ok(Stamp {
            mono: [ml, mh],
            raw: [rl, rh],
        })
    }
    pub fn execute() -> Result<Value> {
        let args = std::env::args_os()
            .map(|a| a.into_string().map_err(|_| "invalid_argument"))
            .collect::<Result<Vec<_>>>()?;
        if args.len() != 3 || std::env::var("ESP32CAM_RUST_CLOCK_PROBE").as_deref() != Ok("120") {
            return Err("explicit_120s_probe_required");
        }
        let boot = std::env::var("ESP32CAM_RUST_TRIAL_EXPECTED_BOOT")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .filter(|b| *b > 0)
            .ok_or("invalid_boot")?;
        let key = std::env::var("ESP32CAM_RUST_CLOCK_PROBE_KEY").map_err(|_| "missing_key")?;
        let auth = Auth::new(&args[2], key.as_bytes()).map_err(|_| "invalid_auth")?;
        let address: SocketAddr = args[1].parse().map_err(|_| "invalid_address")?;
        let listener = TcpListener::bind(address).map_err(|_| "bind")?;
        listener.set_nonblocking(true).map_err(|_| "listener")?;
        let begun = Instant::now();
        let mut rejected = 0;
        let socket = loop {
            if begun.elapsed() > Duration::from_secs(20) {
                return Err("connect_timeout");
            }
            match listener.accept() {
                Ok((tcp, _)) => match Connection::accept(tcp, &auth) {
                    Ok(v) => break v,
                    Err(_) => {
                        rejected += 1;
                        if rejected >= 4 {
                            return Err("authentication");
                        }
                    }
                },
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(10))
                }
                Err(_) => return Err("accept"),
            }
        };
        observe(socket, boot, Duration::from_secs(120), 1000)
    }

    fn observe(
        mut socket: Connection,
        boot: u64,
        duration: Duration,
        minimum: usize,
    ) -> Result<Value> {
        let mut probe = Probe::new(boot)?;
        let mut messages = 0u64;
        let mut ignored_frames = 0u64;
        let mut elapsed_us = 0;
        let outcome = (|| -> Result<()> {
            let hello = Instant::now();
            loop {
                if hello.elapsed() > Duration::from_secs(5) {
                    return Err("hello_timeout");
                }
                if let Some(message) = socket.receive().map_err(|_| "receive")? {
                    match message {
                        Message::Text(text) if matches!(control::parse(&text), Ok(Control::Hello { boot: b, .. }) if b == boot) =>
                        {
                            break;
                        }
                        _ => return Err("invalid_hello"),
                    }
                }
            }
            let start = Instant::now();
            let mut next = start;
            let mut pending_at = start;
            loop {
                let now = Instant::now();
                elapsed_us = start.elapsed().as_micros() as u64;
                if now.duration_since(start) >= duration && !probe.pending() {
                    break;
                }
                if probe.pending() && now.duration_since(pending_at) > Duration::from_secs(1) {
                    return Err("pong_timeout");
                }
                if !probe.pending() && now >= next && now.duration_since(start) < duration {
                    let nonce = transport::nonce().map_err(|_| "entropy")?;
                    let payload = json!({"type":"clock.ping","id":nonce}).to_string();
                    probe.begin(nonce, stamp()?)?;
                    socket.send_text(&payload).map_err(|_| "send")?;
                    pending_at = now;
                    next = now + Duration::from_millis(50);
                }
                if let Some(message) = socket.receive().map_err(|_| "receive")? {
                    let received = stamp()?;
                    messages += 1;
                    if messages > 8192 {
                        return Err("message_limit");
                    }
                    match message {
                        Message::Text(text) => {
                            match control::parse(&text).map_err(|_| "invalid_control")? {
                                Control::Pong {
                                    boot: b,
                                    nonce,
                                    device_us,
                                } => probe.pong(b, &nonce, device_us, received)?,
                                Control::Status { boot: b, .. } if b == boot => {}
                                _ => return Err("unexpected_control"),
                            }
                        }
                        Message::Binary(_) => ignored_frames += 1,
                        _ => {}
                    }
                }
            }
            if probe.records().len() < minimum {
                return Err("insufficient_exchanges");
            }
            Ok(())
        })();
        socket.shutdown();
        Ok(
            json!({"schema":1,"diagnostics_only":true,"global_acceptance":false,
            "physical_motor_control":false,"model_inference":false,"mode_requests":0,"images_saved":0,
            "continuous_tracking_gate_passed":false,"finite_live_gate_passed":false,
            "completed":outcome.is_ok(),"reason":outcome.err().unwrap_or("completed"),"boot_id":boot,
            "requested_seconds":duration.as_secs(),"elapsed_us":elapsed_us,"ignored_frames":ignored_frames,
            "fields":["device_us","sent_mono_low_ns","sent_mono_high_ns","sent_raw_low_ns","sent_raw_high_ns",
                "received_mono_low_ns","received_mono_high_ns","received_raw_low_ns","received_raw_high_ns"],
            "exchanges":probe.records()}),
        )
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use tungstenite::client::IntoClientRequest;
        #[test]
        fn wire_probe_only_sends_pings_and_retains_failed_evidence() {
            for poison in [false, true] {
                let listener = TcpListener::bind("127.0.0.1:0").unwrap();
                let address = listener.local_addr().unwrap();
                let client = thread::spawn(move || {
                    let mut request = format!("ws://{address}/ws/cameras/test/live")
                        .into_client_request()
                        .unwrap();
                    request
                        .headers_mut()
                        .insert("x-api-key", "test-private-key".parse().unwrap());
                    let (mut socket, _) = tungstenite::connect(request).unwrap();
                    socket.send(Message::Text(json!({"type":"profile.hello","schema":2,"boot_id":7,"generation_floor":0,"profiles":["eco","tracking"]}).to_string().into())).unwrap();
                    let mut count = 0u64;
                    while let Ok(Message::Text(text)) = socket.read() {
                        let value: Value = serde_json::from_str(&text).unwrap();
                        assert_eq!(value["type"], "clock.ping");
                        count += 1;
                        let id = if poison && count == 2 {
                            "0000000000000000"
                        } else {
                            value["id"].as_str().unwrap()
                        };
                        socket.send(Message::Text(json!({"type":"clock.pong","boot_id":7,"id":id,"device_us":count*50000}).to_string().into())).unwrap();
                    }
                    count
                });
                let (tcp, _) = listener.accept().unwrap();
                let connection =
                    Connection::accept(tcp, &Auth::new("test", b"test-private-key").unwrap())
                        .unwrap();
                let report = observe(connection, 7, Duration::from_millis(200), 1).unwrap();
                assert_eq!(report["completed"], !poison);
                assert_eq!(report["mode_requests"], 0);
                assert_eq!(report["physical_motor_control"], false);
                assert_eq!(report["model_inference"], false);
                assert_eq!(report["continuous_tracking_gate_passed"], false);
                assert!(!report.to_string().contains("test-private-key"));
                if poison {
                    assert_eq!(report["exchanges"].as_array().unwrap().len(), 1);
                }
                assert!(client.join().unwrap() >= 2);
            }
        }
    }
}

fn failure_report(reason: &'static str) -> serde_json::Value {
    serde_json::json!({"schema":1,"diagnostics_only":true,"completed":false,"reason":reason,
        "global_acceptance":false,"physical_motor_control":false,"model_inference":false,
        "mode_requests":0,"images_saved":0,"continuous_tracking_gate_passed":false,
        "finite_live_gate_passed":false,"exchanges":[]})
}

fn main() {
    use std::io::Write;
    #[cfg(target_os = "linux")]
    let result = linux::execute();
    #[cfg(not(target_os = "linux"))]
    let result: Result<serde_json::Value, &'static str> = Err("linux_required");
    let report = result.unwrap_or_else(failure_report);
    let passed = report["completed"] == true;
    let mut bytes = serde_json::to_vec(&report).expect("numeric clock report");
    bytes.push(b'\n');
    if bytes.len() > 1024 * 1024 || std::io::stdout().lock().write_all(&bytes).is_err() {
        std::process::exit(1);
    }
    std::process::exit(i32::from(!passed));
}

#[cfg(test)]
mod report_tests {
    #[test]
    fn early_failure_never_claims_live_acceptance() {
        let report = super::failure_report("invalid_argument");
        for field in [
            "completed",
            "global_acceptance",
            "physical_motor_control",
            "model_inference",
            "continuous_tracking_gate_passed",
            "finite_live_gate_passed",
        ] {
            assert_eq!(report[field], false);
        }
        assert_eq!(report["exchanges"], serde_json::json!([]));
    }
}
