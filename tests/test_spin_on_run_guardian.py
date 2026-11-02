import json
import re
import os
import signal
import stat
import sys

import pytest

from scripts.spin_on_run import common
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


class Collector:
    def __init__(self):
        self.snap = dict(BASE)
        self.fail = False

    def snapshot(self):
        if self.fail:
            raise OSError("down")
        return dict(self.snap, units_active=[TRIAL + ".service"])


def make_checker(tmp_path, collector=None):
    run_dir = tmp_path / "run"
    tmp_path.mkdir(exist_ok=True)
    native, stderr = lg.prepare_run_dir(run_dir)
    native.close()
    stderr.close()
    return lg.Checker(600, run_dir, dict(BASE), TRIAL, collector or Collector()), run_dir


def reading(temp=60_000, throttled=0, free=10 * 1024**3, fast=None):
    return lg.Reading(temp, throttled, free, fast if fast is not None else {})


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


def test_stale_mode_state_aborts_after_ten_seconds_of_read_errors(tmp_path):
    collector = Collector()
    checker, _ = make_checker(tmp_path, collector)
    checker.tick(0.0, reading(), True)
    collector.fail = True
    codes = []
    for second in range(1, 14):
        codes = checker.tick(float(second), reading(), True)
        if second <= 10:
            assert codes == [], second
    assert codes == ["A05"]


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
    broken = lg.Reading(None, None, None, {})
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


def drive(tmp_path, child, readings, collector=None):
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
        checker_factory=lambda d: lg.Checker(600, d, dict(BASE), TRIAL, collector or Collector()),
        io=lg.IO(monotonic, sleep, lambda now: next(it, reading()), lambda *a: child, lambda: 5),
    )
    return result, run_dir


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
