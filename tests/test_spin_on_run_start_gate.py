import copy
import json
from pathlib import Path

import pytest

from scripts.spin_on_run import (
    cli,
    common,
    environment,
    live_guardian,
    manifest,
    post_runner,
    start_gate,
    units,
)

PACKAGE = Path(cli.__file__).parent
TRIAL = "esp32cam-spin-on-run-v1-live-600-20261105"
SNAPSHOT = {
    "unit_sha": common.HASHES["original_unit"],
    "dropin_paths": "",
    "service_active": True,
    "units_active": ["esp32cam-server.service"],
    "last_firmware_version": "fw-1",
    "pi_boot_id": "b",
    "camera_boot_id": 1,
    "governor": ["ondemand"],
    "cpus_allowed": "0-3",
    "nice_policy": "x",
    "mode": {
        "ready": True,
        "state": "active",
        "requested_mode": "eco",
        "effective_mode": "eco",
        "acknowledged": True,
        "actuation_state": "hold",
    },
}


class FakeCollector:
    def __init__(self, snapshot=None):
        self.value = copy.deepcopy(snapshot or SNAPSHOT)

    def snapshot(self):
        return copy.deepcopy(self.value)


class FakeOps:
    def __init__(self, collector, ok=True):
        self.collector, self.ok = collector, ok

    def snapshot(self):
        return self.collector.snapshot()

    def mode(self):
        if not self.ok:
            raise OSError("api down")
        return {}

    def ready(self):
        return True


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Config, fixed files, frozen manifest and executables that all agree."""
    files = {}
    for label in start_gate.FIXED_FILES:
        path = tmp_path / label
        path.write_bytes(label.encode())
        files[label] = str(path)
        monkeypatch.setitem(common.HASHES, label, common.sha256_file(path))
    binaries = {}
    for label in start_gate.BINARY_LABELS:
        path = tmp_path / label
        path.write_bytes(b"elf-" + label.encode())
        binaries[label] = str(path)
    frozen = manifest.build(PACKAGE, {k: Path(v) for k, v in binaries.items()}, "abc", 1)
    (tmp_path / "manifest.json").write_text(manifest.dump(frozen))
    (tmp_path / "before.json").write_text(json.dumps(SNAPSHOT))
    out = tmp_path / "out"
    unit_file = tmp_path / units.RECOVER_UNIT
    text = units.recover_unit_text(TRIAL, "/root", str(tmp_path / "c.json"), str(out))
    unit_file.write_text(text)
    cfg = {
        **files,
        "binaries": binaries,
        "manifest": str(tmp_path / "manifest.json"),
        "baseline": str(tmp_path / "before.json"),
        "trial_unit": TRIAL,
        "root": "/root",
        "config_path": str(tmp_path / "c.json"),
        "recover_unit_path": str(unit_file),
        "old_guard_units": [],
        "base_url": "http://x",
        "unit_path": "/u",
        "offline_unit": "off",
        "post_unit": "post",
        "seconds": 600,
        "launch_env": str(tmp_path / "env.json"),
    }
    (tmp_path / "c.json").write_text(json.dumps(cfg))
    # v2: the launch environment names the R3 frame log inside the stage directory.
    (tmp_path / "env.json").write_text(json.dumps({units.FRAME_LOG_KEY: units.frame_log_path(out)}))
    return tmp_path, cfg, out


def run_fake(enabled="enabled", linger="Linger=yes\n", active="inactive"):
    def run(argv):
        if "is-enabled" in argv:
            if "esp32cam-old-guard.service" in argv:
                raise OSError("disabled")
            if enabled is None:
                raise OSError("disabled")
            return enabled
        if "is-active" in argv:
            return active
        if argv[0] == "loginctl":
            return linger
        raise AssertionError(argv)

    return run


def evaluate(cfg, out, *, live=False, snapshot=None, free=3 * 1024**3, temp=50_000, **kw):
    collector = FakeCollector(snapshot)
    return start_gate.evaluate(
        cfg,
        live=live,
        collector=collector,
        baseline=copy.deepcopy(SNAPSHOT),
        out_dir=out,
        free_bytes=lambda _p: free,
        temp_millic=lambda: temp,
        run=kw.pop("run", run_fake()),
        ops=kw.pop("ops", FakeOps(collector)),
        **kw,
    )


def test_offline_start_passes_when_everything_matches_the_frozen_state(world):
    _tmp, cfg, out = world
    result = evaluate(cfg, out)
    assert result["ok"], result
    assert result["O05"] == [] and result["O07"] == []


def test_a_changed_frozen_file_or_binary_forbids_the_start(world):
    _tmp, cfg, out = world
    frozen = json.loads(Path(cfg["manifest"]).read_text())
    frozen["files"]["cli.py"] = "0" * 64
    Path(cfg["manifest"]).write_text(manifest.dump(frozen))
    assert "frozen:cli.py" in evaluate(cfg, out)["O05"]
    assert not evaluate(cfg, out)["ok"]
    Path(cfg["binaries"]["frame-worker"]).write_bytes(b"patched")
    assert "frozen:binary:frame-worker" in evaluate(cfg, out)["O05"]


def test_the_manifest_must_exist_and_carry_the_registered_plan(world):
    _tmp, cfg, out = world
    frozen = json.loads(Path(cfg["manifest"]).read_text())
    frozen["plan_sha256"]["plan.md"] = "0" * 64
    Path(cfg["manifest"]).write_text(manifest.dump(frozen))
    assert "frozen:plan_sha256" in evaluate(cfg, out)["O05"]
    Path(cfg["manifest"]).unlink()
    gate = evaluate(cfg, out)
    assert not gate["ok"] and any(x.startswith("frozen:manifest_unreadable") for x in gate["O05"])


@pytest.mark.parametrize(
    ("change", "expected"),
    [({"free": 1}, "free_space"), ({"temp": 70_001}, "start_temperature")],
)
def test_environment_facts_are_checked_before_an_offline_start(world, change, expected):
    _tmp, cfg, out = world
    assert expected in evaluate(cfg, out, **change)["O05"]


def test_fixed_input_hashes_and_the_firmware_read_path_are_checked(world):
    _tmp, cfg, out = world
    Path(cfg["model"]).write_bytes(b"other model")
    assert "fixed_hash:model" in evaluate(cfg, out)["O05"]
    gate = evaluate(cfg, out, snapshot={**SNAPSHOT, "last_firmware_version": "unverified"})
    assert gate["O07"] and not gate["ok"]


def test_an_environment_that_cannot_be_read_forbids_the_start(world):
    _tmp, cfg, out = world

    class Down:
        def snapshot(self):
            raise OSError("no systemd")

    gate = start_gate.evaluate(
        cfg,
        live=False,
        collector=Down(),
        baseline=copy.deepcopy(SNAPSHOT),
        out_dir=out,
        free_bytes=lambda _p: 3 * 1024**3,
        temp_millic=lambda: 50_000,
    )
    assert not gate["ok"] and gate["O07"]


def test_live_start_needs_the_guard_and_linger(world):
    _tmp, cfg, out = world
    assert evaluate(cfg, out, live=True)["ok"]
    no_linger = evaluate(cfg, out, live=True, run=run_fake(linger="Linger=no\n"))
    assert "linger_not_enabled" in no_linger["O05"]
    off = evaluate(cfg, out, live=True, run=run_fake(enabled=None))
    assert "recover_unit_not_enabled" in off["O05"]
    Path(cfg["recover_unit_path"]).write_text("[Unit]\n")
    assert "recover_unit_sha" in evaluate(cfg, out, live=True)["O05"]


def test_live_start_runs_the_recover_check_and_the_standing_guard_check(world):
    _tmp, cfg, out = world
    failing = FakeOps(FakeCollector(), ok=False)
    assert "recover_check_failed" in evaluate(cfg, out, live=True, ops=failing)["O05"]
    unknown = {k: v for k, v in cfg.items() if k != "old_guard_units"}
    assert evaluate(unknown, out, live=True)["O07"]
    named = {**cfg, "old_guard_units": ["esp32cam-old-guard.service"]}
    assert evaluate(named, out, live=True, run=run_fake(active="inactive"))["ok"]
    busy = evaluate(named, out, live=True, run=run_fake(active="active"))
    assert not busy["ok"]


def test_binaries_come_from_the_config_and_both_labels_are_required(world):
    _tmp, cfg, _out = world
    assert set(start_gate.binaries(cfg)) == {"frame-worker", "frame-live"}
    assert cli._with_binaries(cfg)["worker"] == cfg["binaries"]["frame-worker"]
    assert cli._with_binaries(cfg)["frame_live"] == cfg["binaries"]["frame-live"]
    with pytest.raises(KeyError):
        start_gate.binaries({"binaries": {"frame-worker": "x"}})


class Sentinel(Exception):
    pass


def test_offline_run_is_refused_before_any_run_starts(world, monkeypatch):
    _tmp, cfg, out = world
    refusal = {"ok": False, "O05": ["frozen:cli.py"], "O07": [], "notes": []}
    monkeypatch.setattr(cli, "_gate", lambda *a, **k: refusal)

    def forbidden(*_a, **_k):
        raise Sentinel("the harness must not be built after a refusal")

    monkeypatch.setattr(cli, "_offline_config", forbidden)
    assert cli.main(["offline-run", cfg["config_path"], str(out)]) == start_gate.REFUSED_EXIT
    assert (out / "marker").read_text() == "abort O05\n"
    result = json.loads((out / "result.json").read_text())
    assert result["verdict"]["id"] == "V1" and result["codes"] == ["O05"]
    assert json.loads((out / "preflight.json").read_text())["O05"] == ["frozen:cli.py"]


def test_offline_run_starts_only_after_the_gate_passes(world, monkeypatch):
    _tmp, cfg, out = world
    seen = {}

    class Runner:
        codes: list = []

        def run(self):
            seen["preflight_written_first"] = (out / "preflight.json").exists()
            return {"verdict": {"id": "V7"}}

    monkeypatch.setattr(cli, "_gate", lambda *a, **k: {"ok": True, "O05": [], "O07": []})
    monkeypatch.setattr(cli, "_offline_config", lambda c, o, thread: seen.update(cfg=c) or Runner())
    assert cli.main(["offline-run", cfg["config_path"], str(out)]) == 0
    assert seen["preflight_written_first"]
    assert seen["cfg"]["worker"] == cfg["binaries"]["frame-worker"]  # the verified binary runs


def test_live_execute_is_refused_before_the_guardian_starts(world, monkeypatch):
    _tmp, cfg, out = world
    refusal = {"ok": False, "O05": ["recover_unit_not_enabled"], "O07": [], "notes": []}
    monkeypatch.setattr(cli, "_gate", lambda *a, **k: refusal)

    def forbidden(**_k):
        raise Sentinel("the guardian must not start after a refusal")

    monkeypatch.setattr(live_guardian, "execute", forbidden)
    assert cli.main(["live", cfg["config_path"], str(out), "execute"]) == start_gate.REFUSED_EXIT
    refused = json.loads((out / "start-refused.json").read_text())
    assert refused["O05"] == ["recover_unit_not_enabled"]
    assert not (out / "native.json").exists()


def test_live_execute_gates_with_the_live_flag_and_the_recover_ops(world, monkeypatch):
    _tmp, cfg, out = world
    called = {}

    def gate(config, baseline, run_dir, *, live, ops=None):
        called.update(live=live, ops=ops, binaries=config["frame_live"])
        return {"ok": False, "O05": ["x"], "O07": []}

    monkeypatch.setattr(cli, "_gate", gate)
    cli.main(["live", cfg["config_path"], str(out), "execute"])
    assert called["live"] is True and called["ops"] is not None
    assert called["binaries"] == cfg["binaries"]["frame-live"]


def test_recover_and_enqueue_are_not_gated(world, monkeypatch):
    """Restoration must run even when the start conditions no longer hold."""
    _tmp, cfg, out = world
    monkeypatch.setattr(cli, "_gate", lambda *a, **k: pytest.fail("gate on a restore step"))

    def stop(*_a):
        raise Sentinel

    monkeypatch.setattr(cli.os, "execvp", stop)
    out.mkdir()
    with pytest.raises(Sentinel):
        cli.main(["live", cfg["config_path"], str(out), "enqueue"])


def test_the_follow_up_fails_and_settles_o08_when_frozen_code_changed(tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    collector = environment.Collector(run=lambda argv: "", get_json=lambda n: {}, unit_path="/u")
    collector.snapshot = lambda: {**SNAPSHOT, "units_active": []}
    done = post_runner.run_post(
        out,
        collector,
        SNAPSHOT,
        offline_unit="off.service",
        post_unit="post.service",
        frozen_changes=["cli.py"],
    )
    assert not done["pass"] and "frozen:cli.py" in done["reasons"]
    final = post_runner.final_check(out, collector, SNAPSHOT, frozen_changes=["cli.py"])
    assert "O08" in final["codes"] and final["verdict"]["id"] == "V1"


def test_the_gate_module_is_frozen():
    assert "start_gate.py" in manifest.FROZEN_FILES


def _with_units(*names):
    return {**SNAPSHOT, "units_active": ["esp32cam-server.service", *names]}


def _unexpected(result):
    return [x for x in result["O05"] if "unexpected_unit" in x]


def test_the_offline_gate_does_not_flag_its_own_unit(world):
    _tmp, cfg, out = world
    result = evaluate(cfg, out, snapshot=_with_units("off.service"))
    assert _unexpected(result) == [], result
    assert result["ok"], result


def test_the_live_gate_does_not_flag_its_own_trial_and_recover_units(world):
    _tmp, cfg, out = world
    snapshot = _with_units(TRIAL + ".service", units.RECOVER_UNIT)
    result = evaluate(cfg, out, live=True, snapshot=snapshot)
    assert _unexpected(result) == [], result
    assert result["O05"] == [], result
    assert result["ok"], result


def test_a_foreign_esp32cam_unit_still_fails_the_live_recover_check(world):
    _tmp, cfg, out = world
    snapshot = _with_units(TRIAL + ".service", "esp32cam-other.service")
    result = evaluate(cfg, out, live=True, snapshot=snapshot)
    assert result["O05"] and not result["ok"], result


def test_a_live_config_without_offline_unit_is_evaluated(world):
    _tmp, cfg, out = world
    live_cfg = {k: v for k, v in cfg.items() if k != "offline_unit"}
    result = evaluate(live_cfg, out, live=True, snapshot=_with_units(TRIAL + ".service"))
    assert result["ok"], result


def test_another_esp32cam_unit_is_still_unexpected(world):
    _tmp, cfg, out = world
    other = "esp32cam-other.service"
    assert _unexpected(evaluate(cfg, out, snapshot=_with_units(other)))
    assert _unexpected(evaluate(cfg, out, live=True, snapshot=_with_units(other)))
    # An allowed unit does not excuse a different one running next to it.
    assert _unexpected(evaluate(cfg, out, snapshot=_with_units("off.service", other)))


@pytest.mark.parametrize(
    "name", ["off", "off.service.bak", "xoff.service", "off-2.service", "OFF.service", "off.timer"]
)
def test_near_miss_unit_names_are_still_unexpected(world, name):
    _tmp, cfg, out = world
    assert _unexpected(evaluate(cfg, out, snapshot=_with_units(name)))


@pytest.mark.parametrize("name", [TRIAL, TRIAL + "-x.service", "x" + TRIAL + ".service"])
def test_near_miss_live_unit_names_are_still_unexpected(world, name):
    _tmp, cfg, out = world
    assert _unexpected(evaluate(cfg, out, live=True, snapshot=_with_units(name)))


class PendingThenActive(FakeCollector):
    def __init__(self, pending_reads):
        super().__init__()
        self.pending = pending_reads

    def snapshot(self):
        value = super().snapshot()
        if self.pending > 0:
            self.pending -= 1
            value["mode"]["state"] = "pending"
        return value


def gate_with(cfg, out, collector):
    return start_gate.evaluate(
        cfg,
        live=False,
        collector=collector,
        baseline=copy.deepcopy(SNAPSHOT),
        out_dir=out,
        free_bytes=lambda _p: 3 * 1024**3,
        temp_millic=lambda: 50_000,
        run=run_fake(),
    )


def test_preflight_rechecks_a_pending_mode_and_records_it(world, monkeypatch):
    from types import SimpleNamespace

    now = [0.0]
    monkeypatch.setattr(
        environment,
        "time",
        SimpleNamespace(sleep=lambda s: now.__setitem__(0, now[0] + s), monotonic=lambda: now[0]),
    )
    _tmp, cfg, out = world
    gate = gate_with(cfg, out, PendingThenActive(1))
    assert gate["ok"], gate
    assert gate["mode_recheck"]["rechecked"] is True and gate["mode_recheck"]["rereads"] == 2


def test_preflight_still_fails_when_the_mode_stays_pending(world, monkeypatch):
    from types import SimpleNamespace

    now = [0.0]
    monkeypatch.setattr(
        environment,
        "time",
        SimpleNamespace(sleep=lambda s: now.__setitem__(0, now[0] + s), monotonic=lambda: now[0]),
    )
    _tmp, cfg, out = world
    gate = gate_with(cfg, out, PendingThenActive(99))
    assert not gate["ok"] and "mode_not_restored" in gate["O05"]
    assert gate["mode_recheck"]["rereads"] == 10
