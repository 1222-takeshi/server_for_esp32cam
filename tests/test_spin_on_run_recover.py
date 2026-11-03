import json
import stat

import pytest

from scripts.spin_on_run import common, environment, recover_guard as rg
from tests.test_spin_on_run_guardian import BASE


class FakeOps:
    def __init__(self, **kw):
        self.t = 0.0
        self.calls = []
        self.stopped = kw.get("stopped", True)
        self.ready_after = kw.get("ready_after", 0)
        self.version = kw.get("version", 5)
        self.eco_after_requests = kw.get("eco_after_requests", 1)
        self.requests = 0
        self.bad_samples = kw.get("bad_samples", 0)  # R6 samples that are not restored
        self.fail_attempts = kw.get("fail_attempts", 0)  # first n attempts fail at R2
        self.units = kw.get("units", [])
        self.enabled = kw.get("enabled", "disabled")
        self.snapshots = 0
        self.started = 0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def stop_trial(self):
        self.calls.append("stop")

    def trial_state(self):
        return {
            "active_state": "inactive" if self.stopped else "active",
            "main_pid": 0,
            "cgroup_empty": True,
        }

    def start_service(self):
        self.started += 1
        self.calls.append("start")

    def ready(self):
        if self.fail_attempts and self.started <= self.fail_attempts:
            return False
        return self.t >= self.ready_after

    def mode(self):
        eco = self.requests >= self.eco_after_requests and not (
            self.bad_samples and self.calls.count("sample") < self.bad_samples
        )
        self.calls.append("sample")
        return {
            "state": "active" if eco else "degraded",
            "requested_mode": "eco",
            "effective_mode": "eco" if eco else "tracking",
            "acknowledged": eco,
            "actuation_state": "hold",
            "paused": False,
            "version": self.version,
        }

    def request_eco(self, mode):
        self.requests += 1
        self.calls.append("eco")

    def snapshot(self):
        self.snapshots += 1
        return dict(
            BASE,
            service_active=True,
            units_active=["esp32cam-server.service", *self.units],
            mode={
                "ready": True,
                "state": "active",
                "requested_mode": "eco",
                "effective_mode": "eco",
                "acknowledged": True,
                "actuation_state": "hold",
            },
        )

    def disable_self(self):
        self.calls.append("disable")

    def is_enabled(self):
        return self.enabled


def run_dir(tmp_path, report=None, marker="completed", child=None, epochs=True):
    d = tmp_path / "trial"
    d.mkdir(parents=True)
    if epochs and isinstance(report, dict) and "measurement" not in report:
        # A complete report carries the generations the trial used (below the ACK of 5).
        report = {**report, "measurement": {"generation_epochs": [[1, 1], [2, 3]]}}
    (d / "native.json").write_text(json.dumps(report) if report is not None else "")
    (d / "native.stderr").write_bytes(b"")
    if marker:
        (d / "marker").write_text(marker + "\n")
    (d / "child-end.json").write_text(
        json.dumps(child or {"exit": 0, "signalled": False, "guardian_stopped_child": False})
    )
    return d


def baseline():
    return dict(
        BASE,
        service_active=True,
        units_active=["esp32cam-server.service"],
        mode={
            "ready": True,
            "state": "active",
            "requested_mode": "eco",
            "effective_mode": "eco",
            "acknowledged": True,
            "actuation_state": "hold",
            "version": 3,
        },
    )


def guard(tmp_path, ops=None, **kw):
    d = run_dir(tmp_path, **kw)
    return rg.Guard(ops or FakeOps(), d, baseline(), 600), d


def test_eco_reached_requires_every_condition():
    ok = {
        "state": "active",
        "requested_mode": "eco",
        "effective_mode": "eco",
        "acknowledged": True,
        "actuation_state": "hold",
        "paused": False,
    }
    assert rg.eco_reached(ok)
    for key, value in [
        ("state", "pending"),
        ("acknowledged", False),
        ("actuation_state", "move"),
        ("paused", True),
        ("effective_mode", "tracking"),
        ("requested_mode", "tracking"),
    ]:
        assert not rg.eco_reached({**ok, key: value})


