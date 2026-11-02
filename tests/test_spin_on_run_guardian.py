import json
import re
import os
import signal
import stat
import subprocess
import sys

import pytest

from scripts.spin_on_run import cli, common, system_ops
from scripts.spin_on_run import live_guardian as lg

BASE = {
    "pi_boot_id": "b",
    "unit_sha": common.HASHES["original_unit"],
    "dropin_paths": "",
    "governor": ["ondemand"],
    "cpus_allowed": "0-3",
    "nice_policy": "x",
    "camera_boot_id": 1,
    "last_firmware_version": "fw",
    "service_active": False,
    "units_active": [],
    "mode": {},
}
TRIAL = "esp32cam-spin-on-run-v1-live-600-20261105"


def local(**overrides):
    """What `Collector.snapshot_local()` returns while the trial runs."""
    keys = {
        k: v
        for k, v in BASE.items()
        if k not in {"camera_boot_id", "last_firmware_version", "mode"}
    }
    return {
        **keys,
        "units_active": [TRIAL + ".service"],
        "cpus_allowed": "",
        "nice_policy": "",
        **overrides,
    }


def make_checker(tmp_path):
    run_dir = tmp_path / "run"
    tmp_path.mkdir(exist_ok=True)
    native, stderr = lg.prepare_run_dir(run_dir)
    native.close()
    stderr.close()
    return lg.Checker(600, run_dir, dict(BASE), TRIAL), run_dir


def reading(temp=60_000, throttled=0, free=10 * 1024**3, fast=None):
    return lg.Reading(temp, throttled, free, local(**(fast or {})))


def run_ticks(checker, count, **kw):
    for second in range(count):
        checker.tick(float(second), reading(**kw), True)
    return checker.codes


def test_run_dir_is_new_private_and_logs_exist_from_the_start(tmp_path):
    checker, run_dir = make_checker(tmp_path)
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert all((run_dir / name).exists() for name in lg.LOGS)
    with pytest.raises(FileExistsError):
        lg.prepare_run_dir(run_dir)
    assert run_ticks(checker, 8) == []


def test_temperature_limits_are_exact_and_warning_is_not_an_abort(tmp_path):
    checker, _ = make_checker(tmp_path)
    assert run_ticks(checker, 3, temp=79_999) == [] and checker.warnings
    checker, _ = make_checker(tmp_path / "x")
    assert run_ticks(checker, 1, temp=80_000) == ["A01"]
    checker, _ = make_checker(tmp_path / "y")
    assert run_ticks(checker, 1, temp=74_999) == [] and checker.warnings == []


def test_throttle_capacity_and_invariants(tmp_path):
    for name, kwargs, code in [
        ("t", {"throttled": 0x50000}, "A02"),
        ("f", {"free": 1024**3 - 1}, "A03"),
        ("i", {"fast": {"pi_boot_id": "changed"}}, "A04"),
        ("s", {"fast": {"unit_sha": "0" * 64}}, "A04"),
        ("u", {"fast": {"units_active": ["esp32cam-other.service"]}}, "A04"),
    ]:
        checker, _ = make_checker(tmp_path / name)
        assert run_ticks(checker, 1, **kwargs) == [code], name
    checker, _ = make_checker(tmp_path / "ok")
    assert run_ticks(checker, 1, free=1024**3) == []
    assert run_ticks(checker, 1, fast={"units_active": [TRIAL + ".service"]}) == []


def test_stale_vitals_abort_only_after_three_seconds(tmp_path):
    checker, _ = make_checker(tmp_path)
    checker.tick(0.0, reading(), True)
    broken = lg.Reading(None, None, None, None)
    for second in (1.0, 2.0, 3.0):
        assert checker.tick(second, broken, True) == []
    assert checker.tick(3.5, broken, True) == ["A05"]


def test_stale_local_snapshot_aborts_after_three_seconds_of_read_errors(tmp_path):
    checker, _ = make_checker(tmp_path)
    checker.tick(0.0, reading(), True)
    no_snapshot = lg.Reading(60_000, 0, 10 * 1024**3, None)
    for second in (1.0, 2.0, 3.0):
        assert checker.tick(second, no_snapshot, True) == []
    assert checker.tick(3.5, no_snapshot, True) == ["A05"]


def test_the_checker_never_needs_the_original_services_api(tmp_path):
    import inspect

    checker, _ = make_checker(tmp_path)
    assert not hasattr(checker, "collector")
    assert "snapshot()" not in inspect.getsource(lg.Checker)
    # Many seconds with the service (and so its API) stopped stay clean.
    assert run_ticks(checker, 30) == []


