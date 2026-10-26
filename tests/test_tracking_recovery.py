"""Transient tracking holds retain recognition, never reuse old generations."""

from types import SimpleNamespace
from threading import Event, Thread

from conftest import MINIMAL_JPEG

import pytest

from script.operating_mode import MAX_VERSION, ModeError, OperatingMode
from script.operating_runtime import OperatingRuntime


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def tracking():
    clock = Clock()
    mode = OperatingMode(clock=clock, auto_recover_tracking=True)
    mode.hello(7, 0)
    mode.eco_recovery_ready = True
    mode.request("tracking", mode.version, ready=True)
    acknowledge(mode)
    return clock, mode


def acknowledge(mode):
    assert mode.acknowledge(7, mode.version, mode.requested, 100)


def emit(clock, mode, seq, step=0.19):
    clock.now += step
    frame = SimpleNamespace(
        boot_id=7,
        sequence=seq,
        captured_us=int(clock.now * 1_000_000),
        mode_generation=mode.version,
    )
    assert mode.publish_result(frame, lambda: None)
    return frame


def stable(clock, mode, first=1):
    for seq in range(first, first + 54):
        emit(clock, mode, seq)
    return mode.status()


def test_transient_hold_requires_new_ack_and_full_new_cadence():
    clock, mode = tracking()
    assert stable(clock, mode)["actuation_state"] == "observe_only"
    old_version = mode.version
    old = SimpleNamespace(
        boot_id=7, sequence=99, captured_us=int(clock.now * 1_000_000), mode_generation=old_version
    )
    clock.now += 0.500001
    held = mode.status()
    assert held["tracking_auto_recovery_pending"] and held["actuation_state"] == "hold"
    assert held["requested_mode"] == "tracking" and mode.version == old_version + 1
    assert mode.command()["mode"] == "tracking"
    assert not mode.acknowledge(7, old_version, "tracking", 100)
    assert not mode.publish_result(old, lambda: pytest.fail("old generation"))
    acknowledge(mode)
    for seq in range(100, 152):
        emit(clock, mode, seq)
        assert mode.status()["actuation_state"] == "hold"
    emit(clock, mode, 152)
    emit(clock, mode, 153)
    result = mode.status()
    assert result["state"] == "active" and not result["tracking_auto_recovery_pending"]
    assert result["tracking_hold_events"] == result["tracking_automatic_recoveries"] == 1
    assert result["tracking_hold_seconds"] == pytest.approx(0.19 * 54)


def test_repeated_gap_restarts_proof_without_generation_or_ack_churn():
    clock, mode = tracking()
    mode.protect("cadence_low")
    acknowledge(mode)
    version = mode.version
    for seq in range(1, 11):
        emit(clock, mode, seq)
    clock.now += 0.500001
    for _ in range(10):
        assert mode.status()["actuation_state"] == "hold"
    assert mode.version == version and mode.ack == 100 and mode.command() is None
    assert mode.status()["cadence_samples"] == 0
    result = stable(clock, mode, 11)
    assert result["tracking_automatic_recoveries"] == 1
    assert result["tracking_hold_seconds"] == pytest.approx(1.9 + 0.500001 + 10.26)


@pytest.mark.parametrize(
    "reason",
    [
        "ack_timeout",
        "inference_failed",
        "publication_failed",
        "resource_thermal_fault",
        "disk_low",
        "manual_pause",
    ],
)
def test_nontransient_faults_still_require_manual_recovery(reason):
    clock, mode = tracking()
    mode.protect("frames_expired")
    mode.protect(reason)
    assert mode.requested == "eco" and not mode.status()["tracking_auto_recovery_pending"]
    acknowledge(mode)
    for seq in range(1, 4):
        emit(clock, mode, seq, 5)
    assert mode.status()["reason"] == reason and mode.status()["actuation_state"] == "hold"


def test_no_resource_permission_no_auto_recovery_and_manual_eco_wins():
    clock, mode = tracking()
    mode.protect("frames_expired")
    acknowledge(mode)
    mode.eco_recovery_ready = False
    assert stable(clock, mode)["actuation_state"] == "hold"
    mode.request("eco", mode.version, ready=True)
    assert not mode.status()["tracking_auto_recovery_pending"] and mode.requested == "eco"
    mode.eco_recovery_ready = True
    assert mode.status()["actuation_state"] == "hold"


def test_disconnect_and_version_exhaustion_cannot_auto_track():
    _, mode = tracking()
    mode.protect("frames_expired")
    mode.disconnect()
    mode.hello(8, mode.version)
    assert mode.requested == "eco" and not mode.status()["tracking_auto_recovery_pending"]
    _, mode = tracking()
    mode.version = MAX_VERSION
    with pytest.raises(ModeError):
        mode.protect("frames_expired")
    assert not mode.connected and not mode.status()["tracking_auto_recovery_pending"]
    assert mode.status()["actuation_state"] == "hold"


@pytest.mark.parametrize("value", [None, 1, "true", []])
def test_tracking_recovery_requires_an_explicit_boolean(value):
    with pytest.raises(ValueError):
        OperatingMode(auto_recover_tracking=value)