def test_full_restore_succeeds_and_the_guard_disables_itself_last(tmp_path):
    g, d = guard(
        tmp_path,
        report={"reason": "completed", "measurement": {"generation_epochs": [[1, 1], [2, 4]]}},
    )
    ops = g.ops
    out = g.run()
    assert out["ok"] and rg.exit_code(out) == 0
    assert ops.calls[0] == "stop" and ops.calls[-1] == "disable"
    assert ops.calls.count("sample") == 2 + 13  # R3/R4 polls, then thirteen R6 samples
    assert json.loads((d / "restore-result.json").read_text())["r7"]["is_enabled"] == "disabled"
    for name in ("post-run-check.json", "after.attempt1.json", "after.json", "restore-result.json"):
        assert stat.S_IMODE((d / name).stat().st_mode) == 0o400, name
    assert g.post["max_generation"] == 4 and g.post["native_size"] > 0


def test_new_generation_ack_must_exceed_the_generation_used_in_the_trial(tmp_path):
    g, _ = guard(
        tmp_path,
        ops=FakeOps(version=4),
        report={"reason": "completed", "measurement": {"generation_epochs": [[1, 1], [2, 4]]}},
    )
    r = g.r3_r4(g.post_run()["max_generation"])
    assert not r.ok
    g2, _ = guard(tmp_path / "b", ops=FakeOps(version=5))
    assert g2.r3_r4(4).ok
    # Nothing known about the trial's generation is never a pass.
    assert not g2.r3_r4(None).ok


def test_eco_is_requested_until_acknowledged(tmp_path):
    ops = FakeOps(eco_after_requests=3)
    g, _ = guard(tmp_path, ops=ops)
    assert g.r3_r4(3).ok and ops.calls.count("eco") == 3


def test_service_that_never_gets_ready_fails_after_the_deadline(tmp_path):
    ops = FakeOps(ready_after=10_000)
    g, _ = guard(tmp_path, ops=ops)
    assert not g.r2().ok and 45 <= ops.t <= 47


def test_r1_requires_the_trial_to_be_gone(tmp_path):
    g, _ = guard(tmp_path, ops=FakeOps(stopped=False))
    assert not g.r1().ok


def test_a_failed_attempt_is_retried_but_post_run_runs_once(tmp_path):
    ops = FakeOps(fail_attempts=1)
    g, d = guard(tmp_path, ops=ops, report={"reason": "completed"})
    out = g.run()
    assert out["ok"] and len(out["attempts"]) == 2
    assert [a["attempt"] for a in out["attempts"]] == [1, 2]
    assert (d / "after.attempt2.json").exists() and not (d / "after.attempt1.json").exists()
    assert ops.calls.count("disable") == 1 and ops.t >= rg.ATTEMPT_GAP_S + 45


def test_three_failures_still_run_r7_once_and_exit_75(tmp_path):
    ops = FakeOps(fail_attempts=3)
    g, d = guard(tmp_path, ops=ops)
    out = g.run()
    assert not out["ok"] and rg.exit_code(out) == 75
    assert len(out["attempts"]) == 3 and ops.calls.count("disable") == 1
    assert (d / "after.json").exists() and (d / "restore-result.json").exists()
    assert ops.t >= 2 * rg.ATTEMPT_GAP_S


def test_one_bad_observation_sample_fails_the_attempt(tmp_path):
    ops = FakeOps(bad_samples=0)
    g, _ = guard(tmp_path, ops=ops)
    ops.requests = 1
    assert g.r6().ok
    bad = FakeOps(eco_after_requests=1)
    g2, _ = guard(tmp_path / "x", ops=bad)
    bad.requests = 1
    original = bad.mode
    count = {"n": 0}

    def flaky():
        count["n"] += 1
        mode = original()
        return {**mode, "acknowledged": False} if count["n"] == 7 else mode

    bad.mode = flaky
    assert not g2.r6().ok and count["n"] == 7


