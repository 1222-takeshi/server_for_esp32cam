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
