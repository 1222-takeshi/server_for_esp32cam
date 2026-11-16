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
    """The restore sequence. Nothing a step raises may skip a later step or R7."""

    def __init__(self, ops: Ops, run_dir: Path, baseline: dict, seconds: int):
        self.ops, self.run_dir, self.baseline, self.seconds = ops, run_dir, baseline, seconds
        self.post: dict = {}
        self.attempts: list[dict] = []
        self.saved: list[str] = []
        self.save_errors: list[str] = []

    # -- helpers ---------------------------------------------------------------
    def within(self, started: float, limit: float) -> bool:
        return self.ops.monotonic() - started <= limit

    def save(self, name: str, value: object) -> Path | None:
        """`xb` output that never raises: a name taken by an earlier start gets `.rN`."""
        stem, dot, suffix = name.rpartition(".")
        for index in range(100):
            target = name if index == 0 else f"{stem}.r{index}{dot}{suffix}"
            try:
                path = common.write_json(self.run_dir / target, value)
            except FileExistsError:
                continue
            except OSError as error:
                self.save_errors.append(f"{name}:{type(error).__name__}")
                return None
            self.saved.append(target)
            return path
        self.save_errors.append(f"{name}:names_exhausted")
        return None

    def guarded(self, step) -> Result:
        try:
            return step()
        except Exception as error:  # noqa: BLE001 - a failed step must not end the sequence
            return Result(False, f"error:{type(error).__name__}")

    def last_seen_version(self) -> int | None:
        """Version of the mode state recorded at the start, used when no report exists."""
        version = (self.baseline.get("mode") or {}).get("version")
        return version if isinstance(version, int) else None

    # -- individual steps ----------------------------------------------------
    def r1(self) -> Result:
        started = self.ops.monotonic()
        try:
            self.ops.stop_trial()
        except Exception as error:  # noqa: BLE001 - the state read below decides
            stop_error = type(error).__name__
        else:
            stop_error = ""
        state = self.ops.trial_state()
        stopped = (
            state.get("active_state") in {"inactive", "failed"}
            and state.get("main_pid") == 0
            and state.get("cgroup_empty") is True
        )
        return Result(stopped and self.within(started, R1_S), f"{state} {stop_error}".strip())

    def read_report(self) -> tuple[bytes, object, str | None]:
        native = self.run_dir / "native.json"
        raw = native.read_bytes() if native.exists() else b""
        try:
            return raw, (json.loads(raw) if raw else None), None
        except ValueError as error:
            return raw, None, type(error).__name__

    def post_run(self) -> dict:
        """Once, after the first successful R1; the only place the report is read."""
        started = self.ops.monotonic()
        raw, report, parse_error = self.read_report()
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
            "report_present": isinstance(report, dict),
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
        info["not_started"] = not aborts and live_checker.not_started(report)
        checked = None if aborts else live_checker.check_report(report, self.seconds)
        info["failure_codes"] = [] if checked is None else checked["codes"]
        info["interval_unjudgeable"] = bool(checked and checked["interval_unjudgeable"])
        if checked and checked["interval"] is not None:
            self.save("interval-gate.json", checked["interval"])
        info["within_deadline"] = self.within(started, PR_S)
        self.save("post-run-check.json", info)
        return info

    def once_post_run(self) -> None:
        """PR never repeats: an earlier start's saved result is reused, a failure is kept."""
        if self.post:
            return
        saved = _load(self.run_dir / "post-run-check.json")
        if saved is not None:
            self.post = saved
            return
        try:
            self.post = self.post_run()
        except Exception as error:  # noqa: BLE001 - recorded; restoration continues
            self.post = {"error": type(error).__name__}
            self.save("post-run-check.json", self.post)

    def r2(self) -> Result:
        started = self.ops.monotonic()
        last, requested = "", False
        while self.within(started, R2_S):
            try:
                if not requested:
                    self.ops.start_service()
                    requested = True
                if self.ops.ready():
                    return Result(True)
            except Exception as error:  # noqa: BLE001 - retried until the deadline
                last = type(error).__name__
            self.ops.sleep(1)
        return Result(False, f"service not ready {last}".strip())

    def r3_r4(self, used_generation: int | None) -> Result:
        """Eco, then an ACK whose version is above everything used in the trial."""
        started = self.ops.monotonic()
        if used_generation is None:
            return Result(False, "generation used in the trial is unknown")
        seen = None
        while self.within(started, R34_S):
            try:
                mode = self.ops.mode()
                if eco_reached(mode):
                    seen = mode.get("version")
                    if isinstance(seen, int) and seen > used_generation:
                        return Result(True, f"version={seen}")
                else:
                    self.ops.request_eco(mode)
            except Exception as error:  # noqa: BLE001 - retried until the deadline
                seen = f"error:{type(error).__name__}"
            self.ops.sleep(0.5)
        return Result(False, f"eco/new-generation ACK not reached (last {seen})")

    def r5(self, attempt: int) -> Result:
        started = self.ops.monotonic()
        snapshot = self.ops.snapshot()
        self.save(f"after.attempt{attempt}.json", snapshot)
        # The recheck can add 20 s, and R5 has not been shown to fit its 30 s limit
        # with it, so R5 judges the plain read; the record is still kept.
        found, record = environment.deviations_with_recheck(
            self.baseline,
            snapshot,
            "restored",
            self.ops.snapshot,
            allowed_units=(SELF_UNIT,),
            recheck=False,
            sleep=self.ops.sleep,
            clock=self.ops.monotonic,
        )
        self.save(f"mode-recheck.attempt{attempt}.json", record)
        return Result(not found and self.within(started, R5_S), ",".join(found))

    def r6(self) -> Result:
        """13 samples 10 s apart; the whole step must end within 130 s."""
        started = self.ops.monotonic()
        for index in range(OBSERVE_SAMPLES):
            mode = self.ops.mode()
            if not (self.ops.ready() and eco_reached(mode)):
                return Result(False, f"sample {index} not restored")
            if not self.within(started, R6_S):
                return Result(False, f"sample {index} after the {R6_S} s limit")
            if index < OBSERVE_SAMPLES - 1:
                self.ops.sleep(OBSERVE_INTERVAL_S)
        return Result(self.within(started, R6_S), "")

    # -- sequence ------------------------------------------------------------
    def used_generation(self) -> int | None:
        """Report's last generation; without a report, the last version seen.

        A report that is present but does not carry `generation_epochs` is not "no
        report": what the trial used is unknown then, and R4 must fail rather than
        fall back to the version recorded before the trial.
        """
        post = self.post if isinstance(self.post, dict) else {}
        if isinstance(post.get("max_generation"), int):
            return post["max_generation"]
        if post.get("report_present"):
            return None
        return self.last_seen_version()

    def attempt(self, number: int) -> bool:
        record = {"attempt": number}
        self.attempts.append(record)
        r1 = self.guarded(self.r1)
        record["R1"] = r1.__dict__
        if not r1.ok:
            return False
        self.once_post_run()
        used = self.used_generation()
        for name, step in (
            ("R2", self.r2),
            ("R3R4", lambda: self.r3_r4(used)),
            ("R5", lambda: self.r5(number)),
            ("R6", self.r6),
        ):
            result = self.guarded(step)
            record[name] = result.__dict__
            if not result.ok:
                return False
        return True

    def run(self) -> dict:
        settled = _load(self.run_dir / "restore-result.json")
        if settled is not None:
            return settled  # an earlier start already finished and released the guard
        outcome = {"ok": False, "attempts": self.attempts}
        interrupted = None
        try:
            for number in range(1, MAX_ATTEMPTS + 1):
                if self.attempt(number):
                    outcome["ok"] = True
                    break
                if number < MAX_ATTEMPTS:
                    self.ops.sleep(ATTEMPT_GAP_S)
        except BaseException as error:  # noqa: BLE001 - R7 and the result still happen
            interrupted = error
            outcome["interrupted"] = type(error).__name__
        finally:
            outcome["r7"] = self.r7(outcome["ok"])
            outcome["ok"] = outcome["ok"] and outcome["r7"]["ok"]
            outcome["post"] = self.post
            outcome["post_ok"] = post_ok(self.post)
            outcome["saved"] = self.saved
            outcome["save_errors"] = self.save_errors
            if self.save("restore-result.json", outcome) is None:
                outcome["ok"] = False
        if interrupted is not None:
            raise interrupted
        return outcome

    def r7(self, restored: bool) -> dict:
        """Last, exactly once: final after.json, disable the guard, confirm it, report.

        Each part is attempted whatever happened before it; any failure, or more
        than 15 s in total, is `restoration_failed`.
        """
        started = self.ops.monotonic()
        result = {"ok": True}

        def part(name, action):
            try:
                action()
            except Exception as error:  # noqa: BLE001 - recorded, the next part still runs
                result["ok"] = False
                result.setdefault("errors", []).append(f"{name}:{type(error).__name__}")

        def final_after():
            final = self.ops.snapshot()
            if self.save("after.json", {**final, "restore_ok": restored}) is None:
                raise OSError("after.json not saved")

        def release():
            self.ops.disable_self()
            result["is_enabled"] = self.ops.is_enabled()
            if result["is_enabled"] != "disabled":
                raise RuntimeError("guard still enabled")

        part("after", final_after)
        part("release", release)
        if not self.within(started, R7_S):
            result["ok"] = False
            result["late"] = True
        return result


