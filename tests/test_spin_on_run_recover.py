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


def run_dir(tmp_path, report=None, marker="completed", child=None):
    d = tmp_path / "trial"
    d.mkdir(parents=True)
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
    assert g2.r3_r4(None).ok


def test_eco_is_requested_until_acknowledged(tmp_path):
    ops = FakeOps(eco_after_requests=3)
    g, _ = guard(tmp_path, ops=ops)
    assert g.r3_r4(None).ok and ops.calls.count("eco") == 3


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
