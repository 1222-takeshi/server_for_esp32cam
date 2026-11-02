"""Candidate M: per-thread CPU time sampling and its success rules (plan section 6).

Measurement only. The sampler reads `/proc/<pid>/task/<tid>/{stat,schedstat,status}`
without privileges; the evaluation functions are pure and judge only whether the
measurement itself worked, never the cause of any latency.
"""

from __future__ import annotations

import math
import os
from fractions import Fraction

from .metrics import nearest_rank

PERIOD_S = 0.1
MAX_LATE_S = 0.1
M_ORDER = "XYYXXY"


class ProcReader:
    """Real `/proc` access. Each call raises on failure; callers classify it."""

    def __init__(self, root: str = "/proc"):
        self.root = root

    def tids(self, pid: int) -> list[int]:
        return sorted(int(name) for name in os.listdir(f"{self.root}/{pid}/task"))

    def read(self, pid: int, tid: int, name: str) -> str:
        with open(f"{self.root}/{pid}/task/{tid}/{name}") as handle:
            return handle.read()

    def self_ticks(self, pid: int) -> int:
        with open(f"{self.root}/{pid}/stat") as handle:
            return parse_stat(handle.read())[0]


def parse_stat(text: str) -> tuple[int, int]:
    """`(utime + stime, starttime)` in clock ticks."""
    fields = text.rsplit(") ", 1)[1].split()
    return int(fields[11]) + int(fields[12]), int(fields[19])


def parse_schedstat(text: str) -> int:
    parts = text.split()
    if len(parts) < 3:
        raise ValueError("schedstat")
    return int(parts[0])


def take_sample(reader, pid: int, slot: int, started: float, scheduled: float) -> dict | None:
    """One reading of every thread. None when the thread list is unavailable."""
    try:
        tids = reader.tids(pid)
        own = reader.self_ticks(pid)
    except OSError:
        return None
    threads = {}
    for tid in tids:
        entry = {"sched_ns": None, "ticks": None, "start": None, "gone": False, "failed": False}
        try:
            ticks, start = parse_stat(reader.read(pid, tid, "stat"))
            entry.update(ticks=ticks, start=start)
        except (FileNotFoundError, ProcessLookupError):
            entry["gone"] = True
        except (OSError, ValueError, IndexError):
            entry["failed"] = True
        if not entry["gone"]:
            try:
                entry["sched_ns"] = parse_schedstat(reader.read(pid, tid, "schedstat"))
            except (FileNotFoundError, ProcessLookupError):
                entry["gone"] = True
            except (OSError, ValueError):
                entry["failed"] = True
        threads[(tid, entry["start"])] = entry
    return {
        "slot": slot,
        "read_at": started,
        "lateness": started - scheduled,
        "own_ticks": own,
        "tasks": len(tids),
        "threads": threads,
    }


def run_sampler(reader, pid: int, clock, sleep, stop, *, t0: float | None = None) -> list[dict]:
    """Fixed-schedule loop; a slot more than 100 ms late is skipped, never caught up."""
    t0 = clock() if t0 is None else t0
    samples, slot, skipped = [], 0, 0
    while not stop():
        scheduled = t0 + slot * PERIOD_S
        now = clock()
        if now < scheduled:
            sleep(scheduled - now)
            continue
        if now - scheduled > MAX_LATE_S:
            skipped += 1
            slot = int((now - t0) / PERIOD_S) + 1
            continue
        sample = take_sample(reader, pid, slot, now, scheduled)
        if sample:
            samples.append(sample)
        slot += 1
    return samples


def expected_samples(t0: float, window: tuple[float, float]) -> int:
    """Number of schedule slots `t0 + k * 100 ms` inside the half-open `[start, end)`."""
    start, end = window
    first = max(0, math.ceil((start - t0) / PERIOD_S - 1e-9))
    after = math.ceil((end - t0) / PERIOD_S - 1e-9)
    return max(0, after - first)


