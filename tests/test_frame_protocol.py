from __future__ import annotations

import struct

import pytest

from script.frame_protocol import ClockSample, FrameOrder, decode_frame


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
