import json
import stat

import pytest

from scripts.spin_on_run import common, environment, post_runner

UNIT = "/home/ubuntu/.config/systemd/user/esp32cam-server.service"


class FakeRun:
    def __init__(self, **overrides):
        self.state = {
            "boot_id": "boot-1\n",
            "sha": f"{common.HASHES['original_unit']}  {UNIT}\n",
            "units": "esp32cam-server.service loaded active running x\n",
            "dropin": "",
            "active": "active",
            "governor": "ondemand ondemand",
            **overrides,
        }

    def __call__(self, argv):
        text = " ".join(argv)
        if "show" in argv:
            return (
                f"ActiveState={self.state['active']}\nSubState=running\nMainPID=42\n"
                f"DropInPaths={self.state['dropin']}\n"
            )
        if "sha256sum" in text:
            return self.state["sha"]
        if "scaling_governor" in text:
            return self.state["governor"]
        if "Cpus_allowed_list" in text:
            return "Cpus_allowed_list: 0-3"
        if argv[0] == "ps":
            return "PID NI CLS RTPRIO\n42 0 TS -"
        if "list-units" in argv:
            return self.state["units"]
        if argv[0] == "cat":
            return self.state["boot_id"]
        raise AssertionError(text)


def get_json(overrides=None):
    data = {
        "mode": {
            "state": "active",
            "requested_mode": "eco",
            "effective_mode": "eco",
            "acknowledged": True,
            "actuation_state": "hold",
            "boot_id": 1817,
        },
        "ready": {"status": "ready"},
        "device": {"last_firmware_version": "fw-1"},
    }
    for key, value in (overrides or {}).items():
        data[key] = {**data[key], **value}
    return lambda name: data[name]


def collect(run=None, mode=None):
    return environment.Collector(
        run=run or FakeRun(), get_json=get_json(mode), unit_path=UNIT
    ).snapshot()


def test_active_unit_parser_accepts_only_project_units():
    text = (
        "  esp32cam-server.service loaded active running a\n"
        "● esp32cam-spin-on-run-v1-offline-20261101.service loaded active running b\n"
        "dbus.service loaded active running c\n"
        "not-esp32cam-x.service loaded active running d\n"
    )
    assert environment.parse_active_units(text) == [
        "esp32cam-server.service",
        "esp32cam-spin-on-run-v1-offline-20261101.service",
    ]


def test_snapshot_has_no_credentials_and_matches_itself():
    snap = collect()
    assert snap["service_active"] and snap["camera_boot_id"] == 1817
    assert environment.deviations(snap, snap, "offline") == []
    assert "key" not in json.dumps(snap).lower()


@pytest.mark.parametrize(
    ("run", "mode", "expected"),
    [
        ({"boot_id": "boot-2\n"}, None, "invariant:pi_boot_id"),
        ({"governor": "performance performance"}, None, "invariant:governor"),
        ({"sha": "0" * 64 + "  x\n"}, None, "unit_sha_not_original"),
        ({"dropin": "/x.conf"}, None, "dropin_present"),
        ({"active": "inactive"}, None, "service_inactive"),
        (
            {"units": "esp32cam-server.service l a r\nesp32cam-other.service l a r\n"},
            None,
            "unexpected_unit:esp32cam-other.service",
        ),
        ({}, {"mode": {"requested_mode": "tracking"}}, "mode_not_restored"),
        ({}, {"mode": {"acknowledged": False}}, "mode_not_restored"),
        ({}, {"mode": {"boot_id": 99}}, "invariant:camera_boot_id"),
        ({}, {"device": {"last_firmware_version": "fw-2"}}, "invariant:last_firmware_version"),
    ],
)
def test_each_deviation_from_the_baseline_is_reported(run, mode, expected):
    base = collect()
    now = collect(FakeRun(**run), mode)
    assert expected in environment.deviations(base, now, "restored")


def test_self_unit_is_allowed_only_by_exact_name():
    base = collect()
    own = "esp32cam-spin-on-run-v1-offline-post-20261101.service"
    run = FakeRun(units=f"esp32cam-server.service l a r\n{own} l a r\n")
    now = collect(run)
    assert environment.deviations(base, now, "restored", allowed_units=(own,)) == []
    assert environment.deviations(base, now, "restored", allowed_units=(own + "x",))
    assert environment.deviations(base, now, "restored")


