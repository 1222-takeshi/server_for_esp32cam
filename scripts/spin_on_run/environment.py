"""Read-only environment snapshots and the phase state table (plan sections 7, 10.2).

The collector only runs read-only commands and GETs. Credentials are read from
the environment of the caller and are never written to a snapshot.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable

from .common import HASHES

FIXED_KEYS = (
    "pi_boot_id",
    "unit_sha",
    "dropin_paths",
    "governor",
    "cpus_allowed",
    "nice_policy",
    "camera_boot_id",
    "last_firmware_version",
)
RESTORED_MODE = {
    "ready": True,
    "state": "active",
    "requested_mode": "eco",
    "effective_mode": "eco",
    "acknowledged": True,
    "actuation_state": "hold",
}
UNIT_GLOB = "esp32cam-*"
SERVICE = "esp32cam-server.service"
UNVERIFIED = "unverified"


def run_read_only(argv: list[str], timeout: float = 10.0) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)
    return done.stdout


def parse_active_units(listing: str) -> list[str]:
    """Names from `systemctl --user list-units --state=active --plain --no-legend`."""
    names = []
    for line in listing.splitlines():
        match = re.match(r"\s*(?:[●*]\s*)?(esp32cam-[A-Za-z0-9_.@:-]+\.(?:service|timer))\b", line)
        if match:
            names.append(match.group(1))
    return sorted(names)


class Collector:
    """Gathers one snapshot. Every external effect goes through the injected callables."""

    def __init__(
        self,
        *,
        run: Callable[[list[str]], str] = run_read_only,
        get_json: Callable[[str], dict],
        unit_path: str,
        service_pid: Callable[[str], int] | None = None,
    ):
        self.run = run
        self.get_json = get_json
        self.unit_path = unit_path
        self.service_pid = service_pid

    def snapshot(self) -> dict:
        show = dict(
            line.split("=", 1)
            for line in self.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    SERVICE,
                    "--property=ActiveState,SubState,MainPID,DropInPaths",
                ]
            ).splitlines()
            if "=" in line
        )
        pid = int(show.get("MainPID", "0") or 0)
        sha = self.run(["/usr/bin/sha256sum", self.unit_path]).split()[0]
        governors = self.run(
            ["sh", "-c", "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"]
        ).split()
        allowed = (
            self.run(["grep", "Cpus_allowed_list", f"/proc/{pid}/status"]).strip() if pid else ""
        )
        nice = self.run(["ps", "-o", "pid,ni,cls,rtprio", "-p", str(pid)]).strip() if pid else ""
        units = parse_active_units(
            self.run(
                [
                    "systemctl",
                    "--user",
                    "list-units",
                    "--state=active",
                    "--plain",
                    "--no-legend",
                    UNIT_GLOB,
                ]
            )
        )
        mode = self.get_json("mode")
        ready = self.get_json("ready")
        device = self.get_json("device")
        return {
            "pi_boot_id": self.run(["cat", "/proc/sys/kernel/random/boot_id"]).strip(),
            "unit_sha": sha,
            "dropin_paths": show.get("DropInPaths", ""),
            "service_active": show.get("ActiveState") == "active",
            "units_active": units,
            "governor": governors,
            "cpus_allowed": allowed,
            "nice_policy": nice.split("\n", 1)[-1] if nice else "",
            "mode": {
                "ready": ready.get("status") == "ready",
                "state": mode.get("state"),
                "requested_mode": mode.get("requested_mode"),
                "effective_mode": mode.get("effective_mode"),
                "acknowledged": mode.get("acknowledged"),
                "actuation_state": mode.get("actuation_state"),
            },
            "camera_boot_id": mode.get("boot_id"),
            "last_firmware_version": device.get("last_firmware_version", UNVERIFIED),
        }


def http_getter(
    base: str, admin_token: str | None, camera_id: str = "esp32cam-01", timeout: float = 5.0
) -> Callable[[str], dict]:
    """GET helper for the three reads a snapshot needs; the token is sent, never returned."""
    paths = {
        "mode": f"api/cameras/{camera_id}/operating",
        "ready": "healthz/ready",
        "device": f"api/cameras/{camera_id}/desired-config",
    }

    def get(name: str) -> dict:
        request = urllib.request.Request(f"{base.rstrip('/')}/{paths[name]}")
        if admin_token and name == "device":
            request.add_header("Authorization", f"Bearer {admin_token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read(1 << 20))
        except urllib.error.HTTPError as error:
            if name == "ready" and error.code == 503:  # degraded still carries a status body
                return json.loads(error.read(1 << 20))
            raise

    return get


def deviations(
    baseline: dict,
    current: dict,
    phase: str,
    *,
    allowed_units: tuple[str, ...] = (),
) -> list[str]:
    """Differences from the phase's expected state; empty means no deviation.

    phase: "offline" and "restored" expect the original service and mode; "live"
    allows the planned Python stop, Tracking and the named trial units only.
    Every phase requires the immutable keys to match the baseline.
    """
    found = [f"invariant:{key}" for key in FIXED_KEYS if baseline.get(key) != current.get(key)]
    if current.get("unit_sha") != HASHES["original_unit"]:
        found.append("unit_sha_not_original")
    if current.get("dropin_paths"):
        found.append("dropin_present")
    extra = [u for u in current.get("units_active", []) if u not in allowed_units]
    if phase in {"offline", "restored", "preflight"}:
        # The original service is not a trial unit; it is named by SERVICE below.
        extra = [u for u in extra if u != SERVICE]
        if not current.get("service_active"):
            found.append("service_inactive")
        mode = current.get("mode", {})
        wanted = RESTORED_MODE
        if any(mode.get(key) != value for key, value in wanted.items()):
            found.append("mode_not_restored")
    elif phase == "live":
        extra = [u for u in extra if u != SERVICE]
    else:
        found.append(f"unknown_phase:{phase}")
    found += [f"unexpected_unit:{u}" for u in extra]
    return found
