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
