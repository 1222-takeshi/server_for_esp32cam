"""Supplementary cadence/hold gates for an independently verified continuous report.

Offline numbers only; does not certify clock/ACK evidence, restoration, accuracy,
thermal/resource safety or physical motor control. Never rewrites input evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPORT_LIMIT = 16 * 1024**2
MAX_UINT = 2**64 - 1


def uint(value, maximum=MAX_UINT):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid integer")
    return value


def evaluate(report):
    """Recalculate measurement windows and clipped natural hold time in integers."""
    if (
        any(
            report[k] is not value
            for k, value in {
                "continuous_tracking_policy": True,
                "continuous_tracking_gate_passed": True,
                "live_validation_only": True,
                "physical_motor_control": False,
                "global_acceptance": False,
            }.items()
        )
        or report["reason"] != "completed"
    ):
        raise ValueError("verified continuous report required")
    m = report["measurement"]
    start, end, duration = (uint(m[k]) for k in ("start_us", "end_us", "duration_us"))
    if duration not in (600_000_000, 3_600_000_000) or end - start != duration:
        raise ValueError("invalid duration")
    if (
        m["policy"] != "continuous-recovery-v1"
        or m["tracking_hold_statistics_scope"] != "full_tracking_phase"
    ):
        raise ValueError("ambiguous policy or hold scope")
    boot = uint(m["boot_id"])
    samples = report["samples"]
    if not boot or not isinstance(samples, list) or not 0 < len(samples) <= 65_536:
        raise ValueError("invalid samples")
    windows = [0] * (duration // 10_000_000)
    rows, previous_sequence, previous_at = [], 0, 0
    for row in samples:
        sequence, at, age, generation = (
            uint(row[k]) for k in ("sequence", "published_us", "age_high_us", "generation")
        )
        if uint(row["boot_id"]) != boot or sequence <= previous_sequence or at < previous_at:
            raise ValueError("invalid sample identity/order")
        if not 0 < generation <= 2**53 - 1 or age > 500_000:
            raise ValueError("invalid generation or freshness")
        previous_sequence, previous_at = sequence, at
        if start <= at < end:
            windows[(at - start) // 10_000_000] += 1
            rows.append(row)
    periods = m["tracking_holds"]
    if not isinstance(periods, list) or len(periods) > 128:
        raise ValueError("invalid holds")
    if any(
        uint(m[k]) != len(periods)
        for k in ("tracking_hold_events", "tracking_automatic_recoveries")
    ):
        raise ValueError("unrecovered or missing hold")
    injected = report["hold_injection_requested"]
    if type(injected) is not bool or m["injected_hold"] is not injected:
        raise ValueError("ambiguous injection")
    injection = m["injected_hold_at"]
    if injected:
        injected_at, injected_gen = map(uint, injection)
        if not start + 30_000_000 <= injected_at < end:
            raise ValueError("injection outside measurement")
    elif injection is not None:
        raise ValueError("unexpected injection")
    natural_us, injected_us, matches, previous_end = 0, 0, 0, 0
    generations = set()
    for generation, began, recovered in periods:
        generation, began, recovered = map(uint, (generation, began, recovered))
        if not 0 < generation <= 2**53 - 1 or generation in generations:
            raise ValueError("invalid hold generation")
        if (
            not previous_end <= began < recovered
            or not 10_000_000 <= recovered - began <= 30_000_000
        ):
            raise ValueError("invalid hold period")
        generations.add(generation)
        previous_end = recovered
        overlap = max(0, min(end, recovered) - max(start, began))
        if injected and (began, generation) == (injected_at, injected_gen):
            if recovered >= end or not any(
                r["generation"] == generation and r["published_us"] >= recovered for r in rows
            ):
                raise ValueError("injection recovery not measured")
            injected_us += overlap
            matches += 1
        else:
            natural_us += overlap
    if matches != int(injected):
        raise ValueError("injection does not identify exactly one hold")
    cadence_ok = min(windows) >= 50
    hold_ok = natural_us * 100 <= duration
    return {
        "schema": 1,
        "criteria": "pi-stable-cadence-hold-v1",
        "supplementary_only": True,
        "global_acceptance": False,
        "duration_us": duration,
        "results": len(rows),
        "recognition_hz": len(rows) * 1_000_000 / duration,
        "window_results": windows,
        "windows_below_50": sum(n < 50 for n in windows),
        "all_windows_5hz": cadence_ok,
        "natural_hold_us": natural_us,
        "injected_hold_us": injected_us,
        "natural_hold_budget_us": duration // 100,
        "natural_hold_within_one_percent": hold_ok,
        "passed": cadence_ok and hold_ok,
    }


def unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON key")
    return value


def load_report(path):
    with Path(path).open("rb") as source:
        raw = source.read(REPORT_LIMIT + 1)
    if len(raw) > REPORT_LIMIT:
        raise ValueError("report limit")

    def invalid_constant(_):
        raise ValueError("non-finite JSON")

    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(load_report(args.report))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print(json.dumps({"error": "invalid_report"}))
        return 2
    print(json.dumps(result, separators=(",", ":"), allow_nan=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
