use super::*;

const S: u64 = 1_000_000;

fn sample(published_us: u64, age_high_us: i128) -> Sample {
    Sample {
        boot: 1,
        generation: 1,
        sequence: published_us,
        published_us,
        age_high_us,
        compute_us: 0,
    }
}

fn steady(start: u64, until: u64, step: u64) -> Vec<Sample> {
    (0..)
        .map(|i| start + i * step)
        .take_while(|p| *p < until)
        .map(|p| sample(p, 100_000))
        .collect()
}

fn at(now: u64, interval: (u64, u64)) -> Observation<'static> {
    Observation {
        now,
        interval: Some(interval),
        hold_started: None,
        last_hold: None,
        samples: &[],
        protection: Protection::default(),
    }
}

#[test]
fn budget_is_one_percent_of_the_interval() {
    assert_eq!(hold_budget_us(600 * S), 6_000_000);
    assert_eq!(hold_budget_us(3600 * S), 36_000_000);
}

#[test]
fn ongoing_hold_trips_at_exactly_fifteen_seconds_from_its_own_start() {
    let interval = (1_000 * S, 4_600 * S);
    let began = 1_200 * S;
    let dense = steady(1_000 * S, 1_300 * S, 100_000);
    for (now, expected) in [
        (began + 15 * S - 1, None),
        (began + 15 * S, Some(CONTINUOUS_HOLD)),
        (began + 15 * S + 1, Some(CONTINUOUS_HOLD)),
    ] {
        let mut guard = OnlineGuard::new(3600 * S);
        let mut o = at(now, interval);
        o.samples = &dense;
        o.hold_started = Some(began);
        // Budget of 36 s is not reached by a 15 s hold.
        assert_eq!(guard.observe(&o), expected, "{now}");
    }
}

#[test]
fn hold_started_before_the_interval_counts_from_its_original_start() {
    let mut guard = OnlineGuard::new(600 * S);
    let mut o = at(1_000 * S + 1, (1_000 * S, 1_600 * S));
    o.hold_started = Some(1_000 * S - 14_999_998);
    assert_eq!(guard.observe(&o), None);
    o.now = 1_000 * S + 2;
    assert_eq!(guard.observe(&o), Some(CONTINUOUS_HOLD));
}

#[test]
fn finished_hold_is_remembered_between_calls_even_when_missed_while_ongoing() {
    let mut guard = OnlineGuard::new(3600 * S);
    let interval = (100 * S, 3_700 * S);
    let dense = steady(100 * S, 201 * S, 100_000);
    let mut o = at(200 * S, interval);
    o.samples = &dense;
    o.last_hold = Some((150 * S, 150 * S + 14_999_999));
    assert_eq!(guard.observe(&o), None);
    let mut guard = OnlineGuard::new(3600 * S);
    o.last_hold = Some((150 * S, 165 * S));
    assert_eq!(guard.observe(&o), Some(CONTINUOUS_HOLD));
}

#[test]
fn natural_budget_is_strictly_greater_and_clips_to_the_interval() {
    let interval = (1_000 * S, 1_600 * S);
    let dense = steady(1_000 * S, 1_101 * S, 100_000);
    let mut guard = OnlineGuard::new(600 * S);
    let mut o = at(1_100 * S, interval);
    o.samples = &dense;
    // Two finished holds summing to exactly the budget are allowed.
    o.last_hold = Some((1_010 * S, 1_013 * S));
    assert_eq!(guard.observe(&o), None);
    o.last_hold = Some((1_020 * S, 1_023 * S));
    assert_eq!(guard.observe(&o), None);
    // One more microsecond of ongoing hold exceeds it.
    o.last_hold = Some((1_020 * S, 1_023 * S));
    o.hold_started = Some(1_050 * S);
    o.now = 1_050 * S;
    assert_eq!(guard.observe(&o), None);
    o.now = 1_050 * S + 1;
    assert_eq!(guard.observe(&o), Some(NATURAL_HOLD_BUDGET));
    // A hold straddling the start only counts its overlap.
    let mut guard = OnlineGuard::new(600 * S);
    let mut o = at(1_100 * S, interval);
    o.samples = &dense;
    o.last_hold = Some((990 * S, 1_006 * S));
    assert_eq!(guard.observe(&o), Some(CONTINUOUS_HOLD)); // 16 s since its start
    let mut guard = OnlineGuard::new(600 * S);
    o.last_hold = Some((995 * S, 1_006 * S));
    assert_eq!(guard.observe(&o), None); // 11 s long, 6 s inside
    o.last_hold = Some((995 * S, 1_006 * S + 1));
    let mut guard = OnlineGuard::new(600 * S);
    assert_eq!(guard.observe(&o), Some(NATURAL_HOLD_BUDGET));
}

