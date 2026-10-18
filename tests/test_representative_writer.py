import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from script.detector import Detection
from script.representative_writer import HOUR_BUDGET, MIB, RepresentativeWriter, ResourceGuard

CAT = Detection("cat", .9, .1, .2, .5, .8)
IDENTITY = (2**64 - 1, 1, 1)


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def sample(**changes):
    return {"free": 5000 * MIB, "total": 10_000 * MIB, "db": 0, "wal": 0,
            "write_bytes": 0, **changes}


def ready():
    clock = Clock()
    guard = ResourceGuard(clock=clock)
    guard.observe(sample())
    return clock, guard


def wait_for(predicate):
    end = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < end
        time.sleep(.005)


def test_all_detected_cadence_and_no_detect_never_writes():
    clock, guard = ready()
    saved = []
    writer = RepresentativeWriter(lambda *args: saved.append(args), guard, clock=clock)
    try:
        assert not writer.submit("cam", b"jpeg", [], IDENTITY)
        detections = [CAT]
        assert writer.submit("cam", b"jpeg", detections, IDENTITY)
        detections.clear()
        wait_for(lambda: writer.status()["saved"] == 1)
        assert saved[0][2] == (CAT,)
        for _ in range(50):
            assert not writer.submit("cam", b"jpeg", [CAT], IDENTITY)
        for _ in range(9):
            clock.now += 1
            guard.observe(sample())
        clock.now += .999
        assert not writer.submit("cam", b"jpeg", [CAT], IDENTITY)
        clock.now += .001
        assert writer.submit("cam", b"jpeg", [CAT], IDENTITY)
        wait_for(lambda: writer.status()["saved"] == 2)
        assert writer.status()["interval"] == 51
        assert writer.status()["undetected"] == 1
    finally:
        writer.close()


def test_stall_bounds_count_bytes_and_concurrent_producers():
    clock, guard = ready()
    entered, release = Event(), Event()

    def save(*_args):
        entered.set()
        release.wait(5)

    writer = RepresentativeWriter(save, guard, clock=clock)
    try:
        assert writer.submit("a", b"x" * (128 * 1024), [CAT], IDENTITY)
        assert entered.wait(1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: writer.submit(f"b{i}", b"x" * (128 * 1024),
                                                           [CAT], IDENTITY), range(8)))
        assert sum(results) == 1
        status = writer.status()
        assert status["pending"] == 2
        assert status["pending_bytes"] == 384 * 1024
        assert status["full"] == 7
        started = time.monotonic()
        writer.close()
        assert time.monotonic() - started < 1.5
        assert not writer.submit("z", b"x", [CAT], IDENTITY)
        release.set()
        wait_for(lambda: writer.status()["pending"] == 0)
        assert writer.status()["discarded"] == 1
    finally:
        release.set()
        writer.close()


def test_camera_cap_disk_failure_and_zero_detection_option():
    clock, guard = ready()
    saved = []
    writer = RepresentativeWriter(lambda *args: saved.append(args), guard,
                                  keep_only_detected=False, clock=clock)
    try:
        for camera in ("a" * 128, "b", "c", "d"):
            assert writer.submit(camera, b"x", [], IDENTITY)
            wait_for(lambda: writer.status()["pending"] == 0)
        assert len(saved) == 4
        assert not writer.submit("e", b"x", [], IDENTITY)
        assert writer.status()["cameras"] == 1
        writer.save = lambda *_: (_ for _ in ()).throw(OSError("full"))
        for _ in range(10):
            clock.now += 1
            guard.observe(sample())
        assert writer.submit("a" * 128, b"x", [], IDENTITY)
        wait_for(lambda: writer.status()["errors"] == 1)
        assert guard.status()["reason"] == "monitor_error"
        assert guard.status()["reserved_bytes_hour"] == 5 * (1 + 64 * 1024)
    finally:
        writer.close()


@pytest.mark.parametrize("field,value,reason", [
    ("free", 1024 * MIB, "low_disk"), ("db", 256 * MIB, "db_limit"),
    ("wal", 8 * MIB, "wal_limit"), ("write_bytes", HOUR_BUDGET, "write_limit"),
])
def test_resource_boundaries_manual_hysteresis(field, value, reason):
    clock, guard = ready()
    clock.now += 1
    guard.observe(sample(**{field: value}))
    assert guard.status()["reason"] == reason
    assert not guard.reserve(1)
    assert not guard.resume()
    # Physical writes remain in the rolling window even after the counter is
    # stable; the other faults can recover without a process restart.
    if field == "write_bytes":
        for _ in range(3661):
            clock.now += 1
            guard.observe(sample(write_bytes=value))
    else:
        clock.now += 1
        guard.observe(sample())
    assert not guard.resume()
    for _ in range(60):
        clock.now += 1
        guard.observe(sample(write_bytes=value if field == "write_bytes" else 0))
    assert guard.status()["resume_ready"]
    assert guard.status()["paused"]  # No automatic restart after a fault.
    assert guard.resume()
    assert guard.reserve(1)


