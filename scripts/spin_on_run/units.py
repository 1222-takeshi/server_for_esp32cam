"""Generators for the transient units, the recover unit and the launch record (plan 8.2, 10.1).

Everything returns plain data or text; nothing here starts a unit. Credential
values are never part of any returned structure that is written to disk.
"""

from __future__ import annotations

import hashlib
import json
import os

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
    "ESP32CAM_RUST_TRIAL_FRAME_LOG",
}
# v2 spec D7: R3 writes every frame record once, at the end, to this file of the stage dir.
FRAME_LOG_KEY = "ESP32CAM_RUST_TRIAL_FRAME_LOG"
FRAME_LOG_NAME = "frames.jsonl"
CREDENTIAL_KEYS = {
    "ESP32CAM_DEVICE_API_KEYS",
    "ESP32CAM_DEVICE_API_KEY",
    "ESP32CAM_SHARED_KEY_DEVICE_ID",
}


def trial_unit(seconds: int, date: str) -> str:
    if seconds not in RUNTIME_MAX:
        raise ValueError("trial length must be 600 or 3600 seconds")
    return f"{LIVE_PREFIX}-{seconds}-{date}"


def frame_log_path(run_dir: str) -> str:
    """Absolute path of the R3 frame log inside the stage (evidence) directory."""
    parts = str(run_dir).split(os.sep)
    if not os.path.isabs(run_dir) or ".." in parts or "." in parts[1:]:
        raise ValueError("the stage directory must be an absolute path without dot segments")
    return os.path.join(os.path.normpath(run_dir), FRAME_LOG_NAME)


def live_env(
    *,
    nonce: str,
    camera_boot: int,
    seconds: int,
    credentials: dict[str, str],
    run_dir: str | None = None,
) -> dict[str, str]:
    """The closed environment set. Hold injection is never part of it.

    With `run_dir` (the v2 live start) R3 frame logging is switched on and writes
    `<run_dir>/frames.jsonl`; without it the variable is absent, as in v1.
    """
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
    if run_dir is not None:
        env[FRAME_LOG_KEY] = frame_log_path(run_dir)
    return {**env, **credentials}


def validate_live_env(env: dict[str, str]) -> None:
    extra = set(env) - LIVE_ENV_KEYS - CREDENTIAL_KEYS
    if extra or "ESP32CAM_RUST_TRIAL_INJECT_HOLD" in env:
        raise ValueError(f"environment outside the frozen set: {sorted(extra)}")
    if env.get("ESP32CAM_RUST_TRIAL_SPIN_ON_RUN") != "1":
        raise ValueError("the live trial must select run-only spinning")
    if FRAME_LOG_KEY in env:
        path = env[FRAME_LOG_KEY]
        if (
            path != frame_log_path(os.path.dirname(path))
            or os.path.basename(path) != FRAME_LOG_NAME
        ):
            raise ValueError("the frame log must be an absolute <stage dir>/frames.jsonl")


def check_frame_log(env: dict[str, str], run_dir: str) -> None:
    """The v2 live start must write the frame log into this very stage directory."""
    if env.get(FRAME_LOG_KEY) != frame_log_path(run_dir):
        raise ValueError("ESP32CAM_RUST_TRIAL_FRAME_LOG is not <run dir>/frames.jsonl")


def native_argv(binary: str, model: str, runtime: str) -> list[str]:
    return [binary, model, runtime, "0.0.0.0:8766", "esp32cam-01"]


def cli_command(root: str, *args: str) -> list[str]:
    """The entry point every unit runs, from the deployment root so `scripts` imports."""
    return [PYTHON, "-m", "scripts.spin_on_run.cli", *args]


def _exec(root: str, *args: str) -> str:
    return " ".join(cli_command(root, *args))


def live_systemd_run(unit: str, seconds: int, root: str, config: str, run_dir: str) -> list[str]:
    return [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        f"--property=WorkingDirectory={root}",
        f"--property=RuntimeMaxSec={RUNTIME_MAX[seconds]}",
        "--property=TimeoutStopSec=5",
        "--property=KillMode=control-group",
        "--property=LimitFSIZE=16777216",
        "--property=StandardOutput=null",
        "--property=StandardError=null",
        f"--property=OnFailure={RECOVER_UNIT}",
        f"--property=ExecStopPost={_exec(root, 'live', config, run_dir, 'enqueue')}",
        *cli_command(root, "live", config, run_dir, "execute"),
    ]


def offline_systemd_run(
    unit: str, root: str, config: str, out_dir: str, *, thread: bool, runtime_max_s: int | None = None
) -> list[str]:
    """The offline unit; its stop starts the follow-up unit without blocking."""
    runtime_max = runtime_max_s if runtime_max_s is not None else (900 if thread else 1500)
    return [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        f"--property=WorkingDirectory={root}",
        f"--property=RuntimeMaxSec={runtime_max}",
        "--property=TimeoutStopSec=30",
        "--property=KillMode=control-group",
        f"--property=ExecStopPost={_exec(root, 'offline-post', config, out_dir, 'launch')}",
        *cli_command(root, "offline-run", config, out_dir, *(["--thread"] if thread else [])),
    ]


def post_systemd_run(
    post_unit: str, offline_unit: str, root: str, config: str, out_dir: str
) -> list[str]:
    """The follow-up unit: oneshot, ordered after the offline unit, 300 s start timeout."""
    return [
        "systemd-run",
        "--user",
        f"--unit={post_unit}",
        f"--property=WorkingDirectory={root}",
        "--property=Type=oneshot",
        f"--property=After={offline_unit}.service",
        "--property=TimeoutStartSec=300",
        "--property=TimeoutStopSec=30",
        f"--property=ExecStopPost={_exec(root, 'offline-post', config, out_dir, 'finalize')}",
        "--no-block",
        *cli_command(root, "offline-post", config, out_dir, "run"),
    ]


def recover_unit_text(trial: str, root: str, config: str, run_dir: str) -> str:
    """Persistent oneshot that completes the restoration after any trial end."""
    return (
        "[Unit]\n"
        f"Description=Restore the original service after {trial}\n"
        f"Conflicts={trial}.service\n"
        f"After={trial}.service esp32cam-server.service\n"
        "StartLimitIntervalSec=0\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={root}\n"
        "TimeoutStartSec=1260\n"
        "Restart=on-failure\n"
        "RestartSec=15\n"
        "RestartPreventExitStatus=75\n"
        f"ExecStart={_exec(root, 'live', config, run_dir, 'recover')}\n\n"
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
