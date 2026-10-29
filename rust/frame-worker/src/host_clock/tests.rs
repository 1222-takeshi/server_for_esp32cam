use super::*;
use crate::{
    Frame,
    clock::{ClockSample, ClockWindow},
};

#[test]
fn default_keeps_instant_and_has_no_raw_epoch_or_witness() {
    let clock = HostClock::new(false).unwrap();
    assert_eq!(clock.name(), "std_instant");
    assert_eq!(clock.epoch_ns(), None);
    assert_eq!(clock.observations().unwrap(), None);
    let first = clock.now_us().unwrap();
    assert!(first > 0 && clock.clone().now_us().unwrap() >= first);
}

#[test]
fn raw_elapsed_rejects_regression_and_preserves_nonzero_microsecond_origin() {
    assert_eq!(elapsed_us_from_ns(1000, 1000), Ok(1));
    assert_eq!(elapsed_us_from_ns(1000, 1999), Ok(1));
    assert_eq!(elapsed_us_from_ns(1000, 2000), Ok(2));
    assert!(elapsed_us_from_ns(1000, 999).is_err());
    assert_eq!(elapsed_us_from_ns(0, u64::MAX), Ok(u64::MAX / 1000 + 1));
    for (seconds, nanos) in [(-1, 0), (0, -1), (0, 1_000_000_000), (i64::MAX, 1), (0, 0)] {
        assert!(ns_parts(seconds, nanos).is_err());
    }
    assert_eq!(ns_parts(1, 999_999_999), Ok(1_999_999_999));
}

#[test]
fn ntp_rate_change_can_invalidate_monotonic_proof_without_changing_200ppm_gate() {
    const EPOCH: u64 = 100_000_000;
    let now = 9_102_000;
    let frame = Frame {
        boot: 1,
        sequence: 1,
        captured_us: 9_100_000,
        sent_us: 9_101_000,
        generation: Some(1),
        jpeg: vec![0xff],
    };
    let mut raw = ClockWindow::default();
    let mut adjusted = ClockWindow::default();
    // A synthetic 2000ppm host adjustment; device and RAW retain the same rate.
    let read = |us: u64, adjust: bool| {
        elapsed_us_from_ns(EPOCH, EPOCH + us * 1000 + if adjust { us * 2 } else { 0 }).unwrap()
    };
    for device in [1_000_000, 9_000_000] {
        raw.add(
            ClockSample::from_exchange(
                1,
                device,
                read(device - 1000, false),
                read(device + 1000, false),
            )
            .unwrap(),
        )
        .unwrap();
        adjusted
            .add(
                ClockSample::from_exchange(
                    1,
                    device,
                    read(device - 1000, true),
                    read(device + 1000, true),
                )
                .unwrap(),
            )
            .unwrap();
    }
    let bounds = raw.age_bounds(&frame, read(now, false)).unwrap();
    assert!(bounds.0 <= 2000 && bounds.1 >= 2000 && bounds.1 <= 500_000);
    assert!(adjusted.age_bounds(&frame, read(now, true)).is_none());
}

#[cfg(target_os = "linux")]
#[test]
fn linux_raw_source_epoch_clone_and_witness_share_one_domain() {
    let clock = HostClock::new(true).unwrap();
    assert_eq!(clock.name(), "linux_monotonic_raw");
    let epoch = clock.epoch_ns().unwrap();
    assert_eq!(clock.clone().epoch_ns(), Some(epoch));
    let before = clock.observations().unwrap().unwrap();
    let at = clock.now_us().unwrap();
    let after = clock.observations().unwrap().unwrap();
    assert!(epoch <= before[2] && before[2] <= before[3] && before[3] <= after[2]);
    assert!(epoch + (at - 1) * 1000 <= after[3] && epoch + at * 1000 > before[2]);
    assert!(before[0] <= before[1] && before[1] <= after[0] && after[0] <= after[1]);
}

#[cfg(not(target_os = "linux"))]
#[test]
fn raw_request_on_unsupported_platform_has_no_fallback() {
    assert!(HostClock::new(true).is_err());
}