def test_leftover_unit_other_than_the_guard_itself_fails_r5(tmp_path):
    g, _ = guard(tmp_path, ops=FakeOps(units=[rg.SELF_UNIT]))
    assert g.r5(1).ok
    g2, _ = guard(
        tmp_path / "b", ops=FakeOps(units=["esp32cam-spin-on-run-v1-live-600-20261105.service"])
    )
    assert not g2.r5(1).ok
    g3, _ = guard(tmp_path / "c", ops=FakeOps(units=[rg.SELF_UNIT + "x"]))
    assert not g3.r5(1).ok


def test_r7_failure_is_a_restoration_failure(tmp_path):
    g, _ = guard(tmp_path, ops=FakeOps(enabled="enabled"))
    out = g.run()
    assert not out["ok"] and out["r7"]["ok"] is False


def test_post_run_settles_abort_and_failure_codes(tmp_path):
    g, _ = guard(tmp_path, marker=None, report=None)
    info = g.post_run()
    assert info["abort_codes"] == ["A06"] and info["native_size"] == 0
    g2, _ = guard(
        tmp_path / "online",
        report={"reason": "protection_violation"},
        child={"exit": 20, "signalled": False, "guardian_stopped_child": False},
    )
    assert g2.post_run()["abort_codes"] == ["A14"]
    g3, _ = guard(tmp_path / "bad", report=None)
    info = g3.post_run()
    assert info["abort_codes"] == [] and info["failure_codes"] == ["F01", "F06"]


def test_clock_unresolved_before_the_measurement_is_neither_a07_nor_f06_nor_a_failure(tmp_path):
    report = {
        "reason": "clock_unresolved",
        "measurement": None,
        "measurement_error": "trial_incomplete",
        "online_abort": None,
        "session_final": {"version": 3},
    }
    child = {"exit": 3, "signalled": False, "guardian_stopped_child": False}
    g, d = guard(tmp_path, report=report, child=child)
    info = g.post_run()
    assert info["abort_codes"] == [] and info["failure_codes"] == []
    assert info["max_generation"] == 3  # the restore needs a version above the mode's last
    assert info["not_started"] is True and info["reason"] == "clock_unresolved"
    # The guardian's saved result feeds the final verdict: no milestone, not F, not A.
    out = g.run()
    assert out["ok"]
    collector = environment.Collector(run=lambda argv: "", get_json=lambda n: {}, unit_path="/x")
    collector.snapshot = lambda: baseline()
    final = rg.final_verdict(d, collector, baseline())
    assert final["result"] == "not_started" and final["primary"] == "clock_unresolved"
    assert final["milestone"] is False
    # Any other non-zero exit with the same report is still A07.
    other, _ = guard(tmp_path / "other", report=report, child={**child, "exit": 1})
    assert other.post_run()["abort_codes"] == ["A07"]
    # An ordinary early stop after the same kind of report keeps its handling.
    early, _ = guard(
        tmp_path / "early", report={**report, "reason": "ack_lost", "samples": []}, child=child
    )
    info = early.post_run()
    assert info["not_started"] is False and info["failure_codes"] == ["F01"]


def test_final_verdict_uses_the_restore_result_and_remaining_units(tmp_path):
    g, d = guard(tmp_path, report={"reason": "completed"})
    out = g.run()
    assert out["ok"]
    collector = environment.Collector(run=lambda argv: "", get_json=lambda n: {}, unit_path="/x")
    collector.snapshot = lambda: baseline()
    final = rg.final_verdict(d, collector, baseline())
    assert final["result"] in {"failed", "passed"} and final["final_deviations"] == []
    leftover = dict(baseline(), units_active=["esp32cam-server.service", rg.SELF_UNIT])
    collector.snapshot = lambda: leftover
    other = tmp_path / "again"
    other.mkdir()
    common.write_json(
        other / "restore-result.json",
        {"ok": True, "post": {"abort_codes": [], "failure_codes": []}},
    )
    assert rg.final_verdict(other, collector, baseline())["result"] == "passed_restore_failed"


