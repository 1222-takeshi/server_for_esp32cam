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
        publishing: true,
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
    // A start refused by the clock gate is not an online abort: the binary exits
    // with its ordinary non-zero code (3), not 20, and the report carries no
    // measurement. The checker maps that report to "start refused".
    for reason in [
        "completed",
        "ack_lost",
        "tracking_recovery_timeout",
        "clock_unresolved",
        "",
    ] {
        assert!(!is_abort_reason(reason));
    }
    assert_eq!(EXIT_CODE, 20);
}

/// Synthetic receiver loop: a frame every `frame_us`, accepted with a frozen
/// arrival age of `arrival_age_us`, one controller pass every 50 ms. During a
/// hold no frame is accepted (the old generation is rejected) until the hold
/// ends, which in the receiver happens only after the new generation has been
/// acknowledged and a result has been published. Nothing is published during
/// the hold. The protection view is exactly what the receiver stores: the last
/// accepted input, aged to the current time. `publishing` is what the live
/// binary derives: no hold in progress.
struct Timeline {
    frame_us: u64,
    arrival_age_us: i128,
    hold: Option<(u64, u64)>,
    /// No input is accepted from this time on (a stalled stream, no hold).
    stall_from: Option<u64>,
    /// Hold start as the native mode declares it. `None` follows `hold`; a
    /// later value models the declaration (last publication + 500 ms) coming
    /// after the input has already gone silent.
    declared_at: Option<u64>,
}

