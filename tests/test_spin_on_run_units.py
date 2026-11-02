import json

import pytest

from scripts.spin_on_run import units

CREDS = {"ESP32CAM_DEVICE_API_KEYS": '{"esp32cam-01":"secret-value"}'}


def env(**kw):
    return (
        units.live_env(
            nonce="0123456789abcdef",
            camera_boot=18174604300954271633,
            seconds=600,
            credentials=CREDS,
        )
        | kw
    )


def test_live_env_is_the_frozen_set_with_the_single_difference():
    e = env()
    units.validate_live_env(e)
    assert e["ESP32CAM_RUST_TRIAL_SPIN_ON_RUN"] == "1"
    assert (
        e["ESP32CAM_RUST_TRIAL_AUTO_RECOVERY"] == "1"
        and e["ESP32CAM_RUST_TRIAL_ADAPTIVE_RESERVE"] == "1"
    )
    assert "ESP32CAM_RUST_TRIAL_INJECT_HOLD" not in e
    for forbidden in ("QUICK_ACK", "SMALL_MSS", "ISOLATE_IO", "DYNAMIC_BLOCKS", "RAW_CLOCK"):
        assert not any(forbidden in key for key in e)


@pytest.mark.parametrize(
    "bad",
    [
        {"ESP32CAM_RUST_TRIAL_INJECT_HOLD": "1"},
        {"ESP32CAM_RUST_TRIAL_QUICK_ACK": "1"},
        {"LD_PRELOAD": "x"},
        {"ESP32CAM_RUST_TRIAL_SPIN_ON_RUN": "0"},
    ],
)
def test_validate_rejects_anything_outside_the_frozen_set(bad):
    with pytest.raises(ValueError):
        units.validate_live_env(env(**bad))


def test_live_env_input_validation():
    base = {"nonce": "0123456789abcdef", "camera_boot": 1, "seconds": 600, "credentials": CREDS}
    for patch in (
        {"nonce": "xyz"},
        {"nonce": "0123456789ABCDEF"},
        {"seconds": 300},
        {"camera_boot": 0},
        {"credentials": {}},
        {"credentials": {"ESP32CAM_DEVICE_API_KEY": "k"}},
        {"credentials": {"OTHER": "k"}},
    ):
        with pytest.raises(ValueError):
            units.live_env(**{**base, **patch})
    shared = {"ESP32CAM_DEVICE_API_KEY": "k", "ESP32CAM_SHARED_KEY_DEVICE_ID": "esp32cam-01"}
    assert units.live_env(**{**base, "credentials": shared})


def test_systemd_run_arguments_match_the_plan():
    name = units.trial_unit(600, "20261105")
    assert name == "esp32cam-spin-on-run-v1-live-600-20261105"
    args = units.live_systemd_run(name, 600, "/root", "/root/c.json", "/run/x")
    for expected in (
        "--property=RuntimeMaxSec=900",
        "--property=TimeoutStopSec=5",
        "--property=KillMode=control-group",
        "--property=LimitFSIZE=16777216",
        "--property=StandardOutput=null",
        "--property=StandardError=null",
        "--property=OnFailure=esp32cam-spin-on-run-v1-live-recover.service",
        "--property=WorkingDirectory=/root",
        "--property=ExecStopPost=/usr/bin/python3 -m scripts.spin_on_run.cli live /root/c.json /run/x enqueue",
    ):
        assert expected in args
    assert args[-6:] == [
        "-m",
        "scripts.spin_on_run.cli",
        "live",
        "/root/c.json",
        "/run/x",
        "execute",
    ]
    assert "RuntimeMaxSec=4200" in " ".join(units.live_systemd_run("u", 3600, "r", "c", "d"))
    with pytest.raises(ValueError):
        units.trial_unit(60, "d")


def test_recover_unit_has_the_frozen_directives():
    text = units.recover_unit_text("trial-x", "/root", "/root/c.json", "/run/x")
    for line in (
        "Conflicts=trial-x.service",
        "After=trial-x.service esp32cam-server.service",
        "StartLimitIntervalSec=0",
        "TimeoutStartSec=1260",
        "Restart=on-failure",
        "RestartSec=15",
        "RestartPreventExitStatus=75",
        "WantedBy=default.target",
        "Type=oneshot",
        "ExecStart=/usr/bin/python3 -m scripts.spin_on_run.cli live /root/c.json /run/x recover",
    ):
        assert line in text.splitlines()


def test_offline_post_unit_is_a_300_second_oneshot_after_the_offline_unit():
    args = units.post_systemd_run("p", "o", "/root", "/c.json", "/out")
    assert "--property=Type=oneshot" in args and "--property=After=o.service" in args
    assert "--property=TimeoutStartSec=300" in args
    assert not any("RuntimeMaxSec" in a for a in args)
    assert any(a.startswith("--property=ExecStopPost=") and "finalize" in a for a in args)
    offline = units.offline_systemd_run("o", "/root", "/c.json", "/out", thread=False)
    assert "--property=RuntimeMaxSec=1500" in offline
    assert offline[-3:] == ["offline-run", "/c.json", "/out"]
    assert any("offline-post /c.json /out launch" in a for a in offline)
    assert "--property=RuntimeMaxSec=900" in units.offline_systemd_run(
        "m", "/r", "/c", "/o", thread=True
    )


def test_launch_request_never_contains_credential_values():
    e = env()
    request = units.launch_request(e, units.native_argv("/bin/frame-live", "m", "r"), ["x"])
    text = json.dumps(request)
    assert "secret-value" not in text and "<redacted>" in text
    assert request["env"]["ESP32CAM_RUST_TRIAL_SPIN_ON_RUN"] == "1"
    other = units.launch_request(env(ESP32CAM_RUST_TRIAL_SECONDS="3600"), ["a"], ["x"])
    assert other["request_sha256"] != request["request_sha256"]
    assert units.native_argv("b", "m", "r")[-2:] == ["0.0.0.0:8766", "esp32cam-01"]
