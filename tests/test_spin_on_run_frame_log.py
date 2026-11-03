import json
from pathlib import Path

import pytest

from scripts.spin_on_run import cli, manifest, start_gate, units
from scripts.spin_on_run import live_guardian as lg
from tests.test_spin_on_run_guardian import BASE, TRIAL, FakeServiceOps
from tests.test_spin_on_run_start_gate import evaluate, world  # noqa: F401 - fixture

KEY = "ESP32CAM_RUST_TRIAL_FRAME_LOG"
CREDS = {"ESP32CAM_DEVICE_API_KEYS": '{"esp32cam-01":"secret-value"}'}


def env(**kw):
    base = {
        "nonce": "0123456789abcdef",
        "camera_boot": 18174604300954271633,
        "seconds": 600,
        "credentials": CREDS,
    }
    return units.live_env(**{**base, **kw})


# --- units: the launch environment -------------------------------------------------------


def test_the_v2_live_environment_carries_the_frame_log_inside_the_stage_directory():
    e = env(run_dir="/home/ubuntu/spin-on-run/run")
    assert e[KEY] == "/home/ubuntu/spin-on-run/run/frames.jsonl"
    units.validate_live_env(e)
    units.check_frame_log(e, "/home/ubuntu/spin-on-run/run")


def test_without_a_stage_directory_the_environment_is_the_v1_set():
    e = env()
    assert KEY not in e
    units.validate_live_env(e)  # offline and v1 launches do not carry it


@pytest.mark.parametrize("bad", ["run", "./run", "/a/../b", "", "/a/./b"])
def test_the_stage_directory_must_be_absolute_without_dot_segments(bad):
    with pytest.raises(ValueError):
        units.frame_log_path(bad)
    with pytest.raises(ValueError):
        env(run_dir=bad)


@pytest.mark.parametrize(
    "value",
    [
        "frames.jsonl",
        "relative/frames.jsonl",
        "/run/other.jsonl",
        "/run/../etc/frames.jsonl",
        "",
        "/run/frames.jsonl/",
    ],
)
def test_validate_rejects_a_frame_log_that_is_not_an_absolute_stage_file(value):
    with pytest.raises(ValueError):
        units.validate_live_env({**env(), KEY: value})


def test_check_frame_log_requires_the_variable_and_the_same_directory():
    with pytest.raises(ValueError):
        units.check_frame_log(env(), "/run/x")
    with pytest.raises(ValueError):
        units.check_frame_log(env(run_dir="/run/y"), "/run/x")
    units.check_frame_log(env(run_dir="/run/x"), "/run/x")


def test_the_dry_run_launch_request_names_the_frame_log(tmp_path):
    out = tmp_path / "units"
    args = ["units", "--kind", "live", "--date", "20261105", "--out", str(out)]
    assert cli.main([*args, "--run-dir", "/home/ubuntu/spin-on-run/run"]) == 0
    request = json.loads((out / "launch-request.dry-run.json").read_text())
    assert request["env"][KEY] == "/home/ubuntu/spin-on-run/run/frames.jsonl"
    offline = tmp_path / "offline"
    assert cli.main(["units", "--kind", "offline", "--date", "d", "--out", str(offline)]) == 0
    assert KEY not in (offline / "offline-systemd-run.json").read_text()


# --- the live start ----------------------------------------------------------------------------


def test_the_cli_refuses_a_launch_environment_with_another_frame_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.units, "validate_live_env", lambda env: None)
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps({KEY: "/elsewhere/frames.jsonl"}))
    cfg = {"launch_env": str(env_file), "seconds": 600, "trial_unit": TRIAL}
    ops = FakeServiceOps()
    ops.collector = None
    with pytest.raises(ValueError):
        cli._execute(cfg, tmp_path / "run", dict(BASE), ops)
    assert ops.stop_calls == 0 and not (tmp_path / "run").exists()  # nothing was started


def test_live_start_checks_the_frame_log_environment(world):  # noqa: F811
    tmp, cfg, out = world
    env_file = Path(cfg["launch_env"])
    assert evaluate(cfg, out, live=True)["ok"]
    env_file.write_text(json.dumps({KEY: str(tmp / "elsewhere" / "frames.jsonl")}))
    assert "frame_log_env" in evaluate(cfg, out, live=True)["O05"]
    env_file.write_text(json.dumps({"ESP32CAM_RUST_TRIAL_SECONDS": "600"}))
    assert "frame_log_env" in evaluate(cfg, out, live=True)["O05"]
    env_file.write_text("not json")
    assert "launch_env_unreadable" in evaluate(cfg, out, live=True)["O05"]
    env_file.write_text(json.dumps([KEY]))
    assert "launch_env_unreadable" in evaluate(cfg, out, live=True)["O05"]
    env_file.write_text(json.dumps({KEY: units.frame_log_path(out)}))
    out.mkdir()
    (out / "frames.jsonl").write_text("x")
    assert "frame_log_exists" in evaluate(cfg, out, live=True)["O05"]


def test_a_missing_launch_environment_file_forbids_the_live_start(world):  # noqa: F811
    _tmp, cfg, out = world
    Path(cfg["launch_env"]).rename(Path(cfg["launch_env"]).with_suffix(".moved"))
    gate = evaluate(cfg, out, live=True)
    assert not gate["ok"] and "launch_env_unreadable" in gate["O05"]


def test_offline_start_does_not_look_at_the_frame_log(world):  # noqa: F811
    _tmp, cfg, out = world
    Path(cfg["launch_env"]).rename(Path(cfg["launch_env"]).with_suffix(".moved"))
    gate = evaluate(cfg, out, live=False)
    assert not any(code in gate["O05"] for code in ("frame_log_env", "launch_env_unreadable"))


def test_the_frozen_manifest_covers_the_launch_environment_and_the_frame_log(world):  # noqa: F811
    _tmp, cfg, _out = world
    frozen = json.loads(Path(cfg["manifest"]).read_text())
    assert KEY in frozen["live_env_keys"]
    assert frozen["frame_log"] == {"env": KEY, "file": "frames.jsonl"}
    assert start_gate.frozen_changes(cfg) == []
    frozen["live_env_keys"].remove(KEY)
    Path(cfg["manifest"]).write_text(manifest.dump(frozen))
    assert start_gate.frozen_changes(cfg) == ["live_env_keys"]
    frozen["live_env_keys"].append(KEY)
    frozen["live_env_keys"].sort()
    frozen["frame_log"]["file"] = "other.jsonl"
    Path(cfg["manifest"]).write_text(manifest.dump(frozen))
    assert start_gate.frozen_changes(cfg) == ["frame_log"]


# --- the guardian ---------------------------------------------------------------------------------


def test_the_frame_log_has_its_own_size_limit_in_the_stage(tmp_path):
    checker = lg.Checker(3600, tmp_path, dict(BASE), TRIAL)
    reading = lg.Reading(50_000, 0, 10 * 1024**3, None)
    frames = tmp_path / "frames.jsonl"
    with open(frames, "wb") as handle:
        handle.truncate(lg.FILE_LIMIT + 1)  # over the generic limit, a normal 3,600 s log
    assert not checker._capacity_exceeded(reading)
    with open(frames, "wb") as handle:
        handle.truncate(lg.FRAMES_LIMIT + 1)
    assert checker._capacity_exceeded(reading)
    frames.write_bytes(b"")
    (tmp_path / "other.json").write_bytes(b"x" * (lg.FILE_LIMIT + 1))
    assert checker._capacity_exceeded(reading)  # other files keep the generic limit
