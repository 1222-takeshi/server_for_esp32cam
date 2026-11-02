import json
import re
import stat
from pathlib import Path

import pytest

from scripts.spin_on_run import cli, common, manifest, preflight, system_ops, units

PACKAGE = Path(cli.__file__).parent


def test_units_command_writes_dry_run_files_without_credentials(tmp_path):
    out = tmp_path / "live"
    assert cli.main(["units", "--kind", "live", "--date", "20261105", "--out", str(out)]) == 0
    request = json.loads((out / "launch-request.dry-run.json").read_text())
    assert "placeholder" not in json.dumps(request)
    assert request["env"]["ESP32CAM_RUST_TRIAL_SPIN_ON_RUN"] == "1"
    text = (out / units.RECOVER_UNIT).read_text()
    assert "Conflicts=esp32cam-spin-on-run-v1-live-600-20261105.service" in text
    assert stat.S_IMODE((out / units.RECOVER_UNIT).stat().st_mode) == 0o400
    offline = tmp_path / "off"
    assert (
        cli.main(["units", "--kind", "offline", "--date", "20261105", "--out", str(offline)]) == 0
    )
    plan = json.loads((offline / "offline-systemd-run.json").read_text())
    assert plan["offline"][2] == "--unit=esp32cam-spin-on-run-v1-offline-20261105"
    assert plan["offline"][-3:] == [
        "offline-run",
        "/home/ubuntu/spin-on-run/config.json",
        "/home/ubuntu/spin-on-run/run",
    ]
    assert plan["post"][2] == "--unit=esp32cam-spin-on-run-v1-offline-post-20261105"
    with pytest.raises(FileExistsError):
        cli.main(["units", "--kind", "live", "--date", "20261105", "--out", str(out)])


def test_manifest_covers_every_package_file_and_the_field_map(tmp_path, capsys):
    binary = tmp_path / "frame-live"
    binary.write_bytes(b"elf")
    assert (
        cli.main(
            ["manifest", "--binary", f"frame-live={binary}", "--base-commit", "abc", "--issue", "7"]
        )
        == 0
    )
    built = json.loads(capsys.readouterr().out)
    on_disk = {p.name for p in PACKAGE.glob("*.py")}
    assert set(built["files"]) == on_disk, "every module must be frozen in the manifest"
    assert built["binaries"]["frame-live"] == common.sha256_bytes(b"elf")
    assert built["field_map"]["$.finite_live_gate_passed"] == "continuous_tracking_gate_passed"
    assert built["plan_sha256"]["plan.md"].startswith("abce502d")
    assert manifest.verify(built, PACKAGE, {"frame-live": binary}) == []
    binary.write_bytes(b"other")
    assert manifest.verify(built, PACKAGE, {"frame-live": binary}) == ["binary:frame-live"]


def test_parser_rejects_unknown_lengths_and_commands():
    for bad in (
        ["units", "--kind", "live", "--seconds", "60", "--date", "d", "--out", "o"],
        ["nope"],
        ["live", "c", "o", "restore"],
    ):
        with pytest.raises(SystemExit):
            cli.parser().parse_args(bad)


def test_package_has_no_ota_firmware_or_privilege_code():
    forbidden = re.compile(r"\bota\b|3232|esptool|\bsudo\b|espota|platformio", re.IGNORECASE)
    for path in PACKAGE.glob("*.py"):
        text = path.read_text()
        assert not forbidden.search(text), path.name
    assert "ssh" not in "".join(p.read_text() for p in PACKAGE.glob("*.py")).lower().replace(
        "flask", ""
    )


def test_system_ops_commands_are_fixed_argument_lists_and_keep_the_token_out():
    calls = []
    ops = system_ops.SystemOps(
        trial_unit="t",
        self_unit="g.service",
        base_url="http://x",
        admin_token="s3cret",
        run=lambda argv: calls.append(argv) or "disabled\n",
    )
    ops.stop_trial()
    ops.start_service()
    ops.disable_self()
    assert ops.is_enabled() == "disabled"
    assert calls[0] == ["systemctl", "--user", "stop", "t.service"]
    assert calls[1] == ["systemctl", "--user", "start", "esp32cam-server.service"]
    assert calls[2] == ["systemctl", "--user", "disable", "g.service"]
    assert "s3cret" not in json.dumps(calls)


def ctx(**kw):
    snap = {
        "camera_boot_id": preflight.CAMERA_BOOT,
        "unit_sha": common.HASHES["original_unit"],
        "dropin_paths": "",
        "service_active": True,
        "units_active": ["esp32cam-server.service"],
        "last_firmware_version": "fw-1",
        "pi_boot_id": "b",
        "governor": [],
        "cpus_allowed": "",
        "nice_policy": "",
        "mode": {
            "ready": True,
            "state": "active",
            "requested_mode": "eco",
            "effective_mode": "eco",
            "acknowledged": True,
            "actuation_state": "hold",
        },
    }
    base = {
        "snapshot": snap,
        "file_hashes": {"model": common.HASHES["model"]},
        "expected_hashes": {"model": common.HASHES["model"]},
        "free_bytes": 3 * 1024**3,
        "temp_millic": 50_000,
    }
    return preflight.Context(**{**base, **kw})


def test_preflight_passes_and_flags_each_class_of_problem():
    assert preflight.preflight(ctx())["ok"]
    bad_fw = ctx()
    bad_fw.snapshot["last_firmware_version"] = "unverified"
    assert preflight.preflight(bad_fw)["O07"]
    assert "hash:model" in preflight.preflight(ctx(file_hashes={"model": "0" * 64}))["O05"]
    assert (
        "fixed_hash:model"
        in preflight.preflight(
            ctx(file_hashes={"model": "0" * 64}, expected_hashes={"model": "0" * 64})
        )["O05"]
    )
    assert "free_space" in preflight.preflight(ctx(free_bytes=1))["O05"]
    assert "start_temperature" in preflight.preflight(ctx(temp_millic=70_001))["O05"]
    assert preflight.preflight(ctx(temp_millic=70_000))["ok"]
    changed = ctx()
    changed.snapshot["camera_boot_id"] = 5
    result = preflight.preflight(changed)
    assert result["ok"] and result["notes"]  # recorded, not fatal at preflight


def test_live_preflight_needs_the_guard_linger_and_no_standing_guard_conflict():
    ok = ctx(
        live=True,
        linger="yes",
        recover_unit_sha="x",
        recover_unit_expected_sha="x",
        recover_enabled=True,
        recover_check_exit=0,
        old_guard_conflict=False,
    )
    assert preflight.preflight(ok)["ok"]
    for patch, key, text in [
        ({"linger": "no"}, "O05", "linger_not_enabled"),
        ({"recover_enabled": False}, "O05", "recover_unit_not_enabled"),
        ({"recover_unit_sha": "y"}, "O05", "recover_unit_sha"),
        ({"recover_check_exit": 1}, "O05", "recover_check_failed"),
        ({"old_guard_conflict": None}, "O07", None),
        ({"free_bytes": 1024**3}, "O05", "free_space"),
    ]:
        built = ctx(
            live=True,
            linger="yes",
            recover_unit_sha="x",
            recover_unit_expected_sha="x",
            recover_enabled=True,
            recover_check_exit=0,
            old_guard_conflict=False,
        )
        for k, v in patch.items():
            setattr(built, k, v)
        got = preflight.preflight(built)
        assert not got["ok"] and (got[key] if text is None else text in got[key])
