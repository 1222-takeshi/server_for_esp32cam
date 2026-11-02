"""Independent restore guard: R1 to R7 and the post-run check (plan section 10).

Runs from a persistent oneshot unit after the trial unit stopped, however it
stopped. All system effects go through `Ops`, so the sequence, the deadlines
and the retry rules are testable without a device. Only the original service
is restarted and only Eco is requested; nothing here updates firmware.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import common, environment, live_checker

R1_S, PR_S, R2_S, R34_S, R5_S, R6_S, R7_S = 15, 30, 45, 130, 30, 130, 15
OBSERVE_SAMPLES = 13
OBSERVE_INTERVAL_S = 10
ATTEMPT_GAP_S = 15
MAX_ATTEMPTS = 3
EXIT_FAILED = 75
SELF_UNIT = "esp32cam-spin-on-run-v1-live-recover.service"


class Ops(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
    def stop_trial(self) -> None: ...
    def trial_state(self) -> dict: ...  # active_state, main_pid, cgroup_empty
    def start_service(self) -> None: ...
    def ready(self) -> bool: ...
    def mode(self) -> dict: ...
    def request_eco(self, mode: dict) -> None: ...
    def snapshot(self) -> dict: ...
    def disable_self(self) -> None: ...
    def is_enabled(self) -> str: ...


@dataclass
class Result:
    ok: bool
    detail: str = ""


def eco_reached(mode: dict) -> bool:
    return (
        mode.get("state") == "active"
        and mode.get("requested_mode") == mode.get("effective_mode") == "eco"
        and mode.get("acknowledged") is True
        and mode.get("actuation_state") == "hold"
        and mode.get("paused") is False
    )


class Guard:
    def __init__(self, ops: Ops, run_dir: Path, baseline: dict, seconds: int):
        self.ops, self.run_dir, self.baseline, self.seconds = ops, run_dir, baseline, seconds
        self.post: dict = {}
        self.attempts: list[dict] = []

    # -- individual steps ----------------------------------------------------
    def within(self, started: float, limit: float) -> bool:
        return self.ops.monotonic() - started <= limit

    def r1(self) -> Result:
        started = self.ops.monotonic()
        self.ops.stop_trial()
        state = self.ops.trial_state()
        stopped = (
            state.get("active_state") in {"inactive", "failed"}
            and state.get("main_pid") == 0
            and state.get("cgroup_empty") is True
        )
        return Result(stopped and self.within(started, R1_S), f"{state}")

    def post_run(self) -> dict:
        """Run once after the first successful R1; the only place the report is read."""
        native = self.run_dir / "native.json"
        report, parse_error = None, None
        raw = native.read_bytes() if native.exists() else b""
        try:
            report = json.loads(raw) if raw else None
        except ValueError as error:
            parse_error = type(error).__name__
        for name in ("native.json", "native.stderr"):
            path = self.run_dir / name
            if path.exists():
                path.chmod(0o400)
        marker = (
            (self.run_dir / "marker").read_text().strip()
            if (self.run_dir / "marker").exists()
            else None
        )
        stop_info = _load(self.run_dir / "stop-info.json")
        child = _load(self.run_dir / "child-end.json") or {}
        info = {
            "native_sha256": common.sha256_bytes(raw),
            "native_size": len(raw),
            "stderr_size": (self.run_dir / "native.stderr").stat().st_size
            if (self.run_dir / "native.stderr").exists()
            else None,
            "marker": marker,
            "service_result": (stop_info or {}).get("SERVICE_RESULT"),
            "reason": (report or {}).get("reason") if isinstance(report, dict) else None,
            "parse_error": parse_error,
            "max_generation": _max_generation(report),
        }
        aborts = live_checker.abort_codes(
            marker=marker,
            service_result=info["service_result"],
            report=report if isinstance(report, dict) else None,
            child_exit=child.get("exit"),
            child_signalled=bool(child.get("signalled")),
            guardian_stopped_child=bool(child.get("guardian_stopped_child")),
        )
        info["abort_codes"] = aborts
        info["failure_codes"] = (
            [] if aborts else live_checker.check_report(report, self.seconds)["codes"]
        )
        common.write_json(self.run_dir / "post-run-check.json", info)
        return info

    def r2(self) -> Result:
        started = self.ops.monotonic()
        self.ops.start_service()
        while self.within(started, R2_S):
            if self.ops.ready():
                return Result(True)
            self.ops.sleep(1)
        return Result(False, "service not ready")

    def r3_r4(self, used_generation: int | None) -> Result:
        started = self.ops.monotonic()
        while self.within(started, R34_S):
            mode = self.ops.mode()
            if eco_reached(mode):
                version = mode.get("version")
                if used_generation is None or (
                    isinstance(version, int) and version > used_generation
                ):
                    return Result(True, f"version={version}")
            else:
                self.ops.request_eco(mode)
            self.ops.sleep(0.5)
        return Result(False, "eco/new-generation ACK not reached")

    def r5(self, attempt: int) -> Result:
        started = self.ops.monotonic()
        snapshot = self.ops.snapshot()
        common.write_json(self.run_dir / f"after.attempt{attempt}.json", snapshot)
        found = environment.deviations(
            self.baseline, snapshot, "restored", allowed_units=(SELF_UNIT,)
        )
        return Result(not found and self.within(started, R5_S), ",".join(found))

    def r6(self) -> Result:
        started = self.ops.monotonic()
        for index in range(OBSERVE_SAMPLES):
            mode = self.ops.mode()
            if not (self.ops.ready() and eco_reached(mode)):
                return Result(False, f"sample {index} not restored")
            if index < OBSERVE_SAMPLES - 1:
                self.ops.sleep(OBSERVE_INTERVAL_S)
        return Result(self.within(started, R6_S + OBSERVE_INTERVAL_S), "")

    # -- sequence ------------------------------------------------------------
    def run(self) -> dict:
        outcome = {"ok": False, "attempts": self.attempts}
        used = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            record = {"attempt": attempt}
            self.attempts.append(record)
            r1 = self.r1()
            record["R1"] = r1.__dict__
            if r1.ok and attempt == 1:
                self.post = self.post_run()
                used = self.post.get("max_generation")
            if r1.ok:
                for name, step in (
                    ("R2", self.r2),
                    ("R3R4", lambda used=used: self.r3_r4(used)),
                    ("R5", lambda attempt=attempt: self.r5(attempt)),
                    ("R6", self.r6),
                ):
                    result = step()
                    record[name] = result.__dict__
                    if not result.ok:
                        break
                else:
                    outcome["ok"] = True
                    break
            if attempt < MAX_ATTEMPTS:
                self.ops.sleep(ATTEMPT_GAP_S)
        outcome["r7"] = self.r7(outcome["ok"])
        outcome["ok"] = outcome["ok"] and outcome["r7"]["ok"]
        outcome["post"] = self.post
        common.write_json(self.run_dir / "restore-result.json", outcome)
        return outcome

    def r7(self, restored: bool) -> dict:
        """Last, exactly once: final after.json, disable the guard, confirm it, report."""
        result = {"ok": False}
        try:
            final = self.ops.snapshot()
            common.write_json(self.run_dir / "after.json", {**final, "restore_ok": restored})
            self.ops.disable_self()
            result["is_enabled"] = self.ops.is_enabled()
            result["ok"] = result["is_enabled"] == "disabled"
        except Exception as error:  # noqa: BLE001 - any R7 failure is restoration_failed
            result["error"] = type(error).__name__
        return result


def exit_code(outcome: dict) -> int:
    return 0 if outcome["ok"] else EXIT_FAILED


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _max_generation(report: object) -> int | None:
    try:
        return max(g for _at, g in report["measurement"]["generation_epochs"])
    except (TypeError, KeyError, ValueError):
        return None


def final_verdict(run_dir: Path, collector: environment.Collector, baseline: dict) -> dict:
    """Run by the frozen checker after the guard unit is inactive: no unit may remain."""
    found = environment.deviations(baseline, collector.snapshot(), "restored")
    result = json.loads((run_dir / "restore-result.json").read_text())
    post = result.get("post", {})
    aborts, failures = post.get("abort_codes", ["A06"]), post.get("failure_codes", [])
    final = live_checker.final_result(aborts, failures, restore_ok=result["ok"] and not found)
    final["final_deviations"] = found
    common.write_json(run_dir / "final.json", final)
    return final


def check_only(ops: Ops, baseline: dict) -> list[str]:
    """`recover --check`: read-only look at what a restore would rely on."""
    problems = []
    try:
        snapshot = ops.snapshot()
        problems += environment.deviations(
            baseline, snapshot, "restored", allowed_units=(SELF_UNIT,)
        )
        ops.mode()
        ops.ready()
    except Exception as error:  # noqa: BLE001
        problems.append(f"check_error:{type(error).__name__}")
    return problems