def test_live_phase_allows_the_planned_stop_and_tracking():
    base = collect()
    trial = "esp32cam-spin-on-run-v1-live-600-20261101.service"
    run = FakeRun(units=f"{trial} l a r\n", active="inactive")
    live = collect(run, {"mode": {"requested_mode": "tracking"}})
    assert environment.deviations(base, live, "live", allowed_units=(trial,)) == []
    assert "unexpected_unit:" + trial in environment.deviations(base, live, "live")
    assert environment.deviations(base, live, "bogus")


def write_baseline(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return out, collect()


def prepare_result(out, codes=(), verdict=("V6", "improvement")):
    common.write_json(
        out / "result.json",
        {"codes": list(codes), "verdict": {"id": verdict[0], "result": verdict[1]}},
    )
    common.write_new(out / "marker", "completed\n")


def test_post_run_passes_in_a_restored_environment(tmp_path):
    out, base = write_baseline(tmp_path)
    prepare_result(out)
    collector = environment.Collector(run=FakeRun(), get_json=get_json(), unit_path=UNIT)
    result = post_runner.run_post(
        out, collector, base, offline_unit="esp32cam-off.service", post_unit="esp32cam-post.service"
    )
    assert result == {"pass": True, "reasons": []}
    assert stat.S_IMODE((out / "after.json").stat().st_mode) == 0o400
    assert post_runner.finalize_post(out, "success") is None
    final = post_runner.final_check(out, collector, base)
    assert final["codes"] == [] and final["verdict"]["id"] == "V6"


def test_offline_unit_still_active_fails_the_post_run(tmp_path):
    out, base = write_baseline(tmp_path)
    prepare_result(out)
    run = FakeRun(units="esp32cam-server.service l a r\nesp32cam-off.service l a r\n")
    collector = environment.Collector(run=run, get_json=get_json(), unit_path=UNIT)
    result = post_runner.run_post(
        out, collector, base, offline_unit="esp32cam-off.service", post_unit="esp32cam-post.service"
    )
    assert not result["pass"] and "offline_unit_active" in result["reasons"]
    assert post_runner.finalize_post(out, "success") == "failed"


@pytest.mark.parametrize(
    ("service_result", "reason"),
    [("timeout", "timeout"), ("exit-code", "exit-code"), ("signal", "exit-code")],
)
def test_finalize_records_a_failed_or_timed_out_post_unit(tmp_path, service_result, reason):
    out, _ = write_baseline(tmp_path)
    assert post_runner.finalize_post(out, service_result) == reason
    assert json.loads((out / "post-o08.json").read_text()) == {"reason": reason}
    assert post_runner.finalize_post(out, service_result) == reason  # idempotent


def test_finalize_without_a_post_result_is_no_result(tmp_path):
    out, _ = write_baseline(tmp_path)
    assert post_runner.finalize_post(out, "success") == "no-result"


def test_final_check_settles_o08_when_neither_post_step_ran(tmp_path):
    out, base = write_baseline(tmp_path)
    prepare_result(out)
    collector = environment.Collector(run=FakeRun(), get_json=get_json(), unit_path=UNIT)
    final = post_runner.final_check(out, collector, base)
    assert "O08" in final["codes"] and final["verdict"]["id"] == "V1"
    assert "post-result.json missing" in final["notes"]


def test_o08_outranks_a_quality_mismatch_and_a_missing_marker_is_o06(tmp_path):
    out, base = write_baseline(tmp_path)
    common.write_json(
        out / "result.json",
        {"codes": ["O01"], "verdict": {"id": "V2", "result": "worse_quality_mismatch"}},
    )
    collector = environment.Collector(run=FakeRun(), get_json=get_json(), unit_path=UNIT)
    final = post_runner.final_check(out, collector, base)
    assert final["codes"] == ["O01", "O06", "O08"] and final["verdict"]["id"] == "V1"


def test_final_check_rejects_a_leftover_unit_without_exception(tmp_path):
    out, base = write_baseline(tmp_path)
    prepare_result(out)
    good = environment.Collector(run=FakeRun(), get_json=get_json(), unit_path=UNIT)
    post_runner.run_post(out, good, base, offline_unit="a", post_unit="esp32cam-post.service")
    run = FakeRun(units="esp32cam-server.service l a r\nesp32cam-post.service l a r\n")
    leftover = environment.Collector(run=run, get_json=get_json(), unit_path=UNIT)
    final = post_runner.final_check(out, leftover, base)
    assert "O08" in final["codes"]
    assert "unexpected_unit:esp32cam-post.service" in final["notes"]