def test_the_original_service_running_or_restarting_during_the_trial_is_a_deviation(tmp_path):
    checker, _ = make_checker(tmp_path)
    running = local(service_active=True, cpus_allowed=BASE["cpus_allowed"], nice_policy="x")
    assert run_ticks(checker, 1, fast=running) == ["A04"]


def test_log_loss_and_oversize_files(tmp_path):
    checker, run_dir = make_checker(tmp_path)
    (run_dir / "native.stderr").unlink()
    assert run_ticks(checker, 1) == ["A08"]
    checker, run_dir = make_checker(tmp_path / "a")
    (run_dir / "native.stderr").write_bytes(b"x" * (lg.STDERR_LIMIT + 1))
    assert "A03" in run_ticks(checker, 1)
    checker, run_dir = make_checker(tmp_path / "b")
    (run_dir / "native.json").write_bytes(b"")  # empty report is normal while running
    assert run_ticks(checker, 1) == []


def test_vitals_row_shortfall_is_log_loss(tmp_path):
    checker, run_dir = make_checker(tmp_path)
    for second in range(6):
        checker.tick(float(second), reading(), True)
    broken = lg.Reading(None, None, None, local())
    for second in range(6, 12):
        checker.tick(float(second), broken, True)
    assert "A08" in checker.codes or "A05" in checker.codes


def test_saved_camera_images_abort(tmp_path):
    checker, run_dir = make_checker(tmp_path)
    (run_dir / "frame.jpg").write_bytes(b"x")
    assert run_ticks(checker, 1) == ["A09"]


def test_markers(tmp_path):
    assert lg.write_marker(tmp_path, []).read_text() == "completed\n"
    other = tmp_path / "o"
    other.mkdir()
    assert lg.write_marker(other, ["A05", "A01"]).read_text() == "abort A05,A01\n"
    assert lg.heartbeat_line("0123456789abcdef", 5) == b"ready 0123456789abcdef 5\n"


class FakeChild:
    def __init__(self, live_for, exit_code=0):
        self.live_for = live_for
        self.exit_code = exit_code
        self.returncode = None
        self.signals = []
        self.stdin = open(os.devnull, "wb")  # noqa: SIM115

    def poll(self):
        if self.live_for <= 0 and self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = -signal.SIGTERM if signal.SIGTERM in self.signals else -9
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.signals.append(signal.SIGKILL)


class FakeServiceOps:
    """The original service as the systemd user manager holds it: `stop_service` changes it."""

    def __init__(self, fail=False):
        self.active = True
        self.fail = fail
        self.stop_calls = 0

    def stop_service(self):
        self.stop_calls += 1
        if self.fail:
            raise RuntimeError("stop failed")
        self.active = False

    def read(self, now):
        fast = local(service_active=self.active)
        if self.active:
            fast.update(cpus_allowed=BASE["cpus_allowed"], nice_policy="x")
        return lg.Reading(60_000, 0, 10 * 1024**3, fast)


def drive(tmp_path, child, readings):
    clock = {"t": 0.0}
    it = iter(readings)

    def monotonic():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds
        child.live_for -= seconds

    run_dir = tmp_path / "trial"
    result = lg.execute(
        run_dir=run_dir,
        seconds=600,
        argv=[sys.executable],
        env={},
        nonce="0123456789abcdef",
        checker_factory=lambda d: lg.Checker(600, d, dict(BASE), TRIAL),
        io=lg.IO(
            monotonic,
            sleep,
            lambda now: next(it, reading()),
            lambda *a: child,
            lambda: 5,
            lambda: None,
        ),
    )
    return result, run_dir


def drive_with_ops(tmp_path, ops, stop_service=None):
    """Like `drive`, but readings come from the fake service state and the stop goes through it."""
    clock = {"t": 0.0}
    child = FakeChild(live_for=3.0)
    spawned = []

    def spawn(*args):
        spawned.append(ops.active)  # the service state at the moment the child starts
        return child

    def sleep(seconds):
        clock["t"] += seconds
        child.live_for -= seconds

    run_dir = tmp_path / "trial"
    result = lg.execute(
        run_dir=run_dir,
        seconds=600,
        argv=[sys.executable],
        env={},
        nonce="0123456789abcdef",
        checker_factory=lambda d: lg.Checker(600, d, dict(BASE), TRIAL),
        io=lg.IO(
            lambda: clock["t"], sleep, ops.read, spawn, lambda: 5, stop_service or ops.stop_service
        ),
    )
    return result, run_dir, spawned


