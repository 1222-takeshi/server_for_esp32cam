from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from script.operating_mode import MAX_VERSION, ModeError, OperatingMode


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def connected():
    clock = Clock()
    mode = OperatingMode(clock=clock)
    mode.hello(7, 0)
    return clock, mode


def ack(mode, applied=100):
    assert mode.acknowledge(7, mode.version, mode.requested, applied)


def frame(mode, sequence, **changes):
    return SimpleNamespace(boot_id=7, sequence=sequence, captured_us=100 + sequence * 190_000,
                           mode_generation=mode.version, **changes)


def emit(clock, mode, sequence, interval, sink=lambda: None):
    clock.now += interval
    assert mode.publish_result(frame(mode, sequence), sink)
    mode.tick()


def test_startup_requires_ack_and_multiple_eco_intervals():
    clock, mode = connected()
    assert mode.status()["requested_mode"] == "eco"
    assert mode.status()["effective_mode"] is None
    assert not mode.publish_result(frame(mode, 1), lambda: pytest.fail("unconfirmed"))
    command = mode.command()
    assert command["mode"] == "eco"
    assert mode.command() is None
    ack(mode)
    for sequence in range(1, 4):
        emit(clock, mode, sequence, 5)
        status = mode.status()
        assert status["state"] == ("active" if sequence == 3 else "pending")
        assert status["actuation_state"] == "hold"
    assert mode.command() is None
    ack(mode)  # Retry ACK keeps the evidence.
    assert mode.status()["cadence_samples"] == 3


def test_manual_tracking_fresh_generation_and_return_to_eco():
    clock, mode = connected()
    old = frame(mode, 1)
    before = mode.version
    result = mode.request("tracking", before, ready=True)
    assert result["version"] == before + 1 and result["state"] == "pending"
    assert mode.request("tracking", mode.version, ready=True)["version"] == mode.version
    ack(mode)
    assert not mode.publish_result(old, lambda: pytest.fail("old generation"))
    for sequence in range(1, 57):
        emit(clock, mode, sequence, .19)
    status = mode.status()
    assert status["state"] == "active"
    assert status["recognition_hz"] >= 5  # Completed 10s count, not inter-arrival extrapolation.
    assert status["actuation_state"] == "observe_only"
    old_tracking = frame(mode, 57)
    mode.request("eco", mode.version, ready=True)
    assert not mode.allows(old_tracking)
    assert not mode.publish_result(old_tracking, lambda: pytest.fail("old result"))
    ack(mode)
    for sequence in range(58, 61):
        emit(clock, mode, sequence, 5)
    assert mode.status()["state"] == "active"
    assert mode.status()["requested_mode"] == "eco"
    assert mode.status()["actuation_state"] == "hold"


@pytest.mark.parametrize("field,value", [("boot_id", 8), ("mode_generation", 1), ("captured_us", 100)])
def test_old_boot_generation_and_capture_rejected(field, value):
    clock, mode = connected()
    ack(mode)
    candidate = frame(mode, 1)
    setattr(candidate, field, value)
    assert not mode.allows(candidate)
    assert not mode.publish_result(candidate, lambda: pytest.fail("invalid"))
    assert mode.status()["rejected_result"] == 1


def test_invalid_ack_cas_modes_and_readiness():
    clock, mode = connected()
    for args in ((8, mode.version, "eco", 100), (True, mode.version, "eco", 100),
                 (7, mode.version - 1, "eco", 100), (7, mode.version, "tracking", 100),
                 (7, mode.version, "eco", 0), (7, mode.version, "eco", True)):
        assert not mode.acknowledge(*args)
    ack(mode)
    assert not mode.acknowledge(7, mode.version, "eco", 101)
    for value in ("automatic", "", None, True, []):
        with pytest.raises(ModeError) as exc:
            mode.request(value, mode.version, ready=True)
        assert exc.value.status == 400
    with pytest.raises(ModeError) as exc:
        mode.request("tracking", mode.version - 1, ready=True)
    assert exc.value.status == 409
    with pytest.raises(ModeError):
        mode.request("tracking", mode.version, ready=False)
    mode.disconnect()
    with pytest.raises(ModeError):
        mode.request("tracking", mode.version, ready=True)


