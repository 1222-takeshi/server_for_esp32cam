"""Generators for the transient units, the recover unit and the launch record (plan 8.2, 10.1).

Everything returns plain data or text; nothing here starts a unit. Credential
values are never part of any returned structure that is written to disk.
"""

from __future__ import annotations

import hashlib
import json

from .common import sha256_bytes

PYTHON = "/usr/bin/python3"
LIVE_PREFIX = "esp32cam-spin-on-run-v1-live"
OFFLINE_PREFIX = "esp32cam-spin-on-run-v1-offline"
THREAD_PREFIX = "esp32cam-thread-cpu-offline-v1"
RECOVER_UNIT = f"{LIVE_PREFIX}-recover.service"
RUNTIME_MAX = {600: 900, 3600: 4200}
LIVE_ENV_KEYS = {
    "PATH",
    "ESP32CAM_RUST_TRIAL_NONCE",
    "ESP32CAM_RUST_TRIAL_EXPECTED_BOOT",
    "ESP32CAM_RUST_TRIAL_SECONDS",
    "ESP32CAM_RUST_TRIAL_ADAPTIVE_RESERVE",
    "ESP32CAM_RUST_TRIAL_AUTO_RECOVERY",
    "ESP32CAM_RUST_TRIAL_SPIN_ON_RUN",
}
CREDENTIAL_KEYS = {
    "ESP32CAM_DEVICE_API_KEYS",
    "ESP32CAM_DEVICE_API_KEY",
    "ESP32CAM_SHARED_KEY_DEVICE_ID",
}


def trial_unit(seconds: int, date: str) -> str:
    if seconds not in RUNTIME_MAX:
        raise ValueError("trial length must be 600 or 3600 seconds")
    return f"{LIVE_PREFIX}-{seconds}-{date}"


def live_env(
    *, nonce: str, camera_boot: int, seconds: int, credentials: dict[str, str]
) -> dict[str, str]:
    """The closed environment set. Hold injection is never part of it."""
    if len(nonce) != 16 or any(c not in "0123456789abcdef" for c in nonce):
        raise ValueError("nonce must be 16 lowercase hex characters")
    if seconds not in RUNTIME_MAX or camera_boot <= 0:
        raise ValueError("invalid trial parameters")
    keys = set(credentials)
    shared = "ESP32CAM_DEVICE_API_KEY" in keys
    if not keys <= CREDENTIAL_KEYS or not (
        "ESP32CAM_DEVICE_API_KEYS" in keys or (shared and "ESP32CAM_SHARED_KEY_DEVICE_ID" in keys)
    ):
        raise ValueError("credentials do not match the accepted key configuration")
    env = {
        "PATH": "/usr/bin:/bin",
        "ESP32CAM_RUST_TRIAL_NONCE": nonce,
        "ESP32CAM_RUST_TRIAL_EXPECTED_BOOT": str(camera_boot),
        "ESP32CAM_RUST_TRIAL_SECONDS": str(seconds),
        "ESP32CAM_RUST_TRIAL_ADAPTIVE_RESERVE": "1",
        "ESP32CAM_RUST_TRIAL_AUTO_RECOVERY": "1",
        "ESP32CAM_RUST_TRIAL_SPIN_ON_RUN": "1",
    }
    return {**env, **credentials}


def validate_live_env(env: dict[str, str]) -> None:
    extra = set(env) - LIVE_ENV_KEYS - CREDENTIAL_KEYS
    if extra or "ESP32CAM_RUST_TRIAL_INJECT_HOLD" in env:
        raise ValueError(f"environment outside the frozen set: {sorted(extra)}")
    if env.get("ESP32CAM_RUST_TRIAL_SPIN_ON_RUN") != "1":
        raise ValueError("the live trial must select run-only spinning")


def native_argv(binary: str, model: str, runtime: str) -> list[str]:
    return [binary, model, runtime, "0.0.0.0:8766", "esp32cam-01"]


def live_systemd_run(unit: str, seconds: int, guardian: str, run_dir: str) -> list[str]:
    return [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        f"--property=RuntimeMaxSec={RUNTIME_MAX[seconds]}",
        "--property=TimeoutStopSec=5",
        "--property=KillMode=control-group",
        "--property=LimitFSIZE=16777216",
        "--property=StandardOutput=null",
        "--property=StandardError=null",
        f"--property=OnFailure={RECOVER_UNIT}",
        f"--property=ExecStopPost={PYTHON} {guardian} enqueue {run_dir}",
        PYTHON,
        guardian,
        "execute",
        run_dir,
    ]


def offline_systemd_run(
    unit: str, post_unit: str, runner: str, post: str, out_dir: str
) -> list[str]:
    start_post = (
        f"{PYTHON} {post} launch {out_dir} {post_unit}"  # starts the follow-up unit, no blocking
    )
    return [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "--property=RuntimeMaxSec=1500",
        "--property=TimeoutStopSec=30",
        "--property=KillMode=control-group",
        f"--property=ExecStopPost={start_post}",
        PYTHON,
        runner,
        "run",
        out_dir,
    ]


def post_systemd_run(post_unit: str, offline_unit: str, post: str, out_dir: str) -> list[str]:
    """The follow-up unit: oneshot, ordered after the offline unit, 300 s start timeout."""
    return [
        "systemd-run",
        "--user",
        f"--unit={post_unit}",
        "--property=Type=oneshot",
        f"--property=After={offline_unit}.service",
        "--property=TimeoutStartSec=300",
        "--property=TimeoutStopSec=30",
        f"--property=ExecStopPost={PYTHON} {post} finalize {out_dir}",
        "--no-block",
        PYTHON,
        post,
        "run",
        out_dir,
    ]


def recover_unit_text(trial: str, guardian: str, run_dir: str) -> str:
    """Persistent oneshot that completes the restoration after any trial end."""
    return (
        "[Unit]\n"
        f"Description=Restore the original service after {trial}\n"
        f"Conflicts={trial}.service\n"
        f"After={trial}.service esp32cam-server.service\n"
        "StartLimitIntervalSec=0\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "TimeoutStartSec=1260\n"
        "Restart=on-failure\n"
        "RestartSec=15\n"
        "RestartPreventExitStatus=75\n"
        f"ExecStart={PYTHON} {guardian} recover {run_dir}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def launch_request(env: dict[str, str], argv: list[str], systemd_run: list[str]) -> dict:
    """The record written before launch. Credential values are redacted, names kept."""
    shown = {k: ("<redacted>" if k in CREDENTIAL_KEYS else v) for k, v in sorted(env.items())}
    body = {"argv": argv, "env": shown, "unit_properties": systemd_run}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return {
        **body,
        "env_sha256": sha256_bytes(json.dumps(shown, sort_keys=True).encode()),
        "argv_sha256": hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
        "request_sha256": sha256_bytes(canonical),
    }
