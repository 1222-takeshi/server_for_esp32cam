import json
import stat
import sys
from pathlib import Path

import pytest

from scripts.spin_on_run import offline_runner, quality
from scripts.spin_on_run.common import QUALITY_FIELDS, ROWS_PER_RUN

SUPPORT = Path(__file__).parent / "spin_on_run_support"
FAKE = SUPPORT / "fake_worker.py"


def golden_rows():
    rows = [
        {
            "input_sha256": "in",
            "raw_sha256": "raw",
            "detections": [{"label": "cat", "score": 0.5 + i / 1000, "track_id": None}],
            "runtime_build": "build",
            "sequence": i + 1,
            "boot_id": 7,
            "captured_us": 100 + i,
            "sent_us": 101 + i,
            "mode_generation": 1,
            "replay_only": True,
            "freshness_checked": False,
            "actuation_state": "hold",
        }
        for i in range(ROWS_PER_RUN)
    ]
    assert set(rows[0]) == set(QUALITY_FIELDS)
    return rows


class FakePlatform:
    def __init__(self):
        self.temp = 55_000
        self.hot_after = None
        self.throttle = 0
        self.deviations = []
        self.calls = 0

    def monotonic(self):
        import time

        return time.monotonic()

    def sleep(self, seconds):
        import time

        time.sleep(min(seconds, 0.01))

    def temp_millic(self):
        self.calls += 1
        if self.hot_after is not None and self.calls > self.hot_after:
            return 80_000
        return self.temp

    def throttled(self):
        return self.throttle

    def free_bytes(self, path):
        return 10 * 1024**3

    def stage_bytes(self, path):
        return 0

    def environment_deviations(self):
        return list(self.deviations)


@pytest.fixture()
def setup(tmp_path):
    golden = golden_rows()
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in golden) + "\n")
    (tmp_path / "input.ecf2").write_bytes(b"x")
    parsed = quality.load_golden(path.read_text())

    def make(mode="ok", platform=None, **overrides):
        out = tmp_path / f"out-{mode}-{len(list(tmp_path.iterdir()))}"
        cfg = offline_runner.Config(
            worker=FAKE,
            model=Path("m"),
            ort=Path("o"),
            input_path=tmp_path / "input.ecf2",
            golden=parsed,
            out_dir=out,
            clk_tck=100,
            prefix=[sys.executable],
            idle_s=0,
            monitor_period_s=0.02,
            extra_env={"FAKE_GOLDEN": str(path), "FAKE_MODE": mode},
            **overrides,
        )
        return offline_runner.Runner(cfg, platform or FakePlatform()), out

    return make


def test_child_env_carries_only_the_two_experiment_switches_and_path():
    a, b = offline_runner.child_env("A"), offline_runner.child_env("B")
    assert a == {
        "PATH": "/usr/bin:/bin",
        "ESP32CAM_RUST_BENCH_SPIN_ON_RUN": "0",
        "ESP32CAM_RUST_BENCH_FRAME_GAP_MS": "40",
    }
    assert b["ESP32CAM_RUST_BENCH_SPIN_ON_RUN"] == "1" and len(b) == 3


def test_full_dry_run_completes_all_sixteen_runs_and_decides(setup):
    runner, out = setup("ok")
    result = runner.run()
    assert result["marker"] == "completed" and result["codes"] == []
    assert result["runs_completed"] == 16
    assert [r["condition"] for r in runner.runs] == list("ABBABAABABBABAAB")
    assert result["verdict"] == {"id": "V6", "result": "improvement"}
    assert (out / "marker").read_text() == "completed\n"
    assert stat.S_IMODE((out / "result.json").stat().st_mode) == 0o400
    assert len(list(out.glob("run*-?.jsonl"))) == 16
    with pytest.raises(FileExistsError):  # new files are created exclusively
        (out / "marker").open("x")


def test_b_quality_mismatch_stops_immediately_as_worse(setup):
    runner, out = setup("mismatch_b")
    result = runner.run()
    assert result["codes"] == ["O01"] and result["verdict"]["id"] == "V2"
    assert result["marker"] == "abort O01"
    assert runner.runs[-1]["condition"] != "B" or len(runner.runs) < 16
    assert "field=raw_sha256" in "".join(result["details"])


def test_a_quality_mismatch_is_harness_failure_not_worse(setup):
    result = setup("mismatch_a")[0].run()
    assert result["codes"] == ["O02"] and result["verdict"]["id"] == "V1"


def test_short_run_and_stderr_are_independent_harness_failures(setup):
    short = setup("short")[0].run()
    assert short["codes"] == ["O06"] and "rows=84" in "".join(short["details"])
    noisy = setup("stderr")[0].run()
    assert noisy["codes"] == ["O06"] and noisy["verdict"]["id"] == "V1"


def test_start_temperature_wait_times_out_as_o04(setup):
    platform = FakePlatform()
    platform.temp = 70_001
    runner, _ = setup("ok", platform, temp_wait_s=0.1)
    result = runner.run()
    assert result["codes"] == ["O04"] and result["runs_completed"] == 0


def test_start_temperature_exactly_at_limit_is_allowed(setup):
    platform = FakePlatform()
    platform.temp = 70_000
    result = setup("ok", platform)[0].run()
    assert result["codes"] == []


def test_thermal_stop_and_throttle_are_indeterminate_not_worse(setup):
    platform = FakePlatform()
    platform.hot_after = 12
    result = setup("ok", platform)[0].run()
    assert result["codes"] == ["O03"] and result["verdict"]["id"] == "V3"
    throttled = FakePlatform()
    throttled.throttle = 0x50005
    result = setup("ok", throttled)[0].run()
    assert result["codes"] == ["O03"]


def test_environment_deviation_stops_with_o05(setup):
    platform = FakePlatform()
    platform.deviations = ["unexpected_unit:esp32cam-other.service"]
    result = setup("ok", platform)[0].run()
    assert result["codes"] == ["O05"] and result["verdict"]["id"] == "V1"


def test_files_are_read_only_and_never_overwritten(setup):
    runner, out = setup("ok")
    runner.run()
    for path in out.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o400, path.name