def test_duplicate_and_reordered_results_do_not_count():
    clock, mode = connected()
    ack(mode)
    emit(clock, mode, 2, 5)
    for sequence in (2, 1):
        assert not mode.publish_result(frame(mode, sequence), lambda: pytest.fail("old frame"))
    candidate = frame(mode, 3)
    candidate.captured_us = frame(mode, 2).captured_us
    assert not mode.publish_result(candidate, lambda: pytest.fail("old capture"))
    assert mode.status()["cadence_samples"] == 1


def test_generation_floor_disconnect_and_restart_force_eco():
    clock, mode = connected()
    mode.request("tracking", mode.version, ready=True)
    previous = mode.version
    mode.disconnect()
    assert mode.status()["state"] == "unknown"
    assert mode.status()["requested_mode"] == "eco"
    assert mode.status()["actuation_state"] == "hold"
    mode.hello(7, previous + 100)
    assert mode.version == previous + 101
    assert mode.command()["mode"] == "eco"
    restarted = OperatingMode(clock=clock)
    restarted.hello(7, mode.version)
    assert restarted.version > mode.version
    assert restarted.status()["requested_mode"] == "eco"
    assert not restarted.acknowledge(7, mode.version, "eco", 100)
    with pytest.raises(ModeError):
        restarted.hello(7, 0)


def test_ack_timeout_retries_and_fault_never_auto_promotes():
    clock, mode = connected()
    mode.request("tracking", mode.version, ready=True)
    first = mode.command()
    clock.now += 1
    assert mode.command() == first
    clock.now += 4
    protected = mode.status()
    assert protected["reason"] == "ack_timeout"
    assert protected["requested_mode"] == "eco"
    assert protected["actuation_state"] == "hold"
    assert mode.command()["mode"] == "eco"
    ack(mode)
    for sequence in range(1, 4):
        emit(clock, mode, sequence, 5)
    assert mode.status()["state"] == "degraded"
    mode.request("eco", mode.version, ready=True)
    assert mode.status()["state"] == "pending"


@pytest.mark.parametrize("elapsed,accepted", [(4.999, True), (5, False), (5.001, False)])
def test_ack_checks_deadline_without_watchdog_tick(elapsed, accepted):
    clock, mode = connected()
    mode.request("tracking", mode.version, ready=True)
    version = mode.version
    clock.now += elapsed
    assert mode.acknowledge(7, version, "tracking", 100) is accepted
    if accepted:
        # First-frame/cadence deadlines start at the accepted ACK, not request.
        emit(clock, mode, 1, .19)
        assert mode.status()["state"] == "pending"
    else:
        assert mode.status()["requested_mode"] == "eco"
        assert mode.status()["reason"] == "ack_timeout"
        assert not mode.allows(frame(mode, 1))


def test_failed_publication_cannot_count_toward_active():
    clock, mode = connected()
    ack(mode)
    emit(clock, mode, 1, 5)
    emit(clock, mode, 2, 5)
    clock.now += 5

    def fail():
        raise RuntimeError("sink unavailable")

    with pytest.raises(RuntimeError):
        mode.publish_result(frame(mode, 3), fail)
    status = mode.status()
    assert status["cadence_samples"] == 2
    assert status["state"] == "degraded"
    assert status["actuation_state"] == "hold"
    assert status["reason"] == "publication_failed"


