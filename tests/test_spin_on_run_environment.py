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
                f"ActiveState={self.state['active']}\nSubState=running\n"
                f"MainPID={self.state.get('pid', 42)}\n"
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


TRIAL = "esp32cam-spin-on-run-v1-live-600-20261101.service"


def live_local(**run):
    """The trial's view: service stopped, so no process and no API at all."""

    def refused(name):
        raise OSError("the original service is stopped")

    return environment.Collector(
        run=FakeRun(units=f"{TRIAL} l a r\n", active="inactive", pid=0, **run),
        get_json=refused,
        unit_path=UNIT,
    ).snapshot_local()


def test_local_snapshot_needs_no_api_and_carries_no_api_keys():
    snap = live_local()
    assert not set(snap) & {"mode", "camera_boot_id", "last_firmware_version"}
    assert snap["service_active"] is False and snap["cpus_allowed"] == ""


def test_live_phase_judges_the_stopped_service_from_the_local_snapshot():
    base = collect()
    live = live_local()
    assert environment.deviations(base, live, "live", allowed_units=(TRIAL,)) == []
    assert "unexpected_unit:" + TRIAL in environment.deviations(base, live, "live")
    assert environment.deviations(base, live, "bogus")
    for run, expected in [
        ({"boot_id": "boot-2\n"}, "invariant:pi_boot_id"),
        ({"governor": "performance performance"}, "invariant:governor"),
        ({"sha": "0" * 64 + "  x\n"}, "unit_sha_not_original"),
        ({"dropin": "/x.conf"}, "dropin_present"),
    ]:
        found = environment.deviations(base, live_local(**run), "live", allowed_units=(TRIAL,))
        assert expected in found, run


def test_live_phase_rejects_the_original_service_running_in_the_trial():
    base = collect()
    running = collect(FakeRun(units=f"{TRIAL} l a r\n"))
    found = environment.deviations(base, running, "live", allowed_units=(TRIAL,))
    assert "service_active_in_trial" in found
    # Process properties are compared again once the service is up.
    other = collect(FakeRun(units=f"{TRIAL} l a r\n", governor="powersave powersave"))
    assert "invariant:governor" in environment.deviations(
        base, other, "live", allowed_units=(TRIAL,)
    )


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
    assert result["pass"] is True and result["reasons"] == []
    assert result["mode_recheck"]["rechecked"] is False
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


def test_system_ops_stops_the_original_service_once_and_checks_the_state():
    from scripts.spin_on_run import system_ops

    calls = []
    state = {"active": True}

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "stop" in argv:
            state["active"] = False
            return ""
        return "ActiveState=active\n" if state["active"] else "ActiveState=inactive\n"

    ops = system_ops.SystemOps(
        trial_unit="t", self_unit="r", base_url="http://x", admin_token=None, run=run
    )
    ops.stop_service()
    stops = [c for c in calls if "stop" in c[0]]
    assert len(stops) == 1
    assert stops[0][0] == ["systemctl", "--user", "stop", "esp32cam-server.service"]
    assert stops[0][1]["timeout"] == system_ops.STOP_TIMEOUT_S


def test_system_ops_stop_fails_when_the_service_is_still_active():
    from scripts.spin_on_run import system_ops

    ops = system_ops.SystemOps(
        trial_unit="t",
        self_unit="r",
        base_url="http://x",
        admin_token=None,
        run=lambda argv, **kw: "ActiveState=active\n" if "show" in argv else "",
    )
    with pytest.raises(RuntimeError):
        ops.stop_service()


class Replay:
    """Fake clock plus scripted snapshot reader: nothing here really sleeps."""

    def __init__(self, reads):
        self.reads, self.calls, self.now, self.slept = list(reads), 0, 1000.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def read(self):
        self.calls += 1
        return self.reads[min(self.calls, len(self.reads)) - 1]

    def run(self, base, phase="restored", first=None):
        return environment.deviations_with_recheck(
            base,
            first or collect(mode=PENDING),
            phase,
            self.read,
            sleep=self.sleep,
            clock=self.clock,
        )


PENDING = {"mode": {"state": "pending"}}


def pend():
    return collect(mode=PENDING)


def ok():
    return collect()


def test_pending_then_two_active_reads_pass_and_record_the_reads():
    replay = Replay([ok(), ok()])
    found, record = replay.run(collect())
    assert found == []
    assert record["rechecked"] and record["rereads"] == 2
    assert replay.slept == [2, 2] and record["pending_seconds"] == 2
    assert record["anomaly"] is None
    assert [r["state"] for r in record["reads"]] == ["pending", "active", "active"]
    assert set(record["reads"][0]) == {"t", *environment.RESTORED_MODE}


def test_ten_pending_reads_fail_with_the_unchanged_code():
    replay = Replay([pend()])
    found, record = replay.run(collect())
    assert found == ["mode_not_restored"] and replay.calls == 10
    assert record["rereads"] == 10 and record["pending_seconds"] == 20
    assert record["anomaly"] == "pending_over_15s"


@pytest.mark.parametrize(
    "change",
    [
        {"mode": {"effective_mode": "tracking"}},
        {"mode": {"acknowledged": False}},
        {"mode": {"boot_id": 99}},  # camera_boot_id (a FIXED_KEY)
    ],
)
def test_change_during_the_recheck_fails(change):
    replay = Replay([pend(), collect(mode=change)])
    found, record = replay.run(collect())
    assert found and replay.calls == 2 and record["rereads"] == 2


def test_fixed_key_change_during_the_recheck_fails():
    replay = Replay([ok(), collect(FakeRun(boot_id="boot-2\n"))])
    found, _ = replay.run(collect())
    assert "invariant:pi_boot_id" in found


@pytest.mark.parametrize("mode", [{"state": "degraded"}, {"state": "pending", "ready": 1}])
def test_no_recheck_unless_the_only_deviation_is_pending(mode):
    now = collect(mode={"mode": mode})
    if "ready" in mode:  # ready comes from /ready, not /mode
        now = collect(mode={"mode": {"state": "pending"}, "ready": {"status": "starting"}})
    replay = Replay([ok()])
    found, record = replay.run(collect(), first=now)
    assert "mode_not_restored" in found and replay.calls == 0 and not record["rechecked"]
    assert replay.slept == []


def test_live_phase_never_rechecks():
    replay = Replay([ok()])
    found, record = replay.run(collect(), phase="live", first=pend())
    assert replay.calls == 0 and not record["rechecked"] and found == environment.deviations(
        collect(), pend(), "live"
    )


def test_fall_back_to_pending_keeps_going_until_two_consecutive_matches():
    replay = Replay([ok(), pend(), ok(), ok()])
    assert replay.run(collect())[0] == [] and replay.calls == 4


def test_two_consecutive_matches_on_the_tenth_reread_pass_and_no_eleventh():
    replay = Replay([pend()] * 8 + [ok(), ok(), ok()])
    found, record = replay.run(collect())
    assert found == [] and replay.calls == 10 and record["rereads"] == 10
    replay = Replay([pend()] * 9 + [ok(), ok()])  # only one match fits in the 10
    found, _ = replay.run(collect())
    assert found == ["mode_not_restored"] and replay.calls == 10


def test_pending_over_fifteen_seconds_is_flagged_even_when_it_passes():
    replay = Replay([pend()] * 8 + [ok(), ok()])
    found, record = replay.run(collect())
    assert found == [] and record["anomaly"] == "pending_over_15s"


def test_the_recheck_runs_on_the_fake_clock_only():
    replay = Replay([pend()])
    replay.run(collect())
    assert replay.slept == [2] * 10 and replay.now == 1020.0
