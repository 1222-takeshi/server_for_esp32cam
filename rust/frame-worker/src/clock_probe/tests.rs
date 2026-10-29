use super::*;

fn stamp(at: u64) -> Stamp {
    Stamp {
        mono: [at, at + 1],
        raw: [at + 100, at + 101],
    }
}
fn begin(p: &mut Probe, at: u64) {
    p.begin("0123456789abcdef".into(), stamp(at)).unwrap();
}

#[test]
fn pairs_same_pong_without_merging_clock_domains() {
    let mut p = Probe::new(u64::MAX).unwrap();
    begin(&mut p, 1);
    assert!(p.pending());
    p.pong(u64::MAX, "0123456789abcdef", 9, stamp(100)).unwrap();
    assert!(!p.pending());
    assert_eq!(p.records(), &[[9, 1, 2, 101, 102, 100, 101, 200, 201]]);
    begin(&mut p, 101);
    p.pong(u64::MAX, "0123456789abcdef", 10, stamp(200))
        .unwrap();
    assert_eq!(p.records().len(), 2);
}

#[test]
fn invalid_boot_nonce_order_and_unsolicited_pong_fail_closed() {
    assert!(Probe::new(0).is_err());
    let mut p = Probe::new(1).unwrap();
    for nonce in ["", "short", "0123456789abcdeg", "０123456789abcde"] {
        assert!(p.begin(nonce.into(), stamp(1)).is_err());
    }
    assert!(p.pong(1, "0123456789abcdef", 1, stamp(2)).is_err());
    begin(&mut p, 1);
    assert!(p.begin("0123456789abcdef".into(), stamp(2)).is_err());
    for (boot, nonce, device) in [
        (2, "0123456789abcdef", 1),
        (1, "fedcba9876543210", 1),
        (1, "0123456789abcdef", 0),
    ] {
        assert!(p.pong(boot, nonce, device, stamp(10)).is_err());
        assert!(p.records().is_empty());
    }
    p.pong(1, "0123456789abcdef", 1, stamp(10)).unwrap();
    assert!(p.begin("0123456789abcdef".into(), stamp(5)).is_err());
    begin(&mut p, 11);
    assert!(p.pong(1, "0123456789abcdef", 1, stamp(20)).is_err());
}

#[test]
fn validates_brackets_and_rtt_in_both_domains_at_boundary() {
    for raw_bad in [false, true] {
        let mut p = Probe::new(1).unwrap();
        let start = Stamp {
            mono: [1, 1],
            raw: [1, 1],
        };
        let mut end = Stamp {
            mono: [MAX_RTT_NS + 1; 2],
            raw: [MAX_RTT_NS + 1; 2],
        };
        p.begin("0123456789abcdef".into(), start).unwrap();
        let mut bad = end;
        if raw_bad {
            bad.raw[1] += 1;
        } else {
            bad.mono[1] += 1;
        }
        assert!(p.pong(1, "0123456789abcdef", 1, bad).is_err());
        end.mono = [3, 2];
        assert!(p.pong(1, "0123456789abcdef", 1, end).is_err());
        p.pong(
            1,
            "0123456789abcdef",
            1,
            Stamp {
                mono: [MAX_RTT_NS + 1; 2],
                raw: [MAX_RTT_NS + 1; 2],
            },
        )
        .unwrap();
    }
    let mut p = Probe::new(1).unwrap();
    assert!(p.begin("0123456789abcdef".into(), stamp(0)).is_err());
    begin(&mut p, 1);
    assert!(
        p.pong(
            1,
            "0123456789abcdef",
            1,
            Stamp {
                mono: [u64::MAX; 2],
                raw: [u64::MAX; 2]
            }
        )
        .is_err()
    );
}

#[test]
fn retains_failed_attempt_evidence_and_bounds_storage() {
    let mut p = Probe::new(1).unwrap();
    for i in 0..MAX_RECORDS as u64 {
        let at = 1 + i * 1000;
        begin(&mut p, at);
        p.pong(1, "0123456789abcdef", i + 1, stamp(at + 100))
            .unwrap();
    }
    assert_eq!(p.records().len(), MAX_RECORDS);
    assert!(
        p.begin("0123456789abcdef".into(), stamp(10_000_000))
            .is_err()
    );
    assert_eq!(p.records().len(), MAX_RECORDS);
    assert!(serde_json::to_vec(p.records()).unwrap().len() < 1024 * 1024 - 4096);
    assert!(
        serde_json::to_vec(&vec![[u64::MAX; 9]; MAX_RECORDS])
            .unwrap()
            .len()
            < 1024 * 1024 - 4096
    );
}
