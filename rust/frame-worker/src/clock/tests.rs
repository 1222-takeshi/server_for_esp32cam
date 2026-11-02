use super::*;
use serde_json::Value;

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

#[test]
fn invalid_exchanges_and_full_width_offsets() {
    for args in [
        [0, 1, 1, 1],
        [1, 0, 1, 1],
        [1, 1, 0, 1],
        [1, 1, 1, 0],
        [1, 1, 2, 1],
        [1, 1, 1, 1_000_002],
    ] {
        assert!(ClockSample::from_exchange(args[0], args[1], args[2], args[3]).is_err());
    }
    let extreme = ClockSample::from_exchange(1, u64::MAX, 1, 1).unwrap();
    assert_eq!(
        extreme.age_bounds(&frame(1, 1), 1),
        Some((18_443_054_724_894_809_703, 18_450_433_422_524_293_525))
    );
    assert!(extreme.age_bounds(&frame(1, 1), 0).is_none());
}

#[test]
fn bounded_ring_and_rejected_exchange_do_not_mutate() {
    let mut window = ClockWindow::default();
    for i in 1..=20 {
        window
            .add(ClockSample::from_exchange(1, i, i, i).unwrap())
            .unwrap();
    }
    assert_eq!(window.samples.len(), 8);
    assert_eq!(window.samples.front().unwrap().received_us, 13);
    assert!(
        window
            .add(ClockSample::from_exchange(2, 19, 19, 19).unwrap())
            .is_err()
    );
    assert_eq!(window.samples.len(), 8);
    window
        .add(ClockSample::from_exchange(2, 20, 20, 20).unwrap())
        .unwrap();
    assert_eq!(window.samples.len(), 1);
    assert!(window.age_bounds(&frame(1, 20), 20).is_none());
}

#[test]
fn python_reference_fixtures() {
    // Generated only from script.frame_protocol, checked for drift in CI.
    for line in include_str!("../../fixtures/clock.jsonl").lines() {
        let case: Value = serde_json::from_str(line).unwrap();
        let mut window = ClockWindow::default();
        for (args, accepted) in case["exchanges"]
            .as_array()
            .unwrap()
            .iter()
            .zip(case["accepted"].as_array().unwrap())
        {
            let a: Vec<_> = args
                .as_array()
                .unwrap()
                .iter()
                .map(|x| x.as_u64().unwrap())
                .collect();
            let result =
                ClockSample::from_exchange(a[0], a[1], a[2], a[3]).and_then(|s| window.add(s));
            assert_eq!(result.is_ok(), accepted.as_bool().unwrap(), "{case}");
        }
        let f = frame(
            case["frame"][0].as_u64().unwrap(),
            case["frame"][1].as_u64().unwrap(),
        );
        let actual = window.age_bounds(&f, case["now"].as_u64().unwrap());
        let expected = case["expected"].as_array().map(|b| {
            (
                b[0].as_str().unwrap().parse().unwrap(),
                b[1].as_str().unwrap().parse().unwrap(),
            )
        });
        assert_eq!(actual, expected, "{case}");
    }
}

#[test]
fn drift_bound_contradiction_is_detected_with_the_same_margin_and_rounding() {
    // Offset interval [sent - device, received - device] for each exchange.
    let sample = |device: u64, sent: u64, received: u64| {
        ClockSample::from_exchange(1, device, sent, received).unwrap()
    };
    let mut window = ClockWindow::default();
    assert!(!window.drift_inconsistent());
    window.add(sample(1_000_000, 1_000_100, 1_000_200)).unwrap();
    // 10 s of device time allows 2000 us of drift; a gap of exactly 2000 is fine.
    window
        .add(sample(11_000_000, 11_002_200, 11_002_300))
        .unwrap();
    assert!(!window.drift_inconsistent());
    let mut window = ClockWindow::default();
    window.add(sample(1_000_000, 1_000_100, 1_000_200)).unwrap();
    window
        .add(sample(11_000_000, 11_002_201, 11_002_301))
        .unwrap();
    assert!(window.drift_inconsistent());
    // Windows of one sample, and samples of different boots, never contradict.
    let mut single = ClockWindow::default();
    single.add(sample(1, 1, 2)).unwrap();
    assert!(!single.drift_inconsistent());
    let other = ClockSample::from_exchange(2, 11_000_000, 11_500_000, 11_500_001).unwrap();
    let mut mixed = ClockWindow::default();
    mixed.add(sample(1_000_000, 1_000_100, 1_000_200)).unwrap();
    mixed.add(other).unwrap();
    assert!(!mixed.drift_inconsistent());
}

