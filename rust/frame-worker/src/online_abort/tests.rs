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
        publish_refused_at: None,
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
    let now = 100 * S + 2;
    for (protection, expected) in [
        (
            Protection {
                pending_since_us: Some(now - 1_000_000),
                ..Default::default()
            },
            None,
        ),
        (
            Protection {
                pending_since_us: Some(now - 1_000_001),
                ..Default::default()
            },
            Some(PROTECTION_VIOLATION),
        ),
        (
            Protection {
                last_sync_us: Some(now - 10_000_000),
                ..Default::default()
            },
            None,
        ),
        (
            Protection {
                last_sync_us: Some(now - 10_000_001),
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
        let mut o = at(now, interval);
        o.protection = protection;
        assert_eq!(guard.observe(&o), expected, "{protection:?}");
    }
}

fn accepted(received_us: u64, age_high_us: i128) -> Protection {
    Protection {
        input: Some(AcceptedInput {
            received_us,
            age_high_us,
        }),
        ..Default::default()
    }
}

#[test]
fn input_age_is_aged_to_the_current_time_with_an_exclusive_500ms_limit() {
    let interval = (100 * S, 700 * S);
    // 100 ms old on arrival: exactly 500 ms after 400 ms, over it one µs later.
    for (elapsed, expected) in [
        (399_999, None),
        (400_000, None),
        (400_001, Some(PROTECTION_VIOLATION)),
    ] {
        let mut guard = OnlineGuard::new(600 * S);
        let mut o = at(100 * S + elapsed, interval);
        o.protection = accepted(100 * S, 100_000);
        assert_eq!(guard.observe(&o), expected, "{elapsed}");
    }
    // No input accepted yet is no evidence, not a violation.
    let dense = steady(100 * S, 301 * S, 100_000);
    let mut guard = OnlineGuard::new(600 * S);
    let mut o = at(300 * S, interval);
    o.samples = &dense;
    assert_eq!(guard.observe(&o), None);
}

#[test]
fn a_stalled_input_is_detected_within_two_seconds_with_publication_fully_stopped() {
    let interval = (100 * S, 3_700 * S);
    let violation_at = 400_000; // 100 ms arrival bound reaches 500 ms
    for pass in [50_000, S] {
        let mut guard = OnlineGuard::new(3600 * S);
        let mut detected = None;
        for step in 0..100u64 {
            let elapsed = step * pass;
            let mut o = at(100 * S + elapsed, interval);
            o.protection = accepted(100 * S, 100_000);
            if guard.observe(&o) == Some(PROTECTION_VIOLATION) {
                detected = Some(elapsed);
                break;
            }
        }
        let detected = detected.expect("violation detected");
        assert!(detected > violation_at && detected - violation_at <= 2 * S);
    }
}

#[test]
fn a_refused_publication_is_a_violation_whenever_it_happened() {
    let interval = (100 * S, 700 * S);
    let dense = steady(100 * S, 301 * S, 100_000);
    for at_us in [100 * S - 1, 100 * S, 700 * S - 1, 700 * S] {
        let mut guard = OnlineGuard::new(600 * S);
        let mut o = at(300 * S, interval);
        o.samples = &dense;
        assert_eq!(guard.observe(&o), None);
        o.publish_refused_at = Some(at_us);
        assert_eq!(guard.observe(&o), Some(PROTECTION_VIOLATION), "{at_us}");
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
    for pass in [50_000, S] {
        let mut guard = OnlineGuard::new(3600 * S);
        let mut detected = None;
        for step in 0..100u64 {
            let elapsed = step * pass;
            let mut o = at(100 * S + elapsed, interval);
            o.protection.last_sync_us = Some(91 * S);
            if guard.observe(&o).is_some() {
                detected = Some(elapsed);
                break;
            }
        }
        let detected = detected.expect("violation detected");
        assert!(detected > violation_at && detected - violation_at <= 2 * S);
    }
}

#[test]
fn only_protection_is_judged_before_the_interval_is_known_and_order_is_fixed() {
    let mut guard = OnlineGuard::new(600 * S);
    let mut o = at(5 * S, (0, 0));
    o.interval = None;
    o.hold_started = Some(0);
    assert_eq!(guard.observe(&o), None); // holds and windows need the interval
    o.protection.drift_violation = true;
    assert_eq!(guard.observe(&o), Some(PROTECTION_VIOLATION));
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

/// Synthetic receiver loop: a frame every `frame_us`, accepted with a frozen
/// arrival age of `arrival_age_us`, one controller pass every 50 ms. During a
/// hold no frame is accepted (the old generation is rejected) until
/// `recovered + ack_gap_us`, when the new generation has been acknowledged.
/// Nothing is published during the hold. The protection view is exactly what
/// the receiver stores: the last accepted input, aged to the current time.
struct Timeline {
    frame_us: u64,
    arrival_age_us: i128,
    hold: Option<(u64, u64)>,
    ack_gap_us: u64,
    /// No input is accepted from this time on (a stalled stream, no hold).
    stall_from: Option<u64>,
}

impl Timeline {
    fn accepting(&self, t: u64) -> bool {
        if self.stall_from.is_some_and(|from| t >= from) {
            return false;
        }
        match self.hold {
            Some((began, recovered)) => t < began || t >= recovered + self.ack_gap_us,
            None => true,
        }
    }

    /// First abort code, with its time, over `[start, until)`.
    fn run(&self, interval: (u64, u64), until: u64) -> Option<(&'static str, u64)> {
        let (start, end) = interval;
        // Results every 20 ms outside the hold keep every window far above 50,
        // so only the freshness behaviour is under test.
        let samples: Vec<Sample> = (0..)
            .map(|i| start + i * 20_000)
            .take_while(|p| *p < until)
            .filter(|p| self.hold.is_none_or(|(b, r)| *p < b || *p >= r))
            .map(|p| sample(p, 100_000))
            .collect();
        let mut guard = OnlineGuard::new(end - start);
        let mut input: Option<AcceptedInput> = None;
        let mut next_frame = start;
        let mut now = start;
        while now < until {
            while next_frame <= now {
                if self.accepting(next_frame) {
                    input = Some(AcceptedInput {
                        received_us: next_frame,
                        age_high_us: self.arrival_age_us,
                    });
                }
                next_frame += self.frame_us;
            }
            let published = samples.partition_point(|s| s.published_us <= now);
            let mut o = at(now, interval);
            o.samples = &samples[..published];
            o.protection = Protection {
                last_sync_us: Some(now - now % 1_000_000),
                input,
                ..Protection::default()
            };
            if let Some((began, recovered)) = self.hold {
                if now >= recovered {
                    o.last_hold = Some((began, recovered));
                } else if now >= began {
                    o.hold_started = Some(began);
                }
            }
            if let Some(code) = guard.observe(&o) {
                return Some((code, now));
            }
            now += 50_000;
        }
        None
    }
}

fn timeline(hold: Option<(u64, u64)>) -> Timeline {
    Timeline {
        frame_us: 190_000,
        arrival_age_us: 300_000,
        hold,
        ack_gap_us: 100_000,
        stall_from: None,
    }
}

#[test]
fn steady_input_with_a_300ms_arrival_age_and_190ms_frames_is_not_a_violation() {
    // The age bound peaks at 300 ms + (190 ms - 1 us) just before the next frame.
    assert_eq!(timeline(None).run((100 * S, 700 * S), 160 * S), None);
    let mut slower = timeline(None);
    slower.frame_us = 260_000; // 300 ms + 250 ms at the last pass is over the limit
    assert_eq!(
        slower.run((100 * S, 700 * S), 160 * S).map(|c| c.0),
        Some(PROTECTION_VIOLATION)
    );
}

#[test]
fn a_stalled_stream_is_a_violation_without_any_hold_and_with_publication_stopped() {
    let mut stalled = timeline(None);
    stalled.stall_from = Some(300 * S);
    // The last input arrived at or before 300 s with a 300 ms bound; the bound
    // passes 500 ms about 200 ms later and the next 50 ms pass sees it.
    let (code, when) = stalled.run((100 * S, 700 * S), 310 * S).unwrap();
    assert_eq!(code, PROTECTION_VIOLATION);
    assert!(when <= 300 * S + 400_000, "{when}");
}

/// Plan-conformant behaviour, stated as a fact and not worked around: section
/// 2-9(iv) evaluates the freshness of the most recently accepted input at the
/// current time while publication is stopped, and no input is accepted during
/// a hold. A hold therefore ends in A14 about 200 ms after its last accepted
/// input, long before any hold limit and before a new generation can be
/// acknowledged. Section 8.4 (a natural hold of 3 s recovered by the ACK of
/// the new generation) cannot be reached under that rule.
#[test]
fn plan_conformant_natural_holds_of_3_6_and_15_seconds_end_in_protection_violation() {
    let interval = (100 * S, 700 * S);
    for hold_s in [3, 6, 15] {
        let began = 300 * S;
        let hold = timeline(Some((began, began + hold_s * S)));
        let (code, when) = hold.run(interval, 340 * S).unwrap();
        assert_eq!(code, PROTECTION_VIOLATION, "{hold_s} s hold");
        // The last accepted frame is within 190 ms before the hold; the 500 ms
        // bound is crossed 200 ms after it and the next 50 ms pass sees it.
        assert!(
            when > began && when <= began + 250_000,
            "{hold_s} s hold: {when}"
        );
    }
    // The same with a slow ACK of the new generation (2 s after the hold ends).
    let mut slow_ack = timeline(Some((300 * S, 306 * S)));
    slow_ack.ack_gap_us = 2_000_000;
    let (code, when) = slow_ack.run(interval, 340 * S).unwrap();
    assert_eq!(code, PROTECTION_VIOLATION);
    assert!(when <= 300 * S + 250_000, "{when}");
    // The hold limits were not what stopped it: the hold is still young.
    let (code, _) = timeline(Some((300 * S, 303 * S)))
        .run(interval, 300 * S + 250_000)
        .unwrap();
    assert_ne!(code, CONTINUOUS_HOLD);
    assert_ne!(code, NATURAL_HOLD_BUDGET);
}

#[test]
fn plan_conformant_age_check_has_no_hold_or_generation_exemption() {
    // One input, 300 ms bound at arrival: judged at the current time whatever
    // happens to the mode in the meantime (a hold, a new generation).
    let view = accepted(10 * S, 300_000);
    assert!(!view.violated(10 * S + 199_999));
    assert!(view.violated(10 * S + 200_001));
    assert!(view.violated(30 * S)); // seconds later, still the same input
}

#[test]
fn an_undefined_clock_estimate_is_a_violation_only_inside_the_measurement_interval() {
    let unevaluable = Protection {
        drift_unevaluable: true,
        ..Protection::default()
    };
    // Before the interval is known the start gate is still waiting for it.
    let mut guard = OnlineGuard::new(600 * S);
    let mut before = at(50 * S, (100 * S, 700 * S));
    before.interval = None;
    before.protection = unevaluable;
    assert_eq!(guard.observe(&before), None);
    // Inside the interval it is never read as "no violation".
    let dense = steady(100 * S, 700 * S, 100_000);
    for now in [100 * S, 400 * S, 700 * S - 1] {
        let mut guard = OnlineGuard::new(600 * S);
        let mut o = at(now, (100 * S, 700 * S));
        o.protection = unevaluable;
        o.samples = &dense[..dense.partition_point(|s| s.published_us <= now)];
        assert_eq!(guard.observe(&o), Some(PROTECTION_VIOLATION), "{now}");
        // The same pass with an evaluable estimate is quiet.
        let mut guard = OnlineGuard::new(600 * S);
        o.protection = Protection::default();
        assert_eq!(guard.observe(&o), None, "{now}");
    }
}