def test_freshness_and_sustained_low_rate_protect_immediately():
    clock, mode = connected()
    mode.request("tracking", mode.version, ready=True)
    ack(mode)
    for sequence in range(1, 17):
        emit(clock, mode, sequence, .19)
    clock.now += .501
    assert mode.status()["requested_mode"] == "eco"
    assert mode.status()["reason"] == "frames_expired"
    assert mode.status()["actuation_state"] == "hold"
    mode.request("tracking", mode.version, ready=True)
    ack(mode)
    for sequence in range(17, 70):
        if mode.requested == "eco":
            break
        clock.now += .25
        mode.publish_result(frame(mode, sequence), lambda: None)
        mode.tick()
    assert mode.status()["reason"] == "cadence_low"
    assert mode.status()["requested_mode"] == "eco"


def test_new_result_cannot_hide_expired_gap_without_watchdog_tick():
    clock, mode = connected()
    mode.request("tracking", mode.version, ready=True)
    ack(mode)
    for sequence in range(1, 106):
        emit(clock, mode, sequence, .1)
    assert mode.status()["state"] == "active"
    old = frame(mode, 106)
    clock.now += .501
    assert not mode.publish_result(old, lambda: pytest.fail("expired result"))
    assert mode.status()["requested_mode"] == "eco"
    assert mode.status()["reason"] == "frames_expired"
    assert mode.status()["actuation_state"] == "hold"


