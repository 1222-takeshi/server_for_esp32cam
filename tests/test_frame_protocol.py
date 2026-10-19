from __future__ import annotations

import struct

import pytest

from script.frame_protocol import ClockSample, ClockWindow, FrameOrder, decode_frame


def packet(*, boot=11, seq=1, captured=1000, sent=1100, jpeg=b"jpeg"):
    return struct.pack(">4sQQQQI", b"ECF1", boot, seq, captured, sent, len(jpeg)) + jpeg


def test_frame_matches_network_endian_firmware_header_and_payload():
    payload = packet(boot=0x0102030405060708, seq=2**64 - 1)
    frame = decode_frame(payload)
    assert payload[:12].hex() == "454346310102030405060708"
    assert (frame.boot_id, frame.sequence, frame.captured_us, frame.sent_us, frame.jpeg) == (
        0x0102030405060708, 2**64 - 1, 1000, 1100, b"jpeg"
    )
    assert decode_frame(packet(jpeg=b"abcd"), max_jpeg_bytes=4).jpeg == b"abcd"


@pytest.mark.parametrize("payload", [
    b"", b"ECF1", packet()[:-1], packet() + b"x", b"BAD!" + packet()[4:],
    packet(boot=0), packet(seq=0), packet(captured=0), packet(sent=999), packet(jpeg=b""),
])
def test_invalid_frames_fail_without_returning_partial_payload(payload):
    with pytest.raises(ValueError):
        decode_frame(payload)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limits_and_oversize_frames_are_rejected(limit):
    with pytest.raises(ValueError):
        decode_frame(packet(), max_jpeg_bytes=limit)


def test_frame_over_limit_is_rejected():
    with pytest.raises(ValueError, match="size"):
        decode_frame(packet(jpeg=b"abcde"), max_jpeg_bytes=4)


def test_order_counts_gaps_but_rejects_replays_stale_capture_and_retired_boot():
    order = FrameOrder()
    assert order.observe(decode_frame(packet())) == ("new", 0)
    assert order.observe(decode_frame(packet())) == ("duplicate", 0)
    assert order.observe(decode_frame(packet(seq=3, captured=1001))) == ("new", 1)
    assert order.observe(decode_frame(packet(seq=2, captured=1002))) == ("stale", 0)
    assert order.observe(decode_frame(packet(seq=4, captured=1001))) == ("stale", 0)
    assert order.observe(decode_frame(packet(boot=12))) == ("boot_changed", 0)
    assert order.observe(decode_frame(packet(boot=11, seq=5))) == ("retired_boot", 0)
    assert order.observe(decode_frame(packet(boot=12, seq=2, captured=1001))) == ("new", 0)


def test_clock_round_trip_bounds_and_drift_are_conservative():
    # Server time = device time + 10,000us. Ping/pong bounds include that offset.
    clock = ClockSample.from_exchange(11, 1000, 10_900, 11_100)
    frame = decode_frame(packet(captured=1200, sent=1250))
    assert clock.age_bounds_us(frame, 11_400, drift_ppm=0) == (100, 300)
    low, high = clock.age_bounds_us(frame, 11_400, drift_ppm=200)
    assert low <= 100 and high >= 300
    assert clock.age_bounds_us(frame, 20_000_000) is None
    assert clock.age_bounds_us(decode_frame(packet(boot=12)), 11_400) is None


def test_definitely_future_capture_is_not_treated_as_fresh():
    clock = ClockSample.from_exchange(11, 1000, 10_900, 11_100)
    frame = decode_frame(packet(captured=2000, sent=2100))
    assert clock.age_bounds_us(frame, 11_400, drift_ppm=0) is None


def test_clock_drift_includes_capture_before_the_exchange():
    clock = ClockSample.from_exchange(11, 10_001_000, 20_000_000, 20_000_200)
    frame = decode_frame(packet(captured=1000, sent=1100))
    assert clock.age_bounds_us(frame, 20_000_200, drift_ppm=200) == (
        9_998_000, 10_002_200
    )


def test_retired_boot_history_retains_only_the_latest_32():
    order = FrameOrder()
    for boot in range(1, 35):
        order.observe(decode_frame(packet(boot=boot)))
    assert order.observe(decode_frame(packet(boot=2))) == ("retired_boot", 0)
    # Boot 1 is outside the history; connection boot checks remain mandatory.
    assert order.observe(decode_frame(packet(boot=1))) == ("boot_changed", 0)


@pytest.mark.parametrize("args", [(0, 1, 2, 3), (1, 0, 2, 3), (1, 1, 3, 2),
                                  (1, 1, 2, 2_000_003), (True, 1, 2, 3)])