def post_ok(post: dict) -> bool:
    """The post-run check ran, finished within its 30 s and found its inputs."""
    return bool(post) and "error" not in post and post.get("within_deadline") is True


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
        pass
    if live_checker.not_started(report):
        # No epochs exist before the measurement; the mode's last version, from the
        # session's final status, is what the restore's ACK must exceed.
        version = (report.get("session_final") or {}).get("version")
        if type(version) is int:
            return version
    return None


def final_verdict(run_dir: Path, collector: environment.Collector, baseline: dict) -> dict:
    """Run by the frozen checker after the guard unit is inactive: no unit may remain."""
    found, recheck = environment.deviations_with_recheck(
        baseline, collector.snapshot(), "restored", collector.snapshot
    )
    result = json.loads((run_dir / "restore-result.json").read_text())
    post = result.get("post", {})
    aborts, failures = post.get("abort_codes", ["A06"]), post.get("failure_codes", [])
    restored = result["ok"] and not found and result.get("post_ok", post_ok(post))
    if "invariant:camera_boot_id" in found and "A04" not in aborts:
        # The native process ends on a boot change it sees (A07, recorded first); the
        # guardian cannot read the camera boot id while the service is stopped, so the
        # change is confirmed here, after the restore (plan 10.1, last bullet).
        aborts = [*aborts, "A04"]
    final = live_checker.final_result(
        aborts,
        failures,
        restore_ok=restored,
        not_started=post.get("not_started") is True,
        interval_unjudgeable=post.get("interval_unjudgeable") is True,
    )
    final["final_deviations"] = found
    final["mode_recheck"] = recheck
    common.write_json(run_dir / "final.json", final)
    return final


def check_only(ops: Ops, baseline: dict, allowed_units: tuple[str, ...] = ()) -> list[str]:
    """`recover --check`: read-only look at what a restore would rely on."""
    problems = []
    try:
        snapshot = ops.snapshot()
        problems += environment.deviations(
            baseline, snapshot, "restored", allowed_units=(SELF_UNIT, *allowed_units)
        )
        ops.mode()
        ops.ready()
    except Exception as error:  # noqa: BLE001
        problems.append(f"check_error:{type(error).__name__}")
    return problems