fn exchange(device: u64, sent: u64, received: u64) -> ClockSample {
    ClockSample::from_exchange(1, device, sent, received).unwrap()
}

fn window_of(samples: &[ClockSample]) -> ClockWindow {
    let mut window = ClockWindow::default();
    for sample in samples {
        window.add(*sample).unwrap();
    }
    window
}

#[test]
fn midpoint_estimate_is_200_ppm_exclusive() {
    // Zero-width exchanges: the offset is exact. 100 s of device time at exactly
    // 200 ppm is 20_000 us.
    let base = exchange(1_000_000, 1_000_000, 1_000_000);
    let at = |extra: u64| {
        exchange(
            101_000_000,
            101_000_000 + 20_000 + extra,
            101_000_000 + 20_000 + extra,
        )
    };
    assert!(!window_of(&[base, at(0)]).drift_estimate_exceeded());
    assert!(window_of(&[base, at(1)]).drift_estimate_exceeded());
    // Below the limit, and the sign of the slope does not matter.
    let slower = exchange(101_000_000, 101_000_000 + 19_999, 101_000_000 + 19_999);
    assert!(!window_of(&[base, slower]).drift_estimate_exceeded());
    let behind = exchange(101_000_000, 101_000_000 - 20_001, 101_000_000 - 20_001);
    assert!(window_of(&[base, behind]).drift_estimate_exceeded());
}

#[test]
fn midpoint_estimate_is_stricter_than_the_interval_check() {
    // Two 20 ms wide exchanges 100 s apart whose midpoints differ by 30_000 us
    // (300 ppm): the intervals still overlap once widened by 200 ppm, so the
    // interval check stays quiet while the estimate fires.
    let a = exchange(1_000_000, 1_000_000, 1_020_000);
    let b = exchange(101_000_000, 101_030_000, 101_050_000);
    let window = window_of(&[a, b]);
    assert!(!window.drift_inconsistent());
    assert!(window.drift_estimate_exceeded());
}

#[test]
fn midpoint_estimate_needs_two_samples_and_a_long_enough_baseline() {
    assert!(!ClockWindow::default().drift_estimate_exceeded());
    assert!(!window_of(&[exchange(1_000_000, 1_000_000, 1_000_100)]).drift_estimate_exceeded());
    // 1 s apart with 2 ms wide exchanges and a 1 ms midpoint step (1000 ppm
    // looking): the round trip width alone explains it, so no estimate.
    let a = exchange(1_000_000, 1_000_000, 1_002_000);
    let b = exchange(2_000_000, 2_001_000, 2_003_000);
    assert!(!window_of(&[a, b]).drift_estimate_exceeded());
    // Same device time: no baseline at all.
    let c = exchange(1_000_000, 1_500_000, 1_500_000);
    assert!(!window_of(&[a, c]).drift_estimate_exceeded());
    // The resolution limit is inclusive: widths summing to 4000 us need 10 s.
    let wide = |dev: u64, step: u64| exchange(dev, dev + step, dev + step + 2_000);
    assert!(window_of(&[wide(1_000_000, 0), wide(11_000_000, 100_000)]).drift_estimate_exceeded());
    let wider = exchange(11_000_000, 11_100_000, 11_102_001);
    assert!(!window_of(&[wide(1_000_000, 0), wider]).drift_estimate_exceeded());
}