def test_check_only_reports_problems_without_side_effects(tmp_path):
    ops = FakeOps()
    assert rg.check_only(ops, baseline()) == []
    assert ops.calls == ["sample"] or ops.calls == []
    assert "stop" not in ops.calls and "start" not in ops.calls
    ops.snapshot = lambda: (_ for _ in ()).throw(OSError())
    assert rg.check_only(ops, baseline()) == ["check_error:OSError"]


def test_guard_source_never_touches_firmware_or_ota():
    import inspect
    import re

    source = inspect.getsource(rg).lower()
    assert not re.search(r"\bota\b|3232|flash|esptool", source)
    assert pytest is not None


# -- review fixes: exceptions, restarts, fallbacks and deadlines ----------------


class Flaky(FakeOps):
    """Raises once from the named call (or always, with `always=True`)."""

    def __init__(self, name, *, always=False, error=OSError, **kw):
        super().__init__(**kw)
        self.name, self.always, self.error, self.raised = name, always, error, 0

    def _maybe(self, name):
        if name == self.name and (self.always or self.raised == 0):
            self.raised += 1
            raise self.error(name)

    def stop_trial(self):
        self._maybe("stop_trial")
        super().stop_trial()

    def start_service(self):
        self._maybe("start_service")
        super().start_service()

    def mode(self):
        self._maybe("mode")
        return super().mode()

    def request_eco(self, mode):
        self._maybe("request_eco")
        super().request_eco(mode)

    def snapshot(self):
        self._maybe("snapshot")
        return super().snapshot()

    def disable_self(self):
        self._maybe("disable_self")
        super().disable_self()


@pytest.mark.parametrize("name", ["stop_trial", "start_service", "mode", "request_eco"])
def test_a_transient_failure_in_r1_to_r4_does_not_end_the_restore(tmp_path, name):
    ops = Flaky(name, eco_after_requests=1)
    g, d = guard(tmp_path, ops=ops, report={"reason": "completed"})
    out = g.run()
    assert out["ok"], out
    assert ops.raised == 1 and ops.calls[-1] == "disable"
    assert (d / "restore-result.json").exists() and (d / "after.json").exists()


def test_an_exception_in_r5_or_r6_retries_the_attempt_and_still_closes_with_r7(tmp_path):
    ops = Flaky("snapshot")  # fails the first snapshot (R5 of attempt 1) only
    g, d = guard(tmp_path, ops=ops, report={"reason": "completed"})
    out = g.run()
    assert out["ok"] and len(out["attempts"]) == 2
    assert "error:OSError" in out["attempts"][0]["R5"]["detail"]
    assert (d / "restore-result.json").exists()


def test_every_step_failing_forever_still_runs_r7_and_saves_the_result(tmp_path):
    for name in ("mode", "start_service", "snapshot"):
        ops = Flaky(name, always=True)
        g, d = guard(tmp_path / name, ops=ops, report={"reason": "completed"})
        out = g.run()  # must not raise
        assert not out["ok"] and rg.exit_code(out) == 75
        assert (d / "restore-result.json").exists(), name
        assert out["r7"] and ops.calls.count("disable") == 1, name
    # A failing stop command is judged by the state it leaves: still active is a failure.
    stuck = Flaky("stop_trial", always=True, stopped=False)
    g, _ = guard(tmp_path / "stuck", ops=stuck, report={"reason": "completed"})
    assert not g.run()["ok"] and stuck.calls.count("start") == 0


