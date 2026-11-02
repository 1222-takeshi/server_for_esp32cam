"""Main process of the live trial unit: periodic checks, abort and end marker (plan 8.6, 9.3).

The guardian watches, stops the child on a violation and writes the end marker.
It is never asked to restore anything after it stops; the independent recover
guard does that. Monitoring is a pure `Checker` driven once per second.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import common, environment
from .live_checker import DURATION_US  # noqa: F401 - re-exported for callers

TEMP_STOP_MILLIC = 80_000
TEMP_WARN_MILLIC = 75_000
STALE_S = 3.0
FREE_STOP = 1024**3
FREE_START = 2 * 1024**3
STAGE_LIMIT = {600: 64 * 1024**2, 3600: 256 * 1024**2}
REPORT_LIMIT = 16 * 1024**2
FILE_LIMIT = 4 * 1024**2
STDERR_LIMIT = 64 * 1024
LOGS = ("native.json", "native.stderr", "vitals.jsonl")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".ecf2")


@dataclass
class Reading:
    """One second of fast vitals; `None` fields mean the read failed this tick."""

    temp_millic: int | None
    throttled: int | None
    free_bytes: int | None
    fast: dict | None  # `Collector.snapshot_local()`: no network, no service API


@dataclass
class Checker:
    seconds: int
    run_dir: Path
    baseline: dict
    trial_unit: str
    codes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _good_vitals: float | None = None
    _good_fast: float | None = None
    _started: float | None = None
    _rows: int = 0

    def add(self, code: str) -> None:
        if code not in self.codes:
            self.codes.append(code)

    def tick(self, now: float, reading: Reading, child_alive: bool) -> list[str]:
        """Evaluate one second. Order follows the plan table so ties keep the top code."""
        if self._started is None:
            self._started = self._good_vitals = self._good_fast = now
        if reading.temp_millic is not None and reading.throttled is not None:
            self._good_vitals = now
            self._rows += 1
            self._append_vitals(now, reading)
            if reading.temp_millic >= TEMP_STOP_MILLIC:
                self.add("A01")
            elif reading.temp_millic >= TEMP_WARN_MILLIC:
                self.warnings.append(f"temp {reading.temp_millic} at {now:.0f}")
            if reading.throttled != 0:
                self.add("A02")
        if self._capacity_exceeded(reading):
            self.add("A03")
        self._invariants(now, reading)
        if now - self._good_vitals > STALE_S or now - self._good_fast > STALE_S:
            self.add("A05")
        if self._logs_lost(now):
            self.add("A08")
        if self._images():
            self.add("A09")
        return self.codes

    def _append_vitals(self, now: float, r: Reading) -> None:
        line = json.dumps(
            {"t": now, "temp_millic": r.temp_millic, "throttled": r.throttled, "free": r.free_bytes}
        )
        with open(self.run_dir / "vitals.jsonl", "a") as handle:
            handle.write(line + "\n")

    def _capacity_exceeded(self, r: Reading) -> bool:
        if r.free_bytes is not None and r.free_bytes < FREE_STOP:
            return True
        total = sum(p.stat().st_size for p in self.run_dir.rglob("*") if p.is_file())
        if total > STAGE_LIMIT[self.seconds]:
            return True
        for path in self.run_dir.glob("*"):
            if not path.is_file():
                continue
            limit = REPORT_LIMIT if path.name == "native.json" else FILE_LIMIT
            if path.name == "native.stderr":
                limit = STDERR_LIMIT
            if path.stat().st_size > limit:
                return True
        return False

    def _invariants(self, now: float, r: Reading) -> None:
        """Judge the local snapshot; it never touches the original service's API."""
        if r.fast is None:
            return  # only ages the last good value (A05 after STALE_S)
        self._good_fast = now
        if environment.deviations(
            self.baseline, r.fast, "live", allowed_units=(self.trial_unit + ".service",)
        ):
            self.add("A04")

    def _logs_lost(self, now: float) -> bool:
        if any(not (self.run_dir / name).exists() for name in LOGS):
            return True
        elapsed = now - self._started
        return self._rows < int(elapsed) - 3 and elapsed > 4

    def _images(self) -> bool:
        return any(p.suffix.lower() in IMAGE_SUFFIXES for p in self.run_dir.rglob("*"))


def stop_child(child: subprocess.Popen, grace: float = 5.0) -> None:
    """SIGTERM, wait, SIGKILL, wait; the guardian's own stop is not included."""
    if child.poll() is None:
        child.send_signal(signal.SIGTERM)
        try:
            child.wait(grace)
        except subprocess.TimeoutExpired:
            child.kill()
    child.wait()


def write_marker(run_dir: Path, codes: list[str]) -> Path:
    text = "completed\n" if not codes else "abort " + ",".join(codes) + "\n"
    return common.write_new(run_dir / "marker", text)


def heartbeat_line(nonce: str, utc_ms: int) -> bytes:
    return f"ready {nonce} {utc_ms}\n".encode()


def child_end_state(child: subprocess.Popen) -> dict:
    code = child.returncode
    return {
        "exit": code if code is not None and code >= 0 else None,
        "signalled": bool(code and code < 0),
    }


def prepare_run_dir(run_dir: Path):
    """Fresh 0700 directory; native.json and native.stderr are created exclusively (0600)."""
    run_dir.mkdir(mode=0o700, exist_ok=False)
    native = open(run_dir / "native.json", "xb")  # noqa: SIM115 - handed to the child
    stderr = open(run_dir / "native.stderr", "xb")  # noqa: SIM115
    open(run_dir / "vitals.jsonl", "x").close()  # noqa: SIM115
    for name in LOGS:
        os.chmod(run_dir / name, 0o600)
    return native, stderr


@dataclass
class IO:
    """Everything the guardian loop touches outside its own memory."""

    monotonic: callable
    sleep: callable
    read: callable  # (now) -> Reading
    spawn: (
        callable  # (argv, env, stdout, stderr) -> child with .stdin/.poll/.wait/.send_signal/.kill
    )
    utc_ms: callable


def execute(
    *,
    run_dir: Path,
    seconds: int,
    argv: list[str],
    env: dict[str, str],
    nonce: str,
    checker_factory: callable,
    io: IO,
    grace: float = 5.0,
    period: float = 1.0,
) -> dict:
    """The trial unit's main loop. Writes `child-end.json` and the end marker, then returns."""
    native, stderr = prepare_run_dir(run_dir)
    checker = checker_factory(run_dir)
    child = io.spawn(argv, env, native, stderr)
    native.close()
    stderr.close()
    stopped_by_us = False
    next_tick = io.monotonic()
    try:
        while True:
            now = io.monotonic()
            try:
                child.stdin.write(heartbeat_line(nonce, io.utc_ms()))
                child.stdin.flush()
            except (OSError, ValueError):
                pass  # a dead child is detected by poll() below
            alive = child.poll() is None
            codes = checker.tick(now, io.read(now), alive)
            if codes:
                stopped_by_us = True
                break
            if not alive:
                break
            next_tick += period
            io.sleep(max(0.0, next_tick - io.monotonic()))
    except BaseException:
        checker.add("A06")
        stopped_by_us = True
        raise
    finally:
        stop_child(child, grace)
        end = {**child_end_state(child), "guardian_stopped_child": stopped_by_us}
        common.write_json(run_dir / "child-end.json", end)
        write_marker(run_dir, checker.codes)
    return {"codes": checker.codes, "warnings": checker.warnings, "child": end}
