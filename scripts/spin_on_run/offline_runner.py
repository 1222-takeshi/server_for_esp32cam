"""Offline S harness: sixteen ABBA runs of the replay worker (plan section 5).

The harness is the main process of an offline unit. It stops the child and the
monitor and writes a completion marker, nothing more; the follow-up unit
(`post_runner`) checks the environment afterwards. Every external effect goes
through `Platform` so the whole flow can be dry-run against a fake worker.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Protocol

from . import common, metrics, quality
from .common import (
    CHILD_LIMIT_S,
    FILE_LIMIT,
    FREE_MIN,
    IDLE_BETWEEN_RUNS_S,
    MONITOR_MAX_AGE_S,
    ORDER,
    ROWS_PER_RUN,
    RUN_OUTPUT_LIMIT,
    SIGTERM_GRACE_S,
    STAGE_LIMIT,
    START_TEMP_MAX_MILLIC,
    START_TEMP_WAIT_S,
    STDERR_LIMIT,
    TEMP_STOP_MILLIC,
)

PLANNED = {"O01", "O02", "O03", "O04", "O05"}
HEADER_SIZES = {b"ECF1": 40, b"ECF2": 48}


def split_frames(data: bytes) -> list[bytes]:
    """Cuts a replay stream into whole envelopes (header length field, big endian)."""
    frames, at = [], 0
    while at < len(data):
        size = HEADER_SIZES.get(data[at : at + 4])
        if size is None or at + size > len(data):
            raise ValueError("replay stream is not made of whole envelopes")
        length = int.from_bytes(data[at + size - 4 : at + size], "big")
        end = at + size + length
        if length < 1 or end > len(data):
            raise ValueError("replay envelope length")
        frames.append(data[at:end])
        at = end
    return frames


class Platform(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
    def temp_millic(self) -> int: ...
    def throttled(self) -> int: ...
    def free_bytes(self, path: Path) -> int: ...
    def stage_bytes(self, path: Path) -> int: ...
    def environment_deviations(self) -> list[str]: ...


class LinuxPlatform:
    """Pi-side reads: `vcgencmd` (0.8 s timeout, one retry), statvfs, du."""

    def __init__(self, deviations=None):
        self._deviations = deviations or (lambda: [])

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def _vcgencmd_once(*args: str) -> str:
        done = subprocess.run(
            ["vcgencmd", *args], capture_output=True, text=True, timeout=0.8, check=True
        )
        return done.stdout.strip()

    def _vcgencmd(self, *args: str) -> str:
        try:
            return self._vcgencmd_once(*args)
        except (OSError, subprocess.SubprocessError):
            return self._vcgencmd_once(*args)  # one retry within the same tick

    def temp_millic(self) -> int:
        text = self._vcgencmd("measure_temp")  # temp=51.0'C
        return round(float(text.split("=")[1].split("'")[0]) * 1000)

    def throttled(self) -> int:
        return int(self._vcgencmd("get_throttled").split("=")[1], 16)

    def free_bytes(self, path: Path) -> int:
        stat = os.statvfs(path)
        return stat.f_bavail * stat.f_frsize

    def stage_bytes(self, path: Path) -> int:
        return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())

    def environment_deviations(self) -> list[str]:
        return self._deviations()


class Stop(Exception):  # noqa: N818 - a control-flow signal, not an error condition
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class Config:
    worker: Path
    model: Path
    ort: Path
    input_path: Path
    golden: list[dict]
    out_dir: Path
    clk_tck: int
    prefix: list[str] = field(default_factory=lambda: ["taskset", "--cpu-list", "1-3"])
    candidate_prefix: list[str] | None = None
    idle_s: float = IDLE_BETWEEN_RUNS_S
    monitor_period_s: float = 1.0
    child_limit_s: float = CHILD_LIMIT_S
    temp_wait_s: float = START_TEMP_WAIT_S
    order: str = ORDER
    extra_env: dict = field(default_factory=dict)
    candidate_env: dict = field(default_factory=dict)
    # Block mode (0 = legacy single pass): runs are executed in blocks of
    # this size; a block stopped by an environment deviation (O05) is
    # discarded and redone once the environment is clean again. Verdict
    # inputs and thresholds are identical either way.
    block_size: int = 0
    block_max_retries: int = 3
    block_wait_s: float = 600.0
    env_for: Callable[[str, dict], dict] = None  # condition -> child environment
    start_hook: Callable[[int, str], object] = None  # (child pid, condition) -> handle
    stop_hook: Callable[[object], object] = None  # handle -> collected data
    summarizer: Callable[["Runner", str], dict] = None
    # Unmodified workers have no frame gap setting: the harness then hands over
    # one envelope at a time and waits this long after each result line.
    pace_gap_s: float | None = None


def child_env(condition: str, extra: dict | None = None) -> dict:
    env = {
        "PATH": "/usr/bin:/bin",
        "ESP32CAM_RUST_BENCH_SPIN_ON_RUN": "1" if condition == "B" else "0",
        "ESP32CAM_RUST_BENCH_FRAME_GAP_MS": "40",
    }
    env.update(extra or {})
    return env


def env_with_candidate(
    condition: str, extra: dict | None = None, candidate_env: dict | None = None
) -> dict:
    """`child_env`, with `candidate_env` merged over condition B only.

    A candidate that needs more than the built-in spin switch (plan v4:
    the intra-op thread count, or forcing the spin switch back off for B)
    is expressed in the trial config instead of new condition letters.
    Condition A always sees exactly `extra`.
    """
    merged = dict(extra or {})
    if condition == "B":
        merged.update(candidate_env or {})
    return child_env(condition, merged)


class Runner:
    def __init__(self, cfg: Config, platform: Platform):
        self.cfg = cfg
        self.platform = platform
        self.codes: list[str] = []
        self.details: list[str] = []
        self.runs: list[dict] = []
        self.vitals: list[dict] = []
        self.windows: list[tuple[str, float, float]] = []
        self._last_sample = platform.monotonic()
        self._started = platform.monotonic()
        self._vitals_file = None
        self._metrics = self._temperature = None

    # -- recording ---------------------------------------------------------
    def record(self, code: str, detail: str = "") -> None:
        if code not in self.codes:
            self.codes.append(code)
        self.details.append(f"{code}:{detail}")

    # -- monitoring --------------------------------------------------------
    def sample(self, now: float) -> None:
        self._sample(now, strict=True)

    def _sample(self, now: float, *, strict: bool) -> None:
        """One vitals sample; raises Stop for thermal, capacity or stale values.

        Non-strict samples (block-mode waits) skip the deviation check so
        vitals keep flowing while the environment is known to deviate.
        """
        try:
            temp, throttled = self.platform.temp_millic(), self.platform.throttled()
            free = self.platform.free_bytes(self.cfg.out_dir)
            used = self.platform.stage_bytes(self.cfg.out_dir)
            deviation = self.platform.environment_deviations() if strict else []
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            if now - self._last_sample > MONITOR_MAX_AGE_S:
                raise Stop("O06", f"monitor stale: {type(error).__name__}") from error
            return
        self._last_sample = now
        record = {"t": now, "temp_millic": temp, "throttled": throttled, "free": free, "used": used}
        self.vitals.append(record)
        self._vitals_file.write(json.dumps(record) + "\n")
        self._vitals_file.flush()
        if temp >= TEMP_STOP_MILLIC or throttled != 0:
            raise Stop("O03", f"temp={temp} throttled={throttled:#x}")
        if used > STAGE_LIMIT or free < FREE_MIN:
            raise Stop("O06", "capacity")
        if deviation:
            raise Stop("O05", ",".join(deviation))

    def check_log_rows(self, now: float) -> None:
        expected = int(now - self._started) - MONITOR_MAX_AGE_S
        if len(self.vitals) < expected:
            raise Stop("O06", "vitals rows missing")

    def wait_start_temperature(self) -> None:
        deadline = self.platform.monotonic() + self.cfg.temp_wait_s
        while True:
            now = self.platform.monotonic()
            self.sample(now)
            if self.vitals[-1]["temp_millic"] <= START_TEMP_MAX_MILLIC:
                return
            if now >= deadline:
                raise Stop("O04", f"temp={self.vitals[-1]['temp_millic']}")
            self.platform.sleep(min(self.cfg.monitor_period_s, 1.0))

    def prefix_for(self, condition: str) -> list[str]:
        """Spawn prefix for a condition; candidate B may carry its own (plan v4-E)."""
        if condition == "B" and self.cfg.candidate_prefix is not None:
            return self.cfg.candidate_prefix
        return self.cfg.prefix

    # -- one run -----------------------------------------------------------
    def spawn(self, condition: str, stderr_path: Path):
        argv = [*self.prefix_for(condition), str(self.cfg.worker), str(self.cfg.model), str(self.cfg.ort)]
        paced = self.cfg.pace_gap_s is not None
        if paced:
            self._frames = split_frames(Path(self.cfg.input_path).read_bytes())
        stdin = subprocess.PIPE if paced else open(self.cfg.input_path, "rb")  # noqa: SIM115
        stderr = open(stderr_path, "wb")  # noqa: SIM115
        child = subprocess.Popen(
            argv,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=(
                self.cfg.env_for(condition, self.cfg.extra_env)
                if self.cfg.env_for is not None
                else env_with_candidate(condition, self.cfg.extra_env, self.cfg.candidate_env)
            ),
        )
        if paced:
            stdin = child.stdin
            self.hand_over(child, 0)
        return child, stdin, stderr

    def hand_over(self, child, index: int) -> None:
        """Writes envelope `index`; closes the pipe after the last one."""
        try:
            child.stdin.write(self._frames[index])
            child.stdin.flush()
            if index == len(self._frames) - 1:
                child.stdin.close()
        except (BrokenPipeError, ValueError):
            pass  # a child that is gone is noticed by the read loop

    def after_row(self, child, rows_done: int) -> None:
        """Gap after a result line, then the next envelope (never after the last)."""
        if self.cfg.pace_gap_s is None or rows_done >= len(self._frames):
            return
        self.platform.sleep(self.cfg.pace_gap_s)
        self.hand_over(child, rows_done)

    def stop_child(self, child) -> None:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(SIGTERM_GRACE_S)
            except subprocess.TimeoutExpired:
                child.kill()
        child.wait()

    def run_one(self, index: int, condition: str) -> None:
        run_dir = self.cfg.out_dir
        stderr_path = run_dir / f"run{index:02d}.stderr"
        child, stdin, stderr = self.spawn(condition, stderr_path)
        rows: list[dict] = []
        raw = bytearray()
        started = self.platform.monotonic()
        window_start = started
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        pending = b""
        planned: Stop | None = None
        handle = None
        self._recv = {}
        try:
            if self.cfg.start_hook:
                handle = self.cfg.start_hook(child.pid, condition)
            while True:
                now = self.platform.monotonic()
                self.sample(now)
                self.check_log_rows(now)
                if now - started > self.cfg.child_limit_s:
                    raise Stop("O06", "child time limit")
                if os.path.getsize(stderr_path) > STDERR_LIMIT:
                    raise Stop("O06", "stderr over limit")
                events = selector.select(self.cfg.monitor_period_s)
                if events:
                    chunk = os.read(child.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    raw += chunk
                    if len(raw) > RUN_OUTPUT_LIMIT:
                        raise Stop("O06", "output over limit")
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for line in lines:
                        self.accept_line(index, condition, rows, line)
                        if len(rows) - 1 in (4, ROWS_PER_RUN - 1):
                            self._recv[len(rows) - 1] = self.platform.monotonic()
                        self.after_row(child, len(rows))
            if pending.strip():
                raise Stop("O06", "unterminated output line")
        except Stop as stop:
            planned = stop
        finally:
            collected = self.cfg.stop_hook(handle) if self.cfg.stop_hook and handle else None
            selector.close()
            self.stop_child(child)
            child.stdout.close()
            stdin.close()
            stderr.close()
        window_end = self.platform.monotonic()
        self.windows.append((condition, window_start, window_end))
        if planned is not None:
            # Anything the child wrote while it was being stopped is judged on its own.
            if os.path.getsize(stderr_path) > 0 and planned.code in PLANNED:
                self.record("O06", "child stderr during planned stop")
            self.save_raw(index, condition, raw, stderr_path)
            raise planned
        self.save_raw(index, condition, raw, stderr_path)
        if os.path.getsize(stderr_path) != 0:
            raise Stop("O06", "stderr not empty")
        if child.returncode != 0:
            raise Stop("O06", f"child exit {child.returncode}")
        if len(rows) != ROWS_PER_RUN:
            raise Stop("O06", f"rows={len(rows)}")
        self.runs.append(
            {
                "condition": condition,
                "rows": rows,
                "recv": dict(self._recv),
                "collected": collected,
                "started": started,
            }
        )

    def accept_line(self, index: int, condition: str, rows: list[dict], line: bytes) -> None:
        try:
            row = quality.parse_line(line)
        except ValueError as error:
            raise Stop("O06", "unparsable result line") from error
        rows.append(row)
        position = len(rows) - 1
        if position >= len(self.cfg.golden):
            raise Stop("O06", "extra row")
        field_name = quality.mismatch(row, self.cfg.golden[position])
        if field_name:
            code = "O02" if condition == "A" else "O01"
            raise Stop(code, f"run={index} row={position} field={field_name}")

    def save_raw(self, index: int, condition: str, raw: bytearray, stderr_path: Path) -> None:
        name = self.cfg.out_dir / f"run{index:02d}-{condition}.jsonl"
        common.write_new(name, bytes(raw)[:FILE_LIMIT])
        os.chmod(stderr_path, 0o400)

    # -- whole experiment ----------------------------------------------------
    def run(self) -> dict:
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self._vitals_file = open(self.cfg.out_dir / "vitals.jsonl", "x")  # noqa: SIM115
        marker = "completed"
        try:
            if self.cfg.block_size:
                self._wait_environment_clean()
            else:
                self.sample(self.platform.monotonic())
            self._run_all()
            self.evaluate_metrics()
        except Stop as stop:
            self.record(stop.code, stop.detail)
        except BaseException as error:  # noqa: BLE001 - uncaught harness failure
            self.record("O06", f"uncaught {type(error).__name__}")
        finally:
            if self.codes:
                marker = "abort " + ",".join(self.codes)
            self._vitals_file.close()
        os.chmod(self.cfg.out_dir / "vitals.jsonl", 0o400)
        common.write_new(self.cfg.out_dir / "marker", marker + "\n")
        return self.summarize(marker)

    def _run_all(self) -> None:
        order = self.cfg.order
        if not self.cfg.block_size:
            for index, condition in enumerate(order):
                self.wait_start_temperature()
                self.run_one(index, condition)
                if index < len(order) - 1:
                    self.platform.sleep(self.cfg.idle_s)
            return
        index = 0
        attempts: dict[int, int] = {}
        while index < len(order):
            block_end = min(index + self.cfg.block_size, len(order))
            runs_before, windows_before = len(self.runs), len(self.windows)
            current = index
            try:
                while current < block_end:
                    self.wait_start_temperature()
                    self.run_one(current, order[current])
                    if current < len(order) - 1:
                        self.platform.sleep(self.cfg.idle_s)
                    current += 1
            except Stop as stop:
                if stop.code != "O05" or attempts.get(index, 0) >= self.cfg.block_max_retries:
                    raise
                attempts[index] = attempts.get(index, 0) + 1
                self.details.append(f"block_retry:{index}:{attempts[index]}:{stop.detail}")
                self._discard_block(index, current, runs_before, windows_before)
                self._wait_environment_clean()
                continue
            index = block_end

    def _discard_block(self, block_start: int, failed: int, runs_before: int, windows_before: int) -> None:
        del self.runs[runs_before:]
        del self.windows[windows_before:]
        for index in range(block_start, failed + 1):
            for name in (f"run{index:02d}-{self.cfg.order[index]}.jsonl", f"run{index:02d}.stderr"):
                try:
                    (self.cfg.out_dir / name).unlink()
                except FileNotFoundError:
                    pass

    def _wait_environment_clean(self) -> None:
        """Block mode: keep sampling vitals until no deviations remain."""
        deadline = self.platform.monotonic() + self.cfg.block_wait_s
        while True:
            now = self.platform.monotonic()
            self._sample(now, strict=False)
            if not self.platform.environment_deviations():
                return
            if now >= deadline:
                raise Stop("O05", "environment_not_restored_wait")
            self.platform.sleep(self.cfg.monitor_period_s)

    def evaluate_metrics(self) -> None:
        """Before the marker is written: a malformed per-row field is O06 (plan 5.4, 9.1).

        Pooled problems (empty set, zero denominator, run composition) stay a
        missing metric and end as V4; nothing is repaired or skipped.
        """
        self._metrics = self._temperature = None
        if self.cfg.summarizer or self.codes:
            return
        try:
            self._metrics = metrics.evaluate(self.runs, self.cfg.clk_tck)
            self._temperature = metrics.temperature_summary(self.vitals, self.windows)
        except metrics.FieldError as error:
            self._metrics = self._temperature = None
            self.record("O06", f"metrics: {error}")
        except metrics.MetricsError as error:
            self._metrics = self._temperature = None
            self.details.append(f"metrics:{error}")

    def summarize(self, marker: str) -> dict:
        if self.cfg.summarizer:
            return self.cfg.summarizer(self, marker)
        m, temp = self._metrics, self._temperature
        verdict = metrics.final_verdict(
            self.codes, m, temp, quality_ok=not self.codes, environment_ok=True
        )
        result = {
            "marker": marker,
            "codes": self.codes,
            "details": self.details,
            "verdict": verdict,
            "metrics": metrics.printable(m) if m else None,
            "temperature": temp,
            "runs_completed": len(self.runs),
        }
        common.write_json(self.cfg.out_dir / "result.json", result)
        return result