def test_bad_clock_exchanges_fail(args):
    with pytest.raises(ValueError):
        ClockSample.from_exchange(*args)


def test_clock_window_retains_tight_evidence_after_a_delayed_exchange():
    window = ClockWindow()
    tight = ClockSample.from_exchange(11, 1_000_000, 1_009_900, 1_010_100)
    delayed = ClockSample.from_exchange(11, 1_500_000, 1_110_000, 1_510_000)
    window.add(tight)
    window.add(delayed)
    frame = decode_frame(packet(captured=1_600_000, sent=1_625_000))
    low, high = window.age_bounds_us(frame, 1_660_000)
    assert low <= 50_000 <= high < 51_000
    assert delayed.age_bounds_us(frame, 1_660_000)[1] > 400_000
    assert low >= tight.age_bounds_us(frame, 1_660_000)[0]
    assert high <= tight.age_bounds_us(frame, 1_660_000)[1]


def test_clock_window_expires_each_sample_without_renewing_old_evidence():
    window = ClockWindow()
    old = ClockSample.from_exchange(11, 1_000_000, 1_009_900, 1_010_100)
    recent = ClockSample.from_exchange(11, 11_000_000, 10_610_000, 11_010_000)
    window.add(old)
    window.add(recent)
    frame = decode_frame(packet(captured=11_050_000, sent=11_060_000))
    assert window.age_bounds_us(frame, 11_100_000) == recent.age_bounds_us(frame, 11_100_000)
    assert window.age_bounds_us(frame, 22_000_000) is None


def test_clock_window_rejects_contradictions_and_definitely_future_frames():
    window = ClockWindow()
    assert window.age_bounds_us(decode_frame(packet()), 20_000) is None
    window.add(ClockSample.from_exchange(11, 1000, 10_900, 11_100))
    window.add(ClockSample.from_exchange(11, 2000, 21_900, 22_100))
    assert window.age_bounds_us(decode_frame(packet(captured=2100, sent=2200)), 23_000) is None
    future = ClockWindow()
    future.add(ClockSample.from_exchange(11, 1000, 10_900, 11_100))
    assert future.age_bounds_us(decode_frame(packet(captured=2000, sent=2100)), 11_400) is None


def test_clock_window_is_bounded_and_resets_on_boot_change():
    window = ClockWindow()
    for device in range(1000, 1010):
        window.add(ClockSample.from_exchange(11, device, device+9900, device+10_100))
    assert len(window.samples) == 8 and window.samples[0].device_us == 1002
    window.add(ClockSample.from_exchange(12, 1000, 11_900, 12_100))
    assert len(window.samples) == 1
    assert window.age_bounds_us(decode_frame(packet(captured=1200, sent=1250)), 11_400) is None
    new_frame = decode_frame(packet(boot=12, captured=1200, sent=1250))
    assert window.age_bounds_us(new_frame, 12_400) == window.samples[0].age_bounds_us(new_frame, 12_400)


def test_clock_window_intersection_includes_drift_before_and_after_exchange():
    window = ClockWindow()
    window.add(ClockSample.from_exchange(11, 10_001_000, 20_000_000, 20_000_200))
    before = decode_frame(packet(captured=1000, sent=1100))
    assert window.age_bounds_us(before, 20_000_200) == (9_998_000, 10_002_200)
    after = decode_frame(packet(captured=20_001_000, sent=20_001_100))
    assert window.age_bounds_us(after, 30_000_200) == (0, 2200)


def test_clock_window_rejects_reordered_samples_and_excludes_future_observations():
    window = ClockWindow()
    window.add(ClockSample.from_exchange(11, 2000, 11_900, 12_100))
    with pytest.raises(ValueError, match="Out-of-order"):
        window.add(ClockSample.from_exchange(11, 1000, 10_900, 11_100))
    assert len(window.samples) == 1
    assert window.age_bounds_us(decode_frame(packet(captured=1200, sent=1250)), 11_400) is None


@pytest.mark.parametrize("drift_ppm", [-200, 200])
@pytest.mark.parametrize("captured,age", [(9_000_000, 4_000_000), (12_000_000, 60_000)])
def test_clock_window_contains_true_age_at_both_drift_limits(drift_ppm, captured, age):
    def host(device):
        return 1_000_000 + device + device * drift_ppm // 1_000_000

    window = ClockWindow()
    for device, outbound, inbound in ((10_000_000, 100, 100), (11_000_000, 390_000, 10_000)):
        window.add(ClockSample.from_exchange(11, device, host(device)-outbound, host(device)+inbound))
    frame = decode_frame(packet(captured=captured, sent=captured+20_000))
    low, high = window.age_bounds_us(frame, host(captured)+age)
    assert low <= age <= high
