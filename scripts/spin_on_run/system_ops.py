"""Real `Ops` for the recover guard and the guardian's readers (systemd user manager, HTTP).

Commands are fixed argument lists run without a shell. The admin token stays inside
the HTTP helpers and is never part of a command line or a return value. Routes
(checked against script/operating_routes.py and script/routes.py):
status `GET /api/cameras/<id>/operating`, mode `PUT` and resume `POST` on the same
path with the admin bearer token, readiness `GET /healthz/ready`, and the device
record (`last_firmware_version`) `GET /api/cameras/<id>/desired-config` (admin).
"""

from __future__ import annotations

import json
import time
import urllib.request

from . import environment


# The service may take a while to drain; the stop is attempted once and not retried.
STOP_TIMEOUT_S = 60.0


class SystemOps:
    def __init__(
        self,
        *,
        trial_unit: str,
        self_unit: str,
        base_url: str,
        admin_token: str | None,
        camera_id: str = "esp32cam-01",
        run=None,
    ):
        self.trial_unit = trial_unit
        self.self_unit = self_unit
        self.base_url = base_url.rstrip("/")
        self.token = admin_token
        self.camera_id = camera_id
        self.run = run or environment.run_read_only
        self.collector = None

    def _systemctl(self, *args: str, **kwargs) -> str:
        return self.run(["systemctl", "--user", *args], **kwargs)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def stop_trial(self) -> None:
        self._systemctl("stop", f"{self.trial_unit}.service")

    def stop_service(self) -> None:
        """The planned stop of the original service (plan 7 trial column): one `stop`, then
        one state read. No retry; the independent recover guard restarts it afterwards."""
        self._systemctl("stop", environment.SERVICE, timeout=STOP_TIMEOUT_S)
        state = self._systemctl("show", environment.SERVICE, "--property=ActiveState").strip()
        if state != "ActiveState=inactive":
            raise RuntimeError(f"{environment.SERVICE} not inactive after stop: {state}")

    def trial_state(self) -> dict:
        show = dict(
            line.split("=", 1)
            for line in self._systemctl(
                "show", f"{self.trial_unit}.service", "--property=ActiveState,MainPID,ControlGroup"
            ).splitlines()
            if "=" in line
        )
        group = show.get("ControlGroup", "")
        empty = True
        if group:
            try:
                with open(f"/sys/fs/cgroup{group}/cgroup.procs") as handle:
                    empty = handle.read().strip() == ""
            except FileNotFoundError:
                empty = True
        return {
            "active_state": show.get("ActiveState"),
            "main_pid": int(show.get("MainPID", "0") or 0),
            "cgroup_empty": empty,
        }

    def start_service(self) -> None:
        self._systemctl("start", environment.SERVICE)

    def _request(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base_url}/{path}", data=data, method=method)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        if data:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return json.loads(response.read(1 << 20) or b"{}")

    def ready(self) -> bool:
        try:
            return self._request("healthz/ready").get("status") == "ready"
        except OSError:
            return False

    def _operating(self) -> str:
        return f"api/cameras/{self.camera_id}/operating"

    def mode(self) -> dict:
        status = self._request(self._operating())
        status.setdefault("paused", (status.get("resources") or {}).get("paused"))
        return status

    def request_eco(self, mode: dict) -> None:
        version = mode.get("version")
        if (mode.get("resources") or {}).get("paused"):
            self._request(f"{self._operating()}/resume", "POST", {"expected_version": version})
        if mode.get("requested_mode") != "eco":
            self._request(self._operating(), "PUT", {"mode": "eco", "expected_version": version})

    def firmware_version(self) -> str:
        record = self._request(f"api/cameras/{self.camera_id}/desired-config")
        return record.get("last_firmware_version") or environment.UNVERIFIED

    def snapshot(self) -> dict:
        if self.collector is None:
            raise RuntimeError("collector not configured")
        return self.collector.snapshot()

    def disable_self(self) -> None:
        self._systemctl("disable", self.self_unit)

    def is_enabled(self) -> str:
        try:
            return self._systemctl("is-enabled", self.self_unit).strip()
        except Exception:  # noqa: BLE001 - `is-enabled` exits non-zero for "disabled"
            return "disabled"
