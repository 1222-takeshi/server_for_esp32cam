use super::*;

fn frame(boot: u64, captured_us: u64) -> Frame {
    Frame {
        boot,
        sequence: 1,
        captured_us,
        sent_us: captured_us,
        generation: Some(1),
        jpeg: vec![],
    }
}
fn nonce() -> Result<String> {
    Ok("0123456789abcdef".into())
}
#[test]
fn valid_messages_and_typed_integer_boundaries() {
    let cases = [
        json!({"type":"profile.hello","boot_id":u64::MAX,"generation_floor":0,"schema":2,"profiles":["eco","tracking"]}),
        json!({"type":"profile.ack","boot_id":1,"generation":MAX_GENERATION,"mode":"tracking","applied_us":u64::MAX}),
        json!({"type":"clock.pong","boot_id":1,"id":"0123456789abcdef","device_us":1}),
        json!({"type":"capture.status","boot_id":1,"attempts":0,"captured":u64::MAX,"capture_failures":0,"stale":0,"sent":0,"send_failures":0}),
    ];
    for case in cases {
        assert!(parse(&case.to_string()).is_ok());
        for invalid in [
            json!(true),
            json!(0),
            json!(-1),
            json!(1.0),
            json!(null),
            json!("1"),
        ] {
            let mut modified = case.clone();
            modified["boot_id"] = invalid;
            assert!(parse(&modified.to_string()).is_err());
        }
        let mut extra = case.clone();
        extra["extra"] = json!(1);
        assert!(parse(&extra.to_string()).is_err());
        let mut missing = case;
        missing.as_object_mut().unwrap().remove("boot_id");
        assert!(parse(&missing.to_string()).is_err());
    }
    for invalid in ["[]", "null", "{", "{}", "NaN", &"x".repeat(TEXT_LIMIT + 1)] {
        assert!(parse(invalid).is_err());
    }
}
#[test]
fn clock_nonce_boot_rtt_timeout_and_tracking_ping_cadence() {
    let mut clock = SessionClock::default();
    assert!(clock.poll(1, false, || Err("entropy unavailable")).is_err());
    assert!(clock.poll(1, false, nonce).unwrap().is_some());
    assert!(
        clock
            .poll(50_001, true, || panic!("one pending only"))
            .unwrap()
            .is_none()
    );
    assert!(clock.pong(1, "bad", 1, 100).is_err());
    clock.pong(1, "0123456789abcdef", 1, 100).unwrap();
    assert!(clock.pong(1, "0123456789abcdef", 1, 101).is_err());
    assert!(
        clock
            .poll(49_999, true, || panic!("too soon"))
            .unwrap()
            .is_none()
    );
    assert!(clock.poll(50_001, true, nonce).unwrap().is_some());
    assert!(clock.bind(2).is_err());
    assert!(clock.poll(1_050_001, true, nonce).unwrap().is_none());
    assert_eq!(clock.poll(1_050_002, true, nonce), Err("clock_timeout"));
    assert!(clock.pong(1, "0123456789abcdef", 1, 1_050_002).is_err());
}
#[test]
fn conservative_age_reserve_completion_and_no_evidence_renewal() {
    let mut clock = SessionClock::default();
    clock.poll(10_000, false, nonce).unwrap();
    clock.pong(1, "0123456789abcdef", 1_000, 10_010).unwrap();
    let arrival = clock.admit(&frame(1, 1_000), 10_020).unwrap();
    assert!(arrival.fits(344_999, 165_000)); // age high = elapsed + 20
    assert!(!arrival.fits(345_001, 165_000));
    assert!(arrival.fits(510_000, 0));
    assert!(!arrival.fits(510_001, 0));
    assert!(!arrival.fits(10_019, 0));
    assert!(clock.admit(&frame(2, 1_000), 10_020).is_none());
    assert!(clock.admit(&frame(1, 9_000_000), 10_020).is_none());
    assert!(clock.admit(&frame(1, 1_000), 510_001).is_none());
    assert!(clock.admit(&frame(1, 1_000), 10_010_011).is_none());
    clock.poll(2_010_020, false, nonce).unwrap();
    clock
        .pong(1, "0123456789abcdef", 2_001_020, 2_010_030)
        .unwrap();
    assert!(!arrival.fits(2_010_030, 0));
}

#[test]
fn schema_fields_types_modes_profiles_and_utf8_budget() {
    let hello = json!({"type":"profile.hello","boot_id":1,"generation_floor":0,"schema":2,"profiles":["eco","tracking"]});
    let ack = json!({"type":"profile.ack","boot_id":1,"generation":1,"mode":"eco","applied_us":1});
    for (base, fields) in [
        (&hello, vec!["schema", "generation_floor"]),
        (&ack, vec!["generation", "applied_us"]),
    ] {
        for field in fields {
            for bad in [json!(true), json!(-1), json!(1.0), json!("1"), json!(null)] {
                let mut value = base.clone();
                value[field] = bad;
                assert!(parse(&value.to_string()).is_err());
            }
        }
    }
    for (base, key, bad) in [
        (&hello, "generation_floor", json!(MAX_GENERATION + 1)),
        (&hello, "profiles", json!(["tracking", "eco"])),
        (&hello, "schema", json!(3)),
        (&ack, "generation", json!(0)),
        (&ack, "generation", json!(MAX_GENERATION + 1)),
        (&ack, "applied_us", json!(0)),
        (&ack, "mode", json!("Tracking")),
    ] {
        let mut value = base.clone();
        value[key] = bad;
        assert!(parse(&value.to_string()).is_err());
    }
    let raw = hello.to_string();
    let padded = format!("{raw}{}", " ".repeat(TEXT_LIMIT - raw.len()));
    assert!(parse(&padded).is_ok());
    assert!(parse(&(padded + " ")).is_err());
    let pong = json!({"type":"clock.pong","boot_id":1,"id":"猫".repeat(16),"device_us":1});
    assert!(parse(&pong.to_string()).is_ok()); // nonce length is 16 Unicode scalars, like Python.
    for length in [15, 17] {
        let mut p = pong.clone();
        p["id"] = json!("a".repeat(length));
        assert!(parse(&p.to_string()).is_err());
    }
}
#[test]
fn eco_ping_cadence_invalid_clock_and_entropy_do_not_create_pending() {
    let mut clock = SessionClock::default();
    assert!(clock.poll(0, false, nonce).is_err());
    assert!(clock.poll(1, false, || Ok("short".into())).is_err());
    assert!(clock.poll(1, false, nonce).unwrap().is_some());
    clock.pong(1, "0123456789abcdef", 1, 100).unwrap();
    assert!(
        clock
            .poll(2_000_000, false, || panic!("eco too soon"))
            .unwrap()
            .is_none()
    );
    assert!(clock.poll(2_000_001, false, nonce).unwrap().is_some());
    assert!(clock.bind(0).is_err());
    assert!(clock.poll(2_000_000, false, nonce).is_err());
}