#[test]
fn midpoint_estimate_uses_the_longest_retained_baseline() {
    // Steady 100 ppm for 80 s, then a step: the last sample is 300 ppm from the first
    // while adjacent pairs are short and ambiguous.
    let samples: Vec<ClockSample> = (0..8u64)
        .map(|i| {
            let dev = 1_000_000 + i * 10_000_000;
            let off = if i == 7 { 24_000 } else { i * 1_000 };
            exchange(dev, dev + off, dev + off)
        })
        .collect();
    assert!(window_of(&samples).drift_estimate_exceeded());
    assert!(!window_of(&samples[..7]).drift_estimate_exceeded());
}

#[test]
fn midpoint_estimate_does_not_cross_a_boot_change() {
    let old = exchange(1_000_000, 1_000_000, 1_000_000);
    // Same wall of host time, a rebooted camera with an unrelated device clock.
    let new = ClockSample::from_exchange(2, 50_000, 101_000_000, 101_000_000).unwrap();
    let window = window_of(&[old, new]);
    assert!(!window.drift_estimate_exceeded());
    assert!(!window.drift_inconsistent());
    // After the change only the new boot's samples count.
    let later = ClockSample::from_exchange(2, 100_050_000, 201_000_000, 201_000_000).unwrap();
    assert!(!window_of(&[old, new, later]).drift_estimate_exceeded());
    let drifting = ClockSample::from_exchange(2, 100_050_000, 201_030_001, 201_030_001).unwrap();
    assert!(window_of(&[old, new, drifting]).drift_estimate_exceeded());
}

/// A camera polled every 50 ms of its own clock with a 5 ms round trip, as in
/// tracking. The host reads `ppm` parts per million ahead of the device.
/// Returns the exchanges `[from_s, to_s)` of one boot.
fn polled(boot: u64, from_s: u64, to_s: u64, ppm: u64, rtt_us: u64) -> Vec<ClockSample> {
    (from_s * 20..to_s * 20)
        .map(|k| {
            let device = 1_000_000 + k * 50_000;
            let host = 7_000_000_000 + device + (device * ppm + 500_000) / 1_000_000;
            ClockSample::from_exchange(boot, device, host - rtt_us / 2, host + rtt_us / 2).unwrap()
        })
        .collect()
}

fn feed(window: &mut ClockWindow, samples: &[ClockSample]) {
    for sample in samples {
        window.add(*sample).unwrap();
    }
}

#[test]
fn eight_retained_samples_at_50ms_cannot_resolve_200_ppm_but_the_references_can() {
    let samples = polled(1, 0, 60, 0, 5_000);
    let mut window = ClockWindow::default();
    feed(&mut window, &samples[..10]);
    // The retained eight span 0.35 s: nothing is resolvable yet.
    assert!(!window.drift_evaluable());
    assert!(!window.drift_estimate_exceeded());
    // Two widths of 5 ms resolve 200 ppm from 25 s of device time on (inclusive).
    let mut window = ClockWindow::default();
    feed(&mut window, &samples[..500]); // baseline 24.95 s
    assert!(!window.drift_evaluable());
    feed(&mut window, &samples[500..501]); // baseline 25.00 s
    assert!(window.drift_evaluable());
    // The eight retained samples alone never reach that, however long it runs.
    assert_eq!(window.samples.len(), 8);
}

#[test]
fn drift_above_200_ppm_is_detected_through_the_reference_at_50ms_polling() {
    for (ppm, exceeded) in [
        (250, true),
        (201, true),
        (200, false),
        (199, false),
        (0, false),
    ] {
        let mut window = ClockWindow::default();
        feed(&mut window, &polled(1, 0, 100, ppm, 5_000));
        assert!(window.drift_evaluable(), "{ppm}");
        assert_eq!(window.drift_estimate_exceeded(), exceeded, "{ppm} ppm");
        // Negative drift of the same size is judged the same.
        assert!(!window.drift_inconsistent(), "{ppm}");
    }
}