impl Timeline {
    fn accepting(&self, t: u64) -> bool {
        if self.stall_from.is_some_and(|from| t >= from) {
            return false;
        }
        match self.hold {
            Some((began, recovered)) => t < began || t >= recovered,
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
            .filter(|p| {
                self.accepting(*p) && self.stall_from.is_none_or(|from| *p < from + 300_000)
            })
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
            // The hold ends with a published result, hence an accepted input.
            if let Some((_, recovered)) = self.hold
                && recovered <= now
                && input.is_none_or(|i| i.received_us < recovered)
            {
                input = Some(AcceptedInput {
                    received_us: recovered,
                    age_high_us: self.arrival_age_us,
                });
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
                let began = self.declared_at.unwrap_or(began);
                if now >= recovered {
                    o.last_hold = Some((began, recovered));
                } else if now >= began {
                    o.hold_started = Some(began);
                }
            }
            o.publishing = o.hold_started.is_none();
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
        stall_from: None,
        declared_at: None,
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

/// v2 spec D1: the tick freshness term is judged only while publishing. A
/// hold has no accepted input by construction (the old generation is rejected
/// until the new one is acknowledged), so its stale last input is not read as
/// a violation; the hold limits and the recovery condition bound it instead.
#[test]
fn natural_holds_of_3_and_6_seconds_do_not_trip_the_tick_freshness_term() {
    let interval = (100 * S, 700 * S);
    for hold_s in [3, 6] {
        let began = 300 * S;
        let hold = timeline(Some((began, began + hold_s * S)));
        // Runs well past the recovery: nothing fires, however slow the new
        // generation's ACK was (no frame is accepted for the whole hold).
        assert_eq!(hold.run(interval, 340 * S), None, "{hold_s} s hold");
    }
}

#[test]
fn a_15_second_hold_is_a11_and_an_over_budget_hold_is_a12_not_a_freshness_abort() {
    // Budget of 3600 s is 36 s, so only the 15 s limit can fire. The accepted
    // input is long stale here and the term is out of scope: A11 is what ends it.
    let interval = (1_000 * S, 4_600 * S);
    let began = 1_200 * S;
    let dense = steady(1_000 * S, 1_300 * S, 100_000);
    for (now, expected) in [
        (began + 15 * S - 1, None),
        (began + 15 * S, Some(CONTINUOUS_HOLD)),
    ] {
        let mut guard = OnlineGuard::new(3600 * S);
        let mut o = at(now, interval);
        o.samples = &dense;
        o.hold_started = Some(began);
        o.publishing = false;
        o.protection = accepted(began - 100_000, 300_000);
        assert_eq!(guard.observe(&o), expected, "{now}");
    }
    // With nothing published, the same hold meets the 10 s window rule first.
    let silent = timeline(Some((300 * S, 320 * S)));
    assert_eq!(
        silent.run((100 * S, 3_700 * S), 340 * S),
        Some((WINDOW_BELOW_50, 310 * S))
    );
    // Budget of 600 s is 6 s: a 7 s hold exceeds it first, before 15 s.
    let interval = (100 * S, 700 * S);
    let a12 = timeline(Some((300 * S, 307 * S)));
    let (code, when) = a12.run(interval, 340 * S).unwrap();
    assert_eq!(code, NATURAL_HOLD_BUDGET);
    assert!((306 * S..=306 * S + 50_000).contains(&when), "{when}");
}

#[test]
fn input_age_view_is_unchanged_but_judged_only_while_publishing() {
    // One input, 300 ms bound at arrival: its age keeps growing whatever
    // happens to the mode; only whether the term is evaluated depends on it.
    let view = accepted(10 * S, 300_000);
    assert!(!view.violated(10 * S + 199_999, true));
    assert!(!view.violated(10 * S + 200_000, true));
    assert!(view.violated(10 * S + 200_001, true));
    assert!(view.violated(30 * S, true));
    assert!(!view.violated(30 * S, false)); // not publishing: out of scope
    assert_eq!(view.input_age_high_us(30 * S), Some(20_300_000));
}

#[test]
fn tick_freshness_is_exactly_500ms_inclusive_while_publishing() {
    let interval = (100 * S, 700 * S);
    for (elapsed, expected) in [
        (399_999, None),
        (400_000, None),
        (400_001, Some(PROTECTION_VIOLATION)),
    ] {
        let mut guard = OnlineGuard::new(600 * S);
        let mut o = at(100 * S + elapsed, interval);
        o.protection = accepted(100 * S, 100_000);
        o.publishing = true;
        assert_eq!(guard.observe(&o), expected, "{elapsed}");
        // The same time outside publishing is out of scope.
        let mut guard = OnlineGuard::new(600 * S);
        o.publishing = false;
        assert_eq!(guard.observe(&o), None, "{elapsed} not publishing");
    }
}

#[test]
fn a_published_sample_over_500ms_is_still_a_violation_during_a_hold() {
    let interval = (100 * S, 700 * S);
    for (age, expected) in [(500_000, None), (500_001, Some(PROTECTION_VIOLATION))] {
        let mut guard = OnlineGuard::new(600 * S);
        let samples = [sample(100 * S + 1, age)];
        let mut o = at(100 * S + 2, interval);
        o.samples = &samples;
        o.hold_started = Some(100 * S);
        o.publishing = false;
        assert_eq!(guard.observe(&o), expected, "{age}");
    }
}

#[test]
fn clock_limits_and_an_unevaluable_estimate_are_still_violations_during_a_hold() {
    let interval = (100 * S, 700 * S);
    let now = 100 * S + 2;
    let unevaluable = Protection {
        drift_unevaluable: true,
        ..Protection::default()
    };
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
        (unevaluable, Some(PROTECTION_VIOLATION)),
    ] {
        let mut guard = OnlineGuard::new(600 * S);
        let mut o = at(now, interval);
        o.protection = protection;
        o.hold_started = Some(now - S);
        o.publishing = false;
        assert_eq!(guard.observe(&o), expected, "{protection:?}");
    }
}

/// Known risk K2 (v2 spec D3), fixed as a fact and not worked around: a hold is
/// declared 500 ms after the last publication, but the tick freshness term is
/// "arrival age bound + time since the last accepted input" and crosses 500 ms
/// earlier (the stored live runs: about 127-311 ms after the last
/// publication). When the input goes silent the A14 therefore fires before the
/// hold is declared, and a hold that follows a silent input (`frames_expired`)
/// is not saved by the v2 exemption. A fail in the live stage is a FAIL.
#[test]
fn known_risk_k2_freshness_fires_before_the_hold_is_declared() {
    let interval = (100 * S, 700 * S);
    let silent_from = 300 * S;
    let mut stalled = timeline(Some((silent_from, silent_from + 6 * S)));
    // 300 ms bound on arrival; the last frame is at most 190 ms before 300 s.
    // The hold is declared only 500 ms after the last publication (~300 s).
    stalled.declared_at = Some(silent_from + 500_000);
    let (code, when) = stalled.run(interval, 340 * S).unwrap();
    assert_eq!(code, PROTECTION_VIOLATION);
    assert!(
        when < silent_from + 500_000,
        "fires before the declaration: {when}"
    );
    assert!(when <= silent_from + 250_000, "{when}");
    // Without the declaration lag (the hold starts at the silence) it is quiet.
    assert_eq!(
        timeline(Some((silent_from, silent_from + 6 * S))).run(interval, 340 * S),
        None
    );
}

#[test]
fn normal_publication_with_about_150ms_of_margin_has_no_false_alarm() {
    // 190 ms frames with a 300 ms arrival bound peak just under 490 ms.
    let steady = timeline(None);
    assert_eq!(steady.run((100 * S, 700 * S), 400 * S), None);
    // And across a short hold followed by normal publication.
    let held = timeline(Some((200 * S, 204 * S)));
    assert_eq!(held.run((100 * S, 700 * S), 400 * S), None);
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

#[test]
fn publishing_is_tracking_requested_without_a_hold_in_progress() {
    use crate::operating_mode::OperatingMode;
    let mut mode = OperatingMode::new_with_tracking_recovery(10, || 1_000, true).unwrap();
    mode.hello(1, 0).unwrap();
    assert!(!publishing(&mode)); // initial Eco
    mode.request(Mode::Tracking, mode.generation(), true)
        .unwrap();
    assert!(publishing(&mode));
    mode.protect("frames_expired").unwrap();
    assert!(mode.tracking_hold_started().is_some());
    assert!(!publishing(&mode)); // hold in progress
    mode.protect("resource_thermal_fault").unwrap();
    assert!(!publishing(&mode)); // back to Eco
}

fn tracking_state(hold: bool) -> State {
    let mut s =
        State::new_with_tracking_recovery(10, std::sync::Arc::new(|| 10 * S), true).unwrap();
    s.mode.hello(1, 0).unwrap();
    s.mode
        .request(Mode::Tracking, s.mode.generation(), true)
        .unwrap();
    if hold {
        s.mode.protect("frames_expired").unwrap();
    }
    // The accepted input is 9.7 s old at the evaluation time.
    s.protection.input = Some(AcceptedInput {
        received_us: 300_000,
        age_high_us: 100_000,
    });
    s
}

/// The State-to-Observation wiring (v2 spec D1), judged through the guard.
#[test]
fn observation_judges_the_tick_freshness_term_only_while_publishing() {
    let check = |s: &State| OnlineGuard::new(60 * S).observe(&observation(10 * S, None, s));
    // Initial Eco: out of scope.
    let mut eco = tracking_state(false);
    eco.mode =
        crate::operating_mode::OperatingMode::new_with_tracking_recovery(10, || 10 * S, true)
            .unwrap();
    eco.mode.hello(1, 0).unwrap();
    assert!(!observation(10 * S, None, &eco).publishing);
    assert_eq!(check(&eco), None);
    // Tracking without a hold: the stale input is a violation.
    let publishing_state = tracking_state(false);
    assert!(observation(10 * S, None, &publishing_state).publishing);
    assert_eq!(check(&publishing_state), Some(PROTECTION_VIOLATION));
    // Tracking with a hold in progress: the same input is not judged.
    let held = tracking_state(true);
    let o = observation(10 * S, None, &held);
    assert!(!o.publishing && o.hold_started.is_some());
    assert_eq!(check(&held), None);
    // The clock limits still apply during the hold.
    let mut clock = tracking_state(true);
    clock.protection.pending_since_us = Some(0);
    assert_eq!(check(&clock), Some(PROTECTION_VIOLATION));
}

#[test]
fn observation_carries_the_holds_samples_and_refusal_of_the_state() {
    let mut s = tracking_state(true);
    s.record_test_sample(sample(5 * S, 100_000));
    s.publish_refused_at = Some(7 * S);
    let o = observation(10 * S, Some((S, 20 * S)), &s);
    assert_eq!(o.now, 10 * S);
    assert_eq!(o.interval, Some((S, 20 * S)));
    assert_eq!(o.hold_started, s.mode.tracking_hold_started());
    assert_eq!(o.last_hold, s.mode.last_tracking_hold());
    assert_eq!(o.samples.len(), 1);
    assert_eq!(o.publish_refused_at, Some(7 * S));
    // A refused publication is a violation on its own.
    assert_eq!(
        OnlineGuard::new(60 * S).observe(&o),
        Some(PROTECTION_VIOLATION)
    );
}