def test_normal_completion_writes_a_completed_marker(tmp_path):
    result, run_dir = drive(tmp_path, FakeChild(live_for=3.0), [])
    assert result["codes"] == [] and (run_dir / "marker").read_text() == "completed\n"
    end = json.loads((run_dir / "child-end.json").read_text())
    assert end == {"exit": 0, "signalled": False, "guardian_stopped_child": False}
    assert stat.S_IMODE((run_dir / "marker").stat().st_mode) == 0o400


def test_a_violation_stops_the_child_and_marks_the_abort(tmp_path):
    child = FakeChild(live_for=100.0)
    result, run_dir = drive(tmp_path, child, [reading(), reading(temp=80_000)])
    assert result["codes"] == ["A01"] and (run_dir / "marker").read_text() == "abort A01\n"
    assert signal.SIGTERM in child.signals
    end = json.loads((run_dir / "child-end.json").read_text())
    assert end["guardian_stopped_child"] is True and end["signalled"] is True


def test_the_guardian_does_not_restore_anything_after_stopping():
    import inspect

    source = inspect.getsource(lg)
    for forbidden in ("systemctl", "request_eco", "disable", "after.json"):
        assert forbidden not in source
    assert not re.search(r"\bota\b", source.lower()) and "3232" not in source


def test_the_planned_stop_runs_once_before_the_child_and_the_first_tick(tmp_path):
    ops = FakeServiceOps()
    result, run_dir, spawned = drive_with_ops(tmp_path, ops)
    assert ops.stop_calls == 1 and spawned == [False]  # stopped before the child started
    assert result["codes"] == [] and (run_dir / "marker").read_text() == "completed\n"
    assert json.loads((run_dir / "service-stop.json").read_text()) == {"stopped": True}
    assert ops.active is False  # the guardian does not start it again


def test_without_the_planned_stop_the_first_tick_is_a_deviation(tmp_path):
    ops = FakeServiceOps()
    result, run_dir, _ = drive_with_ops(tmp_path, ops, stop_service=lambda: None)
    assert ops.stop_calls == 0 and ops.active is True
    assert result["codes"] == ["A04"] and (run_dir / "marker").read_text() == "abort A04\n"


def test_a_failed_stop_refuses_the_start_and_leaves_recovery_to_the_guard(tmp_path):
    ops = FakeServiceOps(fail=True)
    result, run_dir, spawned = drive_with_ops(tmp_path, ops)
    assert ops.stop_calls == 1  # no retry
    assert spawned == []  # the child never started
    assert result["start_refused"] and result["codes"] == ["A04"]
    assert (run_dir / "marker").read_text() == "abort A04\n"
    end = json.loads((run_dir / "child-end.json").read_text())
    assert end["child_started"] is False and end["guardian_stopped_child"] is False
    assert json.loads((run_dir / "service-stop.json").read_text())["stopped"] is False
    assert ops.active is True  # restoring it is the independent guard's job, not the guardian's


def test_the_cli_hands_the_service_stop_of_its_ops_to_the_guardian(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        cli.live_guardian, "execute", lambda **kw: captured.update(kw) or {"codes": []}
    )
    monkeypatch.setattr(cli.units, "validate_live_env", lambda env: None)
    monkeypatch.setattr(cli.units, "native_argv", lambda *a: ["frame-live"])
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps({"ESP32CAM_RUST_TRIAL_NONCE": "0123456789abcdef"}))
    cfg = {
        "launch_env": str(env_file),
        "seconds": 600,
        "frame_live": "f",
        "model": "m",
        "ort": "o",
        "trial_unit": TRIAL,
    }
    ops = FakeServiceOps()
    ops.collector = None
    assert cli._execute(cfg, tmp_path / "run", dict(BASE), ops) == 0
    # The callable the guardian holds is the one that stops the original service.
    assert ops.stop_calls == 0
    captured["io"].stop_service()
    assert ops.stop_calls == 1 and ops.active is False


def test_a_stop_timeout_refuses_the_start_and_the_stop_is_not_repeated(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "stop" in argv:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return "ActiveState=active\n"

    ops = system_ops.SystemOps(
        trial_unit="t", self_unit="r", base_url="http://x", admin_token=None, run=run
    )
    service = FakeServiceOps()
    result, run_dir, spawned = drive_with_ops(tmp_path, service, stop_service=ops.stop_service)
    assert len([argv for argv in calls if "stop" in argv]) == 1  # no second stop
    assert spawned == []  # the child never started
    assert result["start_refused"] and result["codes"] == ["A04"]
    assert (run_dir / "marker").read_text() == "abort A04\n"
    end = json.loads((run_dir / "child-end.json").read_text())
    assert end["child_started"] is False and end["guardian_stopped_child"] is False
    stop = json.loads((run_dir / "service-stop.json").read_text())
    assert stop["stopped"] is False and "timed out" in stop["error"]