def test_stale_failed_monitor_recovery_band_and_invalid_samples():
    clock, guard = ready()
    clock.now += 3.001
    assert not guard.reserve(1)
    assert guard.status()["reason"] == "monitor_stale"
    guard.observe(sample(free=1400 * MIB))
    for _ in range(61):
        clock.now += 1
        guard.observe(sample(free=1400 * MIB))
    assert not guard.resume()
    for invalid in ({}, sample(total=0), sample(free=True), sample(write_bytes=-1)):
        guard.observe(invalid)
        assert guard.status()["reason"] == "monitor_error"
    guard.observe(sample(write_bytes=10))
    guard.observe(sample(write_bytes=9))
    assert guard.status()["reason"] == "monitor_error"
    guard.monitor_failed()
    assert guard.status()["paused"]


def test_monitor_gap_and_fault_discard_queued_images():
    clock, guard = ready()
    entered, release = Event(), Event()
    saved = []

    def save(camera, *_args):
        entered.set()
        release.wait(3)
        saved.append(camera)

    writer = RepresentativeWriter(save, guard, clock=clock)
    try:
        assert writer.submit("a", b"x", [CAT], IDENTITY)
        assert entered.wait(1)
        assert writer.submit("b", b"x", [CAT], IDENTITY)
        clock.now += 4
        guard.observe(sample())  # A healthy sample cannot hide a monitor gap.
        assert guard.status()["paused"]
        assert not guard.resume()
        release.set()
        wait_for(lambda: writer.status()["pending"] == 0)
        assert saved == ["a"]  # Only the already-running write may finish.
        assert writer.status()["discarded"] == 1
        assert not writer.submit("c", b"x", [CAT], IDENTITY)
        assert writer.status()["resource"] == 1
    finally:
        release.set()
        writer.close()


def test_close_serializes_with_admitted_submit(monkeypatch):
    clock, guard = ready()
    entered, release = Event(), Event()
    original = guard.reserve

    def blocked_reserve(size):
        entered.set()
        assert release.wait(3)
        return original(size)

    monkeypatch.setattr(guard, "reserve", blocked_reserve)
    writer = RepresentativeWriter(lambda *_: None, guard, clock=clock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            producer = pool.submit(writer.submit, "cam", b"x", [CAT], IDENTITY)
            assert entered.wait(1)
            closer = pool.submit(writer.close)
            # close must wait for the submit critical section; stopping an
            # empty consumer here would orphan the subsequently admitted item.
            time.sleep(.02)
            assert not writer.stop.is_set()
            release.set()
            assert producer.result(timeout=2)
            closer.result(timeout=2)
        assert writer.status()["pending"] == 0
    finally:
        release.set()
        writer.close()


def test_reserved_window_budget_and_validation():
    clock, guard = ready()
    size = 192 * 1024
    for _ in range(HOUR_BUDGET // size):
        assert guard.reserve(size)
    remainder = HOUR_BUDGET % size
    assert guard.reserve(remainder)
    assert not guard.reserve(1)
    assert guard.status()["reserved_bytes_hour"] == HOUR_BUDGET
    for invalid in (0, -1, True, 192 * 1024 + 1):
        with pytest.raises(ValueError):
            guard.reserve(invalid)


@pytest.mark.parametrize("camera,jpeg,detections,context", [
    ("../x", b"a", [CAT], IDENTITY), ("cam", b"", [CAT], IDENTITY),
    ("cam", b"x" * (128 * 1024 + 1), [CAT], IDENTITY),
    ("cam", b"x", [CAT] * 301, IDENTITY), ("cam", b"x", [object()], IDENTITY),
    ("cam", b"x", [Detection("猫" * 64, .9, 0, 0, 1, 1)] * 300, IDENTITY),
    ("cam", b"x", [Detection("cat", float("nan"), 0, 0, 1, 1)], IDENTITY),
    ("cam", b"x", [Detection("cat", 10**1000, 0, 0, 1, 1)], IDENTITY),
    ("cam", b"x", [Detection("\ud800", .9, 0, 0, 1, 1)], IDENTITY),
    ("cam", b"x", [CAT], (1, True, 1)),
])
def test_invalid_record_does_not_reserve_or_save(camera, jpeg, detections, context):
    clock, guard = ready()
    writer = RepresentativeWriter(lambda *_: pytest.fail("must not save"), guard, clock=clock)
    try:
        assert not writer.submit(camera, jpeg, detections, context)
        assert writer.status()["invalid"] == 1
        assert guard.status()["reserved_bytes_hour"] == 0
    finally:
        writer.close()