#[test]
fn hold_before_the_interval_or_after_its_end_is_ignored() {
    let mut guard = OnlineGuard::new(600 * S);
    let dense = steady(1_000 * S, 1_600 * S, 100_000);
    let mut o = at(1_700 * S, (1_000 * S, 1_600 * S));
    o.samples = &dense;
    o.last_hold = Some((900 * S, 999 * S));
    assert_eq!(guard.observe(&o), None);
    o.last_hold = Some((1_600 * S, 1_700 * S));
    assert_eq!(guard.observe(&o), None);
}

#[test]
fn window_is_closed_only_when_it_ends_at_or_before_now() {
    let interval = (100 * S, 700 * S);
    let samples: Vec<_> = (0..49).map(|i| sample(100 * S + i * 200_000, 1)).collect();
    let mut guard = OnlineGuard::new(600 * S);
    let mut o = at(110 * S - 1, interval);
    o.samples = &samples;
    assert_eq!(
        min_closed_window(&samples, 100 * S, 700 * S, 110 * S - 1),
        usize::MAX
    );
    assert_eq!(guard.observe(&o), None);
    let mut guard = OnlineGuard::new(600 * S);
    o.now = 110 * S;
    assert_eq!(min_closed_window(&samples, 100 * S, 700 * S, 110 * S), 49);
    assert_eq!(guard.observe(&o), Some(WINDOW_BELOW_50));
    let fifty: Vec<_> = (0..50).map(|i| sample(100 * S + i * 199_000, 1)).collect();
    let mut guard = OnlineGuard::new(600 * S);
    o.samples = &fifty;
    assert_eq!(guard.observe(&o), None);
}

#[test]
fn a_shifted_window_is_checked_not_only_tumbling_windows() {
    // Both tumbling windows hold 50 results, but a window that starts just
    // after the first burst (t = last burst time + 1) holds none.
    let start = 100 * S;
    let mut samples: Vec<_> = (0..50).map(|i| sample(start + i * 1_000, 1)).collect();
    samples.extend((0..50).map(|i| sample(start + 19_900_000 + i * 1_000, 1)));
    let tumbling = |from: u64| {
        samples
            .iter()
            .filter(|s| (from..from + 10 * S).contains(&s.published_us))
            .count()
    };
    assert_eq!((tumbling(start), tumbling(start + 10 * S)), (50, 50));
    assert_eq!(
        min_closed_window(&samples, start, start + 600 * S, start + 20 * S),
        0
    );
}

#[test]
fn candidate_points_match_a_brute_force_scan() {
    let (start, end) = (0, 30 * S);
    let mut seed = 12345u64;
    let mut samples = vec![];
    for ms in 0..30_000u64 {
        seed = seed
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        if (seed >> 33) % 100 < 18 {
            samples.push(sample(ms * 1_000, 1));
        }
    }
    let times: Vec<u64> = samples.iter().map(|s| s.published_us).collect();
    for now in [10 * S, 17 * S + 500, 30 * S, 45 * S] {
        let last = now.min(end) - WINDOW_US;
        let mut brute = usize::MAX;
        for t in (start..=last).step_by(500).chain(1..=1) {
            let n = times
                .iter()
                .filter(|p| (t..t + WINDOW_US).contains(p))
                .count();
            brute = brute.min(n);
        }
        // Brute force on a 0.5 ms grid cannot beat the exact candidate set.
        assert!(
            min_closed_window(&samples, start, end, now) <= brute,
            "{now}"
        );
    }
}

#[test]
fn the_window_scan_runs_on_a_one_second_schedule_not_on_every_call() {
    let interval = (100 * S, 700 * S);
    let samples: Vec<_> = (0..49).map(|i| sample(100 * S + i * 200_000, 1)).collect();
    let mut guard = OnlineGuard::new(600 * S);
    let mut o = at(109_900_000, interval);
    o.samples = &samples;
    assert_eq!(guard.observe(&o), None); // first call consumes the schedule
    o.now = 110 * S;
    assert_eq!(guard.observe(&o), None); // closed now, but not yet due
    o.now = 110_800_000;
    assert_eq!(guard.observe(&o), None);
    o.now = 110_900_000;
    assert_eq!(guard.observe(&o), Some(WINDOW_BELOW_50));
}