def test_publication_and_mode_change_are_serialized():
    clock, mode = connected()
    ack(mode)
    entered, release = Event(), Event()

    def publish():
        entered.set()
        assert release.wait(3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(mode.publish_result, frame(mode, 1), publish)
        assert entered.wait(1)
        change = pool.submit(mode.request, "tracking", mode.version, ready=True)
        assert not change.done()
        release.set()
        assert result.result(timeout=2)
        assert change.result(timeout=2)["state"] == "pending"
    assert mode.status()["cadence_samples"] == 0


def test_version_exhaustion_holds_and_rejects_replay():
    clock = Clock()
    mode = OperatingMode(clock=clock)
    with pytest.raises(ModeError) as exc:
        mode.hello(7, MAX_VERSION)
    assert exc.value.status == 503
    assert mode.status()["state"] == "degraded"
    assert mode.status()["actuation_state"] == "hold"
    assert not mode.allows(frame(mode, 1))
    mode.disconnect()  # Cleanup must remain safe even at the version ceiling.
    assert mode.status()["actuation_state"] == "hold"


def test_single_bounded_jitter_gap_preserves_ten_second_rate_acceptance():
    clock, mode = connected()
    mode.request("tracking", mode.version, ready=True)
    ack(mode)
    timestamps = []
    for sequence in range(1, 165):
        interval = .29 if sequence == 15 else .195
        emit(clock, mode, sequence, interval)
        timestamps.append(clock.now - 100)
        if sequence == 26:
            assert mode.status()["state"] == "pending"  # No premature active before full evidence.
        assert mode.status()["requested_mode"] == "tracking"
    # The former 3s watchdog falsely protected at 5.165s despite these valid bins.
    assert [sum(start <= stamp < start + 10 for stamp in timestamps) for start in (0, 10, 20)] == [50, 52, 51]
    assert max(b-a for a,b in zip(timestamps, timestamps[1:], strict=False)) == pytest.approx(.29)
    assert mode.status()["state"] == "active"


def test_tracking_requires_full_window_and_protects_sustained_four_hz():
    clock, mode = connected()
    mode.request("tracking", mode.version, ready=True)
    ack(mode)
    for sequence in range(1, 42):
        clock.now += .25
        mode.publish_result(frame(mode, sequence), lambda: None)
        if sequence < 41:
            assert mode.status()["state"] == "pending"
    assert mode.status()["reason"] == "cadence_low"
    assert mode.status()["requested_mode"] == "eco"
    assert mode.status()["actuation_state"] == "hold"


def test_exact_five_hz_window_boundaries_and_watchdog_before_new_result():
    clock, mode = connected()
    mode.request("tracking", mode.version, ready=True)
    ack(mode)
    for index in range(151):
        clock.now = 100.25 + index * .2
        mode.tick()  # Independent watchdog can run before the boundary result.
        assert mode.publish_result(frame(mode, index + 1), lambda: None)
        status = mode.status()
        assert status["requested_mode"] == "tracking"
        assert status["state"] == ("pending" if index < 50 else "active")
        if index >= 50:
            assert status["recognition_hz"] == 5
    mode.request("eco", mode.version, ready=True)
    mode.request("tracking", mode.version, ready=True)
    ack(mode)
    emit(clock, mode, 152, .2)
    assert mode.status()["state"] == "pending"  # A generation cannot inherit old buckets.


@pytest.mark.parametrize('gap', [10, 15, 17])
def test_eco_missing_results_warn_without_latching(gap):
    clock, mode = connected()
    ack(mode)
    for sequence in range(1, 4):
        emit(clock, mode, sequence, 5)
    clock.now += gap
    status = mode.status()
    assert status['state'] == 'pending' and status['eco_warning']
    assert status['reason'] is None and not status['eco_auto_recovery_pending']
    emit(clock, mode, 4, 0)
    emit(clock, mode, 5, 5)
    emit(clock, mode, 6, 5)
    assert mode.status()['state'] == 'active'
    assert mode.status()['actuation_state'] == 'hold'


def test_eco_initial_ack_gap_boundaries_and_resource_gated_recovery():
    clock, mode = connected()
    ack(mode)
    clock.now += 7
    assert not mode.status()['eco_warning']
    clock.now += .001
    assert mode.status()['eco_warning']
    clock.now = mode.ack_at + 17
    assert not mode.status()['eco_auto_recovery_pending']
    clock.now += .001
    assert mode.status()['eco_auto_recovery_pending']
    assert mode.status()['state'] == 'degraded'
    for sequence in range(1, 4):
        emit(clock, mode, sequence, 5)
    assert mode.status()['state'] == 'degraded'  # Health is not yet confirmed.
    mode.eco_recovery_ready = True
    mode.tick()
    assert mode.status()['state'] == 'active'
    assert mode.status()['automatic_recoveries'] == 1


def test_eco_recovery_rejects_replays_and_burst_results():
    clock, mode = connected()
    ack(mode)
    mode.eco_recovery_ready = True
    clock.now += 18
    assert mode.status()['eco_auto_recovery_pending']
    for sequence in range(1, 4):
        emit(clock, mode, sequence, .1)
    assert mode.status()['state'] == 'degraded'
    assert not mode.publish_result(frame(mode, 3), lambda: pytest.fail('duplicate'))
    old = frame(mode, 4)
    old.mode_generation -= 1
    assert not mode.publish_result(old, lambda: pytest.fail('old generation'))
    for sequence in range(4, 7):
        emit(clock, mode, sequence, 5)
    assert mode.status()['state'] == 'active'
    assert mode.status()['automatic_recoveries'] == 1


@pytest.mark.parametrize('reason', ['resource_low_disk', 'resource_thermal_fault',
                                   'resource_monitor_stale', 'publication_failed'])
def test_eco_cadence_cannot_overwrite_or_clear_manual_fault(reason):
    clock, mode = connected()
    ack(mode)
    mode.eco_recovery_ready = True
    mode.protect(reason)
    clock.now += 18
    assert mode.status()['reason'] == reason
    for sequence in range(1, 4):
        emit(clock, mode, sequence, 5)
    assert mode.status()['reason'] == reason
    assert not mode.status()['eco_auto_recovery_pending']
    assert mode.status()['automatic_recoveries'] == 0


def test_tracking_fault_cannot_become_automatic_eco_recovery():
    clock, mode = connected()
    mode.request('tracking', mode.version, ready=True)
    ack(mode)
    mode.eco_recovery_ready = True
    emit(clock, mode, 1, .19)
    clock.now += .501
    assert mode.status()['requested_mode'] == 'eco'
    ack(mode)
    for sequence in range(2, 5):
        emit(clock, mode, sequence, 5)
    assert mode.status()['state'] == 'degraded'
    assert not mode.status()['eco_auto_recovery_pending']