#[test]
fn the_estimate_is_not_exceeded_before_it_is_evaluable_and_exceeded_once_it_is() {
    let mut window = ClockWindow::default();
    let mut first_evaluable = None;
    for (i, sample) in polled(1, 0, 100, 400, 5_000).into_iter().enumerate() {
        window.add(sample).unwrap();
        if first_evaluable.is_none() && window.drift_evaluable() {
            first_evaluable = Some(i);
        }
        // While it cannot be judged it is never reported as drift.
        if !window.drift_evaluable() {
            assert!(!window.drift_estimate_exceeded());
        }
    }
    assert_eq!(first_evaluable, Some(500)); // 25 s
    assert!(window.drift_estimate_exceeded());
}

#[test]
fn a_slow_round_trip_does_not_become_the_reference() {
    let mut samples = polled(1, 0, 100, 0, 5_000);
    // The very first exchange of the boot took 150 ms.
    samples[0] = ClockSample::from_exchange(1, 1_000_000, 7_000_000_000, 7_000_150_000).unwrap();
    let mut window = ClockWindow::default();
    feed(&mut window, &samples[..502]);
    // The narrowest exchange (the second one) is the reference: 25 s from it resolve.
    assert!(window.drift_evaluable());
    assert_eq!(window.references[0].1.width(), 5_000);
}

#[test]
fn references_are_per_boot_and_retaken_after_a_reboot() {
    let mut window = ClockWindow::default();
    feed(&mut window, &polled(1, 0, 60, 300, 5_000));
    assert!(window.drift_estimate_exceeded());
    let before = window.samples.back().unwrap().received_us;
    // A different boot starts later on the host with an unrelated device clock.
    let rebooted: Vec<ClockSample> = (0..1200u64)
        .map(|k| {
            let device = 500_000 + k * 50_000;
            let host = before + 1_000_000 + k * 50_000;
            ClockSample::from_exchange(2, device, host - 2_500, host + 2_500).unwrap()
        })
        .collect();
    window.add(rebooted[0]).unwrap();
    // The first exchange of the new boot: old references are gone, no estimate.
    assert_eq!(window.references.len(), 1);
    assert!(!window.drift_evaluable());
    assert!(!window.drift_estimate_exceeded());
    feed(&mut window, &rebooted[1..400]);
    assert!(!window.drift_evaluable()); // 19.95 s of the new boot
    feed(&mut window, &rebooted[400..501]);
    assert!(window.drift_evaluable()); // 25 s: judged on the new boot alone
    assert!(!window.drift_estimate_exceeded());
    assert!(window.references.iter().all(|(_, s)| s.boot == 2));
}

#[test]
fn old_references_are_replaced_and_the_order_checks_still_hold() {
    let mut window = ClockWindow::default();
    // 30 s at 400 ppm, then a clock that stays put for ten minutes.
    feed(&mut window, &polled(1, 0, 30, 400, 5_000));
    let held: Vec<ClockSample> = (0..12_000u64)
        .map(|k| {
            let device = 30_000_000 + 1_000_000 + k * 50_000;
            ClockSample::from_exchange(
                1,
                device,
                8_000_000_000 + k * 50_000,
                8_000_005_000 + k * 50_000,
            )
            .unwrap()
        })
        .collect();
    feed(&mut window, &held);
    // At most four periods are kept, the oldest within two minutes.
    assert_eq!(window.references.len(), 4);
    let newest = window.samples.back().unwrap().device_us;
    assert!(newest - window.references[0].1.device_us <= 4 * REFERENCE_SPAN_US);
    // The 400 ppm of the first minute no longer counts against the clock.
    assert!(window.drift_evaluable());
    assert!(!window.drift_estimate_exceeded());
    // A rejected exchange changes neither samples nor references.
    let references = window.references.len();
    let stale = ClockSample::from_exchange(1, 1_000_000, 1, 2).unwrap();
    assert!(window.add(stale).is_err());
    assert_eq!(window.references.len(), references);
    assert_eq!(window.samples.len(), 8);
}