def runtime_emit(clock, rt, seq):
    clock.now += 0.19
    stamp = int(clock.now * 1_000_000)
    frame = SimpleNamespace(
        boot_id=7,
        sequence=seq,
        captured_us=stamp - 2_000,
        mode_generation=rt.mode.version,
        jpeg=MINIMAL_JPEG,
    )
    event = {"host_received_us": stamp, "age_low_us": 1_000, "age_high_us": 3_000, "detections": []}
    assert rt.mode.publish_result(frame, lambda: rt._result(frame, event))


@pytest.fixture
def recovery_runtime(app, tmp_path):
    app.config.update(OPERATING_TRACKING_AUTO_RECOVERY=True, KEEP_ONLY_DETECTED_SNAPSHOTS=True)
    clock = Clock()

    def sample(*_):
        return {"free": 5 * 1024**3, "total": 10 * 1024**3, "db": 0, "wal": 0, "write_bytes": 0}

    rt = OperatingRuntime(
        app,
        object(),
        "cam",
        "private",
        tmp_path,
        clock=clock,
        sampler=sample,
        hardware=lambda: (45, 0),
    )
    try:
        rt.maintain()
        rt.sample()
        rt.mode.hello(7, 0)
        yield clock, rt
    finally:
        rt.close()


def test_runtime_keeps_transient_recognition_alive_but_resource_fault_wins(recovery_runtime):
    clock, rt = recovery_runtime
    rt.mode.request("tracking", rt.mode.version, ready=True)
    acknowledge(rt.mode)
    rt.mode.protect("frames_expired")
    rt.sample()
    assert not rt.guard.status()["paused"]
    assert rt.mode.status()["tracking_auto_recovery_pending"]
    acknowledge(rt.mode)
    for seq in range(1, 55):
        runtime_emit(clock, rt, seq)
        rt.sample()
    assert rt.mode.status()["actuation_state"] == "observe_only"
    clock.now += 0.496  # Capture age 499ms; publication gap alone is shorter.
    assert rt.status()["actuation_state"] == "observe_only"
    clock.now += 0.002  # Capture age 501ms, while publication gap is <500ms.
    assert rt.status()["actuation_state"] == "hold"
    assert rt.mode.status()["tracking_auto_recovery_pending"]
    rt.guard.pause("thermal_fault")
    rt._protect()
    assert rt.mode.requested == "eco" and rt.mode.status()["actuation_state"] == "hold"
    assert not rt.mode.status()["tracking_auto_recovery_pending"]


def test_async_writer_pause_cannot_recover_from_cached_readiness(recovery_runtime):
    clock, rt = recovery_runtime
    rt.mode.request("tracking", rt.mode.version, ready=True)
    acknowledge(rt.mode)
    rt.mode.protect("frames_expired")
    acknowledge(rt.mode)
    for seq in range(1, 53):
        runtime_emit(clock, rt, seq)
        rt.sample()
    assert rt.mode.eco_recovery_ready  # Cached before the writer fails.
    rt.guard.monitor_failed()  # Same asynchronous path as RepresentativeWriter._run.
    runtime_emit(clock, rt, 53)
    runtime_emit(clock, rt, 54)
    status = rt.mode.status()  # No intervening runtime health_check allowed.
    assert status["actuation_state"] == "hold"
    assert status["tracking_automatic_recoveries"] == 0
    rt.sample()
    assert rt.mode.requested == "eco" and rt.guard.status()["paused"]


def test_freshness_is_rechecked_after_waiting_for_mode_lock():
    clock, mode = tracking()
    mode.protect("frames_expired")
    acknowledge(mode)
    started, result, published = Event(), [], []
    received_at = clock.now
    frame = SimpleNamespace(
        boot_id=7, sequence=1, captured_us=int(clock.now * 1_000_000), mode_generation=mode.version
    )

    def owner():
        started.set()
        result.append(
            mode.publish_result(
                frame,
                lambda: published.append(True),
                eligible=lambda: clock.now - received_at <= 0.5,
            )
        )

    with mode.lock:
        worker = Thread(target=owner)
        worker.start()
        assert started.wait(1)
        clock.now += 0.500001
    worker.join(2)
    assert not worker.is_alive() and result == [False] and not published
    assert mode.status()["actuation_state"] == "hold"


def test_resource_fault_wins_over_stale_capture_at_last_generation(recovery_runtime):
    clock, rt = recovery_runtime
    rt.mode.request("tracking", rt.mode.version, ready=True)
    rt.mode.version = MAX_VERSION - 1
    acknowledge(rt.mode)
    runtime_emit(clock, rt, 1)
    clock.now += 0.501
    rt.guard.pause("thermal_fault")
    rt._protect()
    status = rt.mode.status()
    assert status["version"] == MAX_VERSION and status["connected"]
    assert status["requested_mode"] == "eco" and status["reason"] == "resource_thermal_fault"
    assert status["tracking_hold_events"] == 0 and not status["tracking_auto_recovery_pending"]
    assert status["actuation_state"] == "hold"


def test_terminal_version_exhaustion_remains_readable_with_old_ram_frame(recovery_runtime):
    clock, rt = recovery_runtime
    rt.mode.request("tracking", rt.mode.version, ready=True)
    rt.mode.version = MAX_VERSION
    acknowledge(rt.mode)
    runtime_emit(clock, rt, 1)
    with pytest.raises(ModeError):
        rt.mode.protect("frames_expired")
    clock.now += 0.501
    status = rt.status()
    assert status["reason"] == "version_exhausted" and not status["connected"]
    assert status["actuation_state"] == "hold" and not status["tracking_auto_recovery_pending"]