def test_r7_runs_each_part_even_when_the_final_snapshot_fails(tmp_path):
    ops = Flaky("snapshot", always=True)
    g, d = guard(tmp_path, ops=ops, report={"reason": "completed"})
    out = g.run()
    assert ops.calls.count("disable") == 1  # the guard is still released
    assert out["r7"]["ok"] is False and any(e.startswith("after") for e in out["r7"]["errors"])


def test_an_interrupted_restore_still_closes_with_r7_and_reraises(tmp_path):
    ops = Flaky("mode", always=True, error=KeyboardInterrupt)
    g, d = guard(tmp_path, ops=ops, report={"reason": "completed"})
    with pytest.raises(KeyboardInterrupt):
        g.run()
    saved = json.loads((d / "restore-result.json").read_text())
    assert saved["interrupted"] == "KeyboardInterrupt" and saved["ok"] is False
    assert ops.calls[-1] == "disable"


def test_a_restarted_guard_reuses_outputs_of_the_earlier_start(tmp_path):
    g, d = guard(tmp_path, report={"reason": "completed"})
    first = g.post_run()
    (d / "after.attempt1.json").write_text("{}")
    (d / "after.json").write_text("{}")
    again = rg.Guard(FakeOps(), d, baseline(), 600)
    out = again.run()  # no FileExistsError, nothing overwritten
    assert out["ok"]
    assert again.post["native_sha256"] == first["native_sha256"]
    assert (d / "after.attempt1.r1.json").exists() and (d / "after.r1.json").exists()
    assert json.loads((d / "after.json").read_text()) == {}
    # A settled result is returned as is, without touching the system again.
    ops = FakeOps()
    settled = rg.Guard(ops, d, baseline(), 600).run()
    assert settled["ok"] and ops.calls == []


def test_post_run_happens_after_the_first_successful_r1_even_if_attempt_one_failed(tmp_path):
    class Late(FakeOps):
        def __init__(self):
            super().__init__()
            self.stops = 0

        def trial_state(self):
            self.stops += 1
            state = super().trial_state()
            return {**state, "active_state": "active" if self.stops == 1 else "inactive"}

    g, d = guard(tmp_path, ops=Late(), report={"reason": "completed"})
    out = g.run()
    assert out["ok"] and out["attempts"][0]["R1"]["ok"] is False
    assert (d / "post-run-check.json").exists() and out["post"]["native_size"] > 0
    assert out["post_ok"] is True


def test_without_a_report_the_last_seen_version_decides_and_unknown_is_never_a_pass(tmp_path):
    # No report: the baseline's version 3 is the fallback; an ACK at 3 is not new.
    g, _ = guard(tmp_path / "old", ops=FakeOps(version=3), report=None)
    out = g.run()
    assert not out["ok"] and "generation" in out["attempts"][0]["R3R4"]["detail"] + "ACK"
    g, _ = guard(tmp_path / "new", ops=FakeOps(version=4), report=None)
    assert g.run()["ok"]
    # Neither a report nor a recorded version: not exempt, fail closed.
    no_version = baseline()
    no_version["mode"].pop("version")
    d = run_dir(tmp_path / "none", report=None)
    out = rg.Guard(FakeOps(version=99), d, no_version, 600).run()
    assert not out["ok"]
    assert out["attempts"][0]["R3R4"]["detail"] == "generation used in the trial is unknown"


class Slow(FakeOps):
    """Each call costs time on the clock the guard measures deadlines with."""

    def __init__(self, **cost):
        super().__init__()
        self.cost = cost

    def mode(self):
        self.t += self.cost.get("mode", 0)
        return super().mode()

    def snapshot(self):
        self.t += self.cost.get("snapshot", 0)
        return super().snapshot()


def test_r6_is_limited_to_130_seconds_not_140(tmp_path):
    for cost, expected in [(0.5, True), (0.7, True), (0.8, False), (1.0, False)]:
        ops = Slow(mode=cost)
        ops.requests = 1
        g, _ = guard(tmp_path / str(cost), ops=ops)
        started = ops.t
        result = g.r6()
        # 12 waits of 10 s plus 13 samples of `cost` each.
        assert ops.t - started == pytest.approx(120 + 13 * cost)
        assert result.ok is expected, cost