def evaluate_run(samples: list[dict], window: tuple[float, float], t0: float, clk_tck: int) -> dict:
    """M1 to M5 (sampler CPU) for one Y run."""
    inside = [s for s in samples if window[0] <= s["read_at"] <= window[1]]
    inside_ids = {id(s) for s in inside}
    expected = expected_samples(t0, window)
    tick_ns = 1_000_000_000 // clk_tck
    denominator = numerator = 0
    last_ok: dict = {}
    first_last: dict = {}
    sched_read = False
    for sample in samples:
        for key, entry in sample["threads"].items():
            if entry["gone"]:
                continue
            denominator += 1
            if entry["sched_ns"] is None or entry["failed"]:
                continue
            sched_read = True
            if entry["sched_ns"] >= last_ok.get(key, 0):
                numerator += 1
            last_ok[key] = max(entry["sched_ns"], last_ok.get(key, 0))
            if id(sample) in inside_ids:
                first_last.setdefault(key, [entry, entry])[1] = entry
    m1_ratio = Fraction(numerator, denominator) if denominator else Fraction(0)
    moving = sum(
        1
        for a, b in first_last.values()
        if b["sched_ns"] is not None and b["sched_ns"] > a["sched_ns"]
    )
    reads = [s["read_at"] for s in inside]
    gaps = [b - a for a, b in zip(reads, reads[1:], strict=False)]
    lateness = [round(s["lateness"] * 1e6) for s in inside]
    many = sum(1 for s in inside if s["tasks"] >= 3)
    delta_sched = delta_stat = 0
    for a, b in first_last.values():
        if None in (a["ticks"], b["ticks"], a["sched_ns"], b["sched_ns"]):
            continue
        delta_sched += b["sched_ns"] - a["sched_ns"]
        delta_stat += (b["ticks"] - a["ticks"]) * tick_ns
    sampler_cpu = None
    if len(inside) >= 2:
        span = inside[-1]["read_at"] - inside[0]["read_at"]
        used = inside[-1]["own_ticks"] - inside[0]["own_ticks"]
        if span > 0:
            sampler_cpu = Fraction(used, clk_tck) / Fraction(span)
    return {
        "expected": expected,
        "taken": len(inside),
        "schedstat_readable": sched_read,
        "m1": m1_ratio >= Fraction(99, 100) and moving >= 3,
        "m1_ratio": m1_ratio,
        "m1_moving_keys": moving,
        "m2": (
            len(inside) >= Fraction(95, 100) * expected
            and bool(lateness)
            and nearest_rank(lateness, 95) <= 20_000
            and (not gaps or max(gaps) <= 0.5)
        ),
        "m3": bool(inside) and Fraction(many, len(inside)) >= Fraction(95, 100),
        "m4": delta_stat > 0
        and abs(delta_sched - delta_stat) <= Fraction(5, 100) * delta_stat + 2 * tick_ns,
        "m5_sampler": sampler_cpu is not None and sampler_cpu <= Fraction(2, 100),
        "all_zero_oncpu": sched_read and all(v == 0 for v in last_ok.values()),
    }


def combine_infer(x_values: list[int], y_values: list[int]) -> dict:
    """M5 impact on the worker: Y/X mean and P95 of the 240 pooled values each."""
    if len(x_values) != 240 or len(y_values) != 240:
        return {"m5_infer": False, "reason": "sample size"}
    mean = Fraction(sum(y_values), 240) / Fraction(sum(x_values), 240)
    p95 = Fraction(nearest_rank(y_values, 95), nearest_rank(x_values, 95))
    return {
        "m5_infer": mean <= Fraction(102, 100) and p95 <= Fraction(105, 100),
        "mean_ratio": mean,
        "p95_ratio": p95,
    }


def outcome(*, harness_failure: bool, runs: list[dict], infer: dict, m6: bool, m7: bool) -> dict:
    """First matching row of the M decision table (M-D1 .. M-D4)."""
    if harness_failure or not m6 or not m7:
        return {"id": "M-D1", "result": "measurement_failed"}
    if len(runs) == 3 and (
        all(not r["schedstat_readable"] for r in runs) or all(r["all_zero_oncpu"] for r in runs)
    ):
        return {"id": "M-D2", "result": "not_measurable"}
    good = len(runs) == 3 and all(
        r["m1"] and r["m2"] and r["m3"] and r["m4"] and r["m5_sampler"] for r in runs
    )
    if not good or not infer.get("m5_infer"):
        return {"id": "M-D3", "result": "measurement_failed"}
    return {"id": "M-D4", "result": "measurement_succeeded"}
