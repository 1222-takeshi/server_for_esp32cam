use super::*;
use std::{env::VarError, sync::Arc, time::Instant};

fn unique(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("frame-log-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn environment_value_is_strict_and_continuous_only() {
    let dir = unique("env");
    let ok = dir.join("frames.jsonl");
    let text = ok.to_str().unwrap().to_string();
    assert_eq!(parse_env(Err(VarError::NotPresent), false), Ok(None));
    assert_eq!(parse_env(Err(VarError::NotPresent), true), Ok(None));
    assert_eq!(parse_env(Ok(text.clone()), true), Ok(Some(ok.clone())));
    // Enabled only for the continuous live trial.
    assert!(parse_env(Ok(text.clone()), false).is_err());
    let existing = dir.join("existing.jsonl");
    std::fs::write(&existing, b"x").unwrap();
    let link = dir.join("link.jsonl");
    #[cfg(unix)]
    std::os::unix::fs::symlink(dir.join("nowhere"), &link).unwrap();
    let bad = [
        String::new(),
        "frames.jsonl".into(),
        "./frames.jsonl".into(),
        format!("{}/", dir.display()),
        format!("{}/../frames.jsonl", dir.display()),
        format!("{}/./frames.jsonl", dir.display()),
        format!("{}//frames.jsonl", dir.display()),
        format!("{}/missing/frames.jsonl", dir.display()),
        format!("{}/fra\nmes.jsonl", dir.display()),
        format!("{}/fra\0mes.jsonl", dir.display()),
        format!("{}/{}", dir.display(), "a".repeat(5000)),
        existing.to_str().unwrap().into(),
        dir.to_str().unwrap().into(),
        "/".into(),
        #[cfg(unix)]
        link.to_str().unwrap().into(),
    ];
    for value in bad {
        assert!(parse_env(Ok(value.clone()), true).is_err(), "{value:?}");
    }
    assert!(parse_env(Err(VarError::NotUnicode("x".into())), true).is_err());
    // A parent that is a file is not a directory.
    let under_file = format!("{}/x", existing.display());
    assert!(parse_env(Ok(under_file), true).is_err());
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn ring_counts_overflow_without_growing() {
    let mut ring = Ring::with_capacity(3);
    let before = ring.items.capacity();
    let stored: Vec<_> = (0..5).map(|i| ring.push(i)).collect();
    assert_eq!(stored, [Some(0), Some(1), Some(2), None, None]);
    assert_eq!(
        (ring.items(), ring.lost(), ring.capacity()),
        (&[0, 1, 2][..], 2, 3)
    );
    assert_eq!(ring.items.capacity(), before);
}

fn recv(sequence: u64, stage: Option<&'static str>) -> RecvRecord {
    RecvRecord {
        boot: 7,
        generation: 3,
        sequence,
        captured_us: 1000 + sequence,
        sent_us: 1100 + sequence,
        received_us: 2000 + sequence,
        bytes: 5000,
        read: ReadSummary {
            first_byte_us: Some(1900),
            last_byte_us: Some(1990),
            reads: 4,
            max_read_us: 12,
            read_timeouts: 1,
            tcp: None,
        },
        stage,
    }
}
fn work(sequence: u64, stage: &'static str, published: bool) -> WorkRecord {
    WorkRecord {
        boot: 7,
        generation: 3,
        sequence,
        begun_us: Some(2100),
        finished_us: Some(2200),
        published_us: published.then_some(2210),
        compute_us: Some(100),
        stage,
    }
}

#[test]
fn rendered_lines_follow_the_fixed_schema() {
    let mut r = Ring::with_capacity(8);
    let mut w = Ring::with_capacity(8);
    r.push(recv(1, None));
    r.push(recv(2, Some("order")));
    r.push(recv(3, None)); // replaced in the queue by frame 4
    r.push(recv(4, None));
    r.push(recv(5, Some("busy")));
    r.push(recv(6, None)); // nothing worked on after it
    w.push(work(1, "published", true));
    w.push(work(4, "publication_gate", false));
    let mut shown = recv(1, None);
    shown.read.tcp = Some(TcpSnapshot {
        total_retrans: 9,
        retrans: 1,
        lost: 2,
        rto_us: 200_000,
        rcv_rtt_us: 4000,
        at_us: 2001,
    });
    r[0] = shown;
    let text = String::from_utf8(render(&r, &w)).unwrap();
    let lines: Vec<serde_json::Value> = text
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(
        lines[0].to_string(),
        r#"{"capacity":8,"clock":"native_monotonic_us","lost_records":0,"nominal_interval_us":200000,"records":6,"v":1}"#
    );
    let keys = [
        "sequence",
        "boot_id",
        "generation",
        "first_byte_us",
        "last_byte_us",
        "bytes",
        "received_us",
        "captured_us",
        "sent_us",
        "begun_us",
        "finished_us",
        "published_us",
        "compute_us",
        "stage",
        "reads",
        "max_read_us",
        "read_timeouts",
        "tcp",
    ];
    for line in text.lines().skip(1) {
        // The field names and their order are part of the format.
        let mut at = 0;
        for key in keys {
            let found = line[at..].find(&format!("\"{key}\":")).expect(key);
            at += found;
        }
    }
    for line in &lines[1..] {
        assert_eq!(line.as_object().unwrap().len(), keys.len());
        for key in keys {
            assert!(line.get(key).is_some(), "{key}");
        }
    }
    let stages: Vec<_> = lines[1..]
        .iter()
        .map(|l| l["stage"].as_str().unwrap())
        .collect();
    assert_eq!(
        stages,
        [
            "published",
            "order",
            "overwritten",
            "publication_gate",
            "busy",
            "unprocessed"
        ]
    );
    assert_eq!(lines[1]["published_us"], 2210);
    assert_eq!(lines[1]["compute_us"], 100);
    assert_eq!(lines[1]["tcp"]["total_retrans"], 9);
    assert_eq!(lines[1]["tcp"].as_object().unwrap().len(), 6);
    assert!(lines[2]["begun_us"].is_null() && lines[2]["published_us"].is_null());
    assert!(lines[4]["published_us"].is_null() && lines[4]["begun_us"] == 2100);
    assert!(lines[1..].iter().skip(1).all(|l| l["tcp"].is_null()));
    assert_eq!(lines[1]["first_byte_us"], 1900);
    assert_eq!(lines[1]["reads"], 4);
}

#[test]
fn overflow_is_recorded_in_the_header_not_in_the_behavior() {
    let mut r = Ring::with_capacity(2);
    let mut w = Ring::with_capacity(1);
    for i in 0..4 {
        r.push(recv(i, None));
    }
    w.push(work(0, "published", true));
    w.push(work(1, "published", true));
    let text = String::from_utf8(render(&r, &w)).unwrap();
    let header: serde_json::Value = serde_json::from_str(text.lines().next().unwrap()).unwrap();
    assert_eq!(header["capacity"], 2);
    assert_eq!(header["records"], 2);
    assert_eq!(header["lost_records"], 3); // 2 receiver + 1 worker
    assert_eq!(text.lines().count(), 3);
}

#[test]
fn write_once_creates_a_new_read_only_file_and_never_overwrites() {
    let dir = unique("write");
    let path = dir.join("frames.jsonl");
    write_once(&path, b"first\n").unwrap();
    assert_eq!(std::fs::read(&path).unwrap(), b"first\n");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o400
        );
    }
    assert_eq!(
        write_once(&path, b"second\n").unwrap_err().kind(),
        io::ErrorKind::AlreadyExists
    );
    assert_eq!(std::fs::read(&path).unwrap(), b"first\n");
    // A dangling symlink is not followed either (O_EXCL).
    #[cfg(unix)]
    {
        let link = dir.join("link");
        std::os::unix::fs::symlink(dir.join("target"), &link).unwrap();
        assert!(write_once(&link, b"x").is_err());
        assert!(!dir.join("target").exists());
    }
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn probe_summarizes_the_reads_of_one_message() {
    let ticks = Arc::new(std::sync::atomic::AtomicU64::new(0));
    let t = ticks.clone();
    let mut probe = Probe::new(Arc::new(move || {
        t.fetch_add(10, std::sync::atomic::Ordering::SeqCst) + 10
    }));
    // Idle waiting before the first byte is not a stall.
    probe.note_read(50_000, ReadKind::Timeout);
    probe.note_read(0, ReadKind::Other);
    probe.note_read(48_000, ReadKind::Data); // waited for the camera
    probe.note_read(70, ReadKind::Data);
    probe.note_read(50_000, ReadKind::Timeout);
    probe.note_read(30, ReadKind::Data);
    probe.complete(None);
    assert_eq!(
        probe.last(),
        ReadSummary {
            first_byte_us: Some(10),
            last_byte_us: Some(30),
            reads: 3,
            max_read_us: 50_000,
            read_timeouts: 1,
            tcp: None,
        }
    );
    // The next message starts from nothing.
    probe.note_read(10, ReadKind::Data);
    probe.complete(None);
    assert_eq!(probe.last().reads, 1);
    assert_eq!(probe.last().read_timeouts, 0);
}

/// Recording performs no I/O while the trial runs: the only writer is
/// `write_once`, and it is called once, from the binary, after the report.
#[test]
fn only_the_end_of_the_binary_writes_the_log() {
    let live = include_str!("../live_main.rs");
    let calls: Vec<_> = live.match_indices("frame_log::write_once(").collect();
    assert_eq!(calls.len(), 1);
    let (at, _) = calls[0];
    let report = live.find("io::stdout().lock().write_all(&raw)").unwrap();
    let ended = live.find("let mut outcome = receiver.run(").unwrap();
    assert!(ended < report && report < at);
    // It is not inside a loop or a callback: nothing after the call but the exit.
    assert!(live[at..].find("online_abort::exit_code").unwrap() < 200);
    for (name, source) in [
        ("session", include_str!("../session.rs")),
        ("pipeline", include_str!("../pipeline.rs")),
        ("transport", include_str!("../transport.rs")),
        ("online_abort", include_str!("../online_abort.rs")),
        ("control", include_str!("../control.rs")),
    ] {
        for banned in ["write_once", "std::fs", "File::", "OpenOptions", "println!"] {
            assert!(!source.contains(banned), "{name} uses {banned}");
        }
    }
    // Inside this module only `write_once` touches the file system.
    let own = include_str!("../frame_log.rs");
    let body = own.split("\n#[cfg(test)]\nmod tests;").next().unwrap();
    assert_eq!(body.matches("OpenOptions::new()").count(), 1);
    assert_eq!(body.matches("symlink_metadata").count(), 1); // parse_env only
}

/// `cargo test --release -- --ignored --nocapture frame_log::tests::cost` prints
/// the recording cost per frame; the numbers are in docs/rust-worker.md.
#[test]
#[ignore]
fn cost_of_recording_one_frame() {
    const FRAMES: usize = 18_000;
    const ROUNDS: usize = 200;
    let epoch = Instant::now();
    let clock: Clock = Arc::new(move || epoch.elapsed().as_micros() as u64);
    let mut probe = Probe::new(clock);
    let mut recv_ring: Ring<RecvRecord> = Ring::with_capacity(FRAMES);
    let mut work_ring: Ring<WorkRecord> = Ring::with_capacity(FRAMES);
    let sample = recv(1, None);
    let work_sample = work(1, "published", true);
    let mut best = [f64::MAX; 3];
    for _ in 0..ROUNDS {
        recv_ring.clear();
        work_ring.clear();
        let begun = Instant::now();
        for _ in 0..FRAMES {
            // About 30 reads of a 4 KB buffer for one 100 KB frame, one timeout.
            for _ in 0..30 {
                probe.note_read(40, ReadKind::Data);
            }
            probe.note_read(50_000, ReadKind::Timeout);
            probe.complete(None);
        }
        best[0] = best[0].min(begun.elapsed().as_secs_f64() * 1e6 / FRAMES as f64);
        let begun = Instant::now();
        for _ in 0..FRAMES {
            let i = recv_ring.push(std::hint::black_box(sample));
            recv_ring[i.unwrap()].stage = Some("busy");
        }
        best[1] = best[1].min(begun.elapsed().as_secs_f64() * 1e6 / FRAMES as f64);
        let begun = Instant::now();
        for _ in 0..FRAMES {
            work_ring.push(std::hint::black_box(work_sample));
        }
        best[2] = best[2].min(begun.elapsed().as_secs_f64() * 1e6 / FRAMES as f64);
    }
    let mut r = Ring::with_capacity(FRAMES);
    let mut w = Ring::with_capacity(FRAMES);
    for i in 0..FRAMES as u64 {
        r.push(recv(i, None));
        w.push(work(i, "published", true));
    }
    let begun = Instant::now();
    let bytes = render(&r, &w);
    let render_ms = begun.elapsed().as_secs_f64() * 1e3;
    let dir = unique("cost");
    let begun = Instant::now();
    write_once(&dir.join("frames.jsonl"), &bytes).unwrap();
    let write_ms = begun.elapsed().as_secs_f64() * 1e3;
    let _ = std::fs::remove_dir_all(dir);
    println!(
        "per frame (best of {ROUNDS} rounds): reads(30 data + 1 timeout) {:.3} us, receiver record+stage {:.3} us, worker record {:.3} us",
        best[0], best[1], best[2]
    );
    println!(
        "end of run, {FRAMES} frames: render {render_ms:.1} ms, write {write_ms:.1} ms, {} bytes, record sizes {} + {} bytes",
        bytes.len(),
        std::mem::size_of::<RecvRecord>(),
        std::mem::size_of::<WorkRecord>()
    );
}

/// The same bytes are read by `tests/test_spin_on_run_interval_classify.py`, so a renamed
/// or reordered field breaks the writer and the reader's tests together.
#[test]
fn rendered_output_matches_the_fixture_the_classifier_reads() {
    let mut r = Ring::with_capacity(8);
    let mut w = Ring::with_capacity(8);
    r.push(recv(1, None));
    r.push(recv(2, Some("order")));
    r.push(recv(4, None));
    w.push(work(1, "published", true));
    w.push(work(4, "publication_gate", false));
    let text = String::from_utf8(render(&r, &w)).unwrap();
    assert_eq!(
        text,
        include_str!("../../../../tests/fixtures/spin_on_run/frames_render.jsonl")
    );
}