def test_r7_is_limited_to_15_seconds(tmp_path):
    for cost, expected in [(7, True), (16, False)]:
        ops = Slow(snapshot=cost)
        g, d = guard(tmp_path / str(cost), ops=ops, report={"reason": "completed"})
        result = g.r7(True)
        assert result["ok"] is expected, cost
        assert (d / "after.json").exists()


def test_post_run_is_limited_to_30_seconds_and_a_late_one_blocks_a_pass(tmp_path):
    class Clock(FakeOps):
        pass

    g, d = guard(tmp_path, ops=Clock(), report={"reason": "completed"})
    original = g.read_report

    def slow():
        g.ops.t += 31
        return original()

    g.read_report = slow
    info = g.post_run()
    assert info["within_deadline"] is False and not rg.post_ok(info)
    quick, _ = guard(tmp_path / "q", report={"reason": "completed"})
    assert rg.post_ok(quick.post_run())
    g.post = info
    out = {"ok": True, "post": info, "post_ok": False}
    common.write_json(d / "restore-result.json", out) if not (
        d / "restore-result.json"
    ).exists() else None
    collector = environment.Collector(run=lambda argv: "", get_json=lambda n: {}, unit_path="/x")
    collector.snapshot = lambda: baseline()
    assert rg.final_verdict(d, collector, baseline())["result"] != "passed"


def test_a_report_without_generation_epochs_does_not_fall_back_to_the_baseline_version(tmp_path):
    # Version 4 is above the baseline's 3, so a silent fallback would pass R4.
    for name, report in [
        ("nomeasurement", {"reason": "completed"}),
        ("noepochs", {"reason": "completed", "measurement": {}}),
        ("emptyepochs", {"reason": "completed", "measurement": {"generation_epochs": []}}),
    ]:
        g, _ = guard(tmp_path / name, ops=FakeOps(version=4), report=report, epochs=False)
        out = g.run()
        assert not out["ok"], name
        assert out["attempts"][0]["R3R4"]["detail"] == "generation used in the trial is unknown"
    # With the epochs present the same ACK is accepted.
    ok = {"reason": "completed", "measurement": {"generation_epochs": [[1, 1], [2, 3]]}}
    g, _ = guard(tmp_path / "present", ops=FakeOps(version=4), report=ok)
    assert g.run()["ok"]
    # No report at all is still the documented fallback.
    g, _ = guard(tmp_path / "absent", ops=FakeOps(version=4), report=None)
    assert g.run()["ok"]


def test_a_camera_boot_change_during_the_trial_is_a_child_exit_first_and_a04_after_the_restore(
    tmp_path,
):
    # The native process leaves on the boot mismatch it sees: a non-zero exit (A07).
    child = {"exit": 1, "signalled": False, "guardian_stopped_child": False}
    g, d = guard(tmp_path, report={"reason": "boot_mismatch"}, marker="completed", child=child)
    out = g.run()
    assert out["post"]["abort_codes"] == ["A07"]
    collector = environment.Collector(run=lambda argv: "", get_json=lambda n: {}, unit_path="/x")
    collector.snapshot = lambda: dict(baseline(), camera_boot_id=BASE["camera_boot_id"] + 1)
    final = rg.final_verdict(d, collector, baseline())
    # The first recorded code stays the primary reason; the restore adds A04.
    assert final["result"] == "aborted" and final["primary"] == "A07"
    assert json.loads((d / "final.json").read_text())["milestone"] is False
    assert "invariant:camera_boot_id" in final["final_deviations"]
    # A boot change seen only after the restore, with nothing aborted before, is A04.
    quiet, d2 = guard(tmp_path / "quiet", report={"reason": "completed"})
    quiet.run()
    final = rg.final_verdict(d2, collector, baseline())
    assert final["primary"] == "A04" and final["milestone"] is False