#[test]
fn protection_limits_are_exclusive_boundaries() {
    let interval = (100 * S, 700 * S);
    let ok = steady(100 * S, 101 * S, 100_000);
    for (age, expected) in [(500_000, None), (500_001, Some(PROTECTION_VIOLATION))] {
        let mut guard = OnlineGuard::new(600 * S);
        let samples = [sample(100 * S + 1, age)];
        let mut o = at(100 * S + 2, interval);
        o.samples = &samples;
        assert_eq!(guard.observe(&o), expected);
    }
    let _ = ok;
    for (protection, expected) in [
        (
            Protection {
                rtt_us: Some(1_000_000),
                ..Default::default()
            },
            None,
        ),
        (
            Protection {
                rtt_us: Some(1_000_001),
                ..Default::default()
            },
            Some(PROTECTION_VIOLATION),
        ),
        (
            Protection {
                sync_age_us: Some(10_000_000),
                ..Default::default()
            },
            None,
        ),
        (
            Protection {
                sync_age_us: Some(10_000_001),
                ..Default::default()
            },
            Some(PROTECTION_VIOLATION),
        ),
        (
            Protection {
                drift_violation: true,
                ..Default::default()
            },
            Some(PROTECTION_VIOLATION),
        ),
        (Protection::default(), None),
    ] {
        let mut guard = OnlineGuard::new(600 * S);
        let mut o = at(100 * S + 2, interval);
        o.protection = protection;
        assert_eq!(guard.observe(&o), expected, "{protection:?}");
    }
}

#[test]
fn a_stale_published_result_is_checked_once_even_after_later_results() {
    let interval = (100 * S, 700 * S);
    let mut guard = OnlineGuard::new(600 * S);
    let first = [sample(100 * S, 100_000)];
    let mut o = at(100 * S + 1, interval);
    o.samples = &first;
    assert_eq!(guard.observe(&o), None);
    let later = [
        first[0],
        sample(100 * S + 10, 500_001),
        sample(100 * S + 20, 100_000),
    ];
    o.samples = &later;
    assert_eq!(guard.observe(&o), Some(PROTECTION_VIOLATION));
}

#[test]
fn protection_is_detected_within_two_seconds_with_publication_fully_stopped() {
    let interval = (100 * S, 3_700 * S);
    // The last accepted clock reply predates the interval by 9 s; no result
    // is ever published, and the controller passes every 50 ms.
    let violation_at = S; // sync age passes 10 s one second into the interval
    let mut guard = OnlineGuard::new(3600 * S);
    let mut detected = None;
    for step in 0..100u64 {
        let elapsed = step * 50_000;
        let mut o = at(100 * S + elapsed, interval);
        o.protection.sync_age_us = Some(9 * S + elapsed);
        if guard.observe(&o).is_some() {
            detected = Some(elapsed);
            break;
        }
    }
    let detected = detected.expect("violation detected");
    assert!(detected > violation_at && detected - violation_at <= 2 * S);
    // Even a coarse one second pass finds it within two seconds.
    let mut guard = OnlineGuard::new(3600 * S);
    let mut detected = None;
    for second in 0..5u64 {
        let mut o = at(100 * S + second * S, interval);
        o.protection.sync_age_us = Some(9 * S + second * S);
        if guard.observe(&o) == Some(PROTECTION_VIOLATION) {
            detected = Some(second * S);
            break;
        }
    }
    assert!(detected.unwrap() - violation_at <= 2 * S);
}

#[test]
fn nothing_is_evaluated_before_the_interval_is_known_and_order_is_fixed() {
    let mut guard = OnlineGuard::new(600 * S);
    let mut o = at(5 * S, (0, 0));
    o.interval = None;
    o.hold_started = Some(0);
    o.protection.drift_violation = true;
    assert_eq!(guard.observe(&o), None);
    // Simultaneous conditions report the first code of the table.
    let mut guard = OnlineGuard::new(600 * S);
    let mut o = at(20 * S, (1, 600 * S));
    o.hold_started = Some(1);
    o.protection.drift_violation = true;
    assert_eq!(guard.observe(&o), Some(CONTINUOUS_HOLD));
}

#[test]
fn abort_reasons_are_closed_set_and_exit_code_is_fixed() {
    for reason in [
        "continuous_hold",
        "natural_hold_budget_exceeded",
        "window_below_50",
        "protection_violation",
    ] {
        assert!(is_abort_reason(reason));
    }
    for reason in ["completed", "ack_lost", "tracking_recovery_timeout", ""] {
        assert!(!is_abort_reason(reason));
    }
    assert_eq!(EXIT_CODE, 20);
}
