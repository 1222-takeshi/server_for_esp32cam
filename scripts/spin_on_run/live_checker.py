"""Live trial settlement: abort codes, post-run failure codes, final result (plan sections 8, 9.3).

Pure functions over the parsed `native.json`, the guardian's end marker and the
unit's stop information. The report is judged only after the child has exited
and the file is closed; nothing here is used while the trial is running.
"""

from __future__ import annotations

from collections import Counter

WINDOW_US = 10_000_000
MIN_RECOGNITIONS = 50
AGE_LIMIT_US = 500_000
CONTINUOUS_HOLD_US = 15_000_000
HOLD_BUDGET_US = {600: 6_000_000, 3_600: 36_000_000}
DURATION_US = {600: 600_000_000, 3_600: 3_600_000_000}

# Plan name -> actual report field (the plan fixes the names, the code owns the fields).
FIELD_MAP = {
    "$.reason": "reason",
    "$.finite_live_gate_passed": "continuous_tracking_gate_passed",
    "$.tracking_recovery_reason": "session_final.reason",
    "$.tracking_auto_recovery_pending": "session_final.tracking_auto_recovery_pending",
    "$.generation_epochs": "measurement.generation_epochs[*][1]",
    "$.tracking_hold_events": "measurement.tracking_hold_events",
    "$.tracking_automatic_recoveries": "measurement.tracking_automatic_recoveries",
    "measurement.tracking_holds[*]": "[generation, began_us, recovered_us]",
}
ONLINE_REASONS = {
    "continuous_hold": "A11",
    "natural_hold_budget_exceeded": "A12",
    "window_below_50": "A13",
    "protection_violation": "A14",
}
ABORT_ORDER = [f"A{n:02d}" for n in range(1, 15)]
FAIL_ORDER = [f"F{n:02d}" for n in range(1, 8)]
MARKER_ONLY = {"A06", "A10"}


def min_window_count(times: list[int], start: int, end: int) -> int | None:
    """Smallest count over every window `[t, t + 10 s)` with integer `start <= t <= end - 10 s`.

    The count only falls right after a result time, so `start` and each `p + 1`
    are the only candidates. None when the interval is shorter than one window.
    """
    last = end - WINDOW_US
    if last < start:
        return None
    ordered = sorted(times)

    def count(t: int) -> int:
        lo = _bisect_left(ordered, t)
        return _bisect_left(ordered, t + WINDOW_US) - lo

    candidates = [start] + [p + 1 for p in ordered if start <= p + 1 <= last]
    return min(count(t) for t in candidates)


def _bisect_left(values: list[int], target: int) -> int:
    lo, hi = 0, len(values)
    while lo < hi:
        mid = (lo + hi) // 2
        if values[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _is_int(value: object) -> bool:
    return type(value) is int


def hold_stats(holds: list, start: int, end: int) -> tuple[int, int]:
    """`(natural total, longest continuous)` in microseconds for holds touching the interval."""
    total = longest = 0
    for _generation, began, recovered in holds:
        if recovered <= start or began >= end:
            continue
        total += max(0, min(recovered, end) - max(began, start))
        longest = max(longest, min(recovered, end) - began)
    return total, longest


def check_report(report: dict | None, seconds: int) -> dict:
    """Failure codes F01-F07 for a finished trial; `report` None means unreadable."""
    codes: list[str] = []
    notes: list[str] = []

    def add(code: str, note: str) -> None:
        if code not in codes:
            codes.append(code)
        notes.append(f"{code}:{note}")

    if not isinstance(report, dict):
        add("F01", "native.json unreadable")
        add("F06", "native.json empty or malformed")
        return {"codes": _ordered(codes, FAIL_ORDER), "notes": notes, "windows": None}
    if (
        report.get("reason") != "completed"
        or report.get("continuous_tracking_gate_passed") is not True
    ):
        add("F01", f"reason={report.get('reason')!r}")
    measurement = report.get("measurement")
    samples = report.get("samples")
    windows = None
    if report.get("hold_injection_requested") or (
        isinstance(measurement, dict) and measurement.get("injected_hold")
    ):
        add("F06", "injection recorded")
    if not isinstance(measurement, dict) or not isinstance(samples, list):
        return {"codes": _ordered(codes, FAIL_ORDER), "notes": notes, "windows": windows}
    try:
        start, end = measurement["start_us"], measurement["end_us"]
        if not (_is_int(start) and _is_int(end)) or end - start != DURATION_US[seconds]:
            raise ValueError("interval")
        epochs = measurement["generation_epochs"]
        holds = measurement["tracking_holds"]
        inside = [s for s in samples if start <= s["published_us"] < end]
        keys = Counter((s["boot_id"], s["sequence"]) for s in samples)
        if any(n > 1 for n in keys.values()):
            add("F06", "duplicate (boot_id, sequence)")
        sequences = [s["sequence"] for s in samples]
        if any(b <= a for a, b in zip(sequences, sequences[1:], strict=False)):
            add("F06", "sequence not increasing")
        expected_boot = inside[0]["boot_id"] if inside else None
        for s in inside:
            owner = [g for at, g in epochs if at <= s["published_us"]]
            if s["boot_id"] != expected_boot or not owner or s["generation"] != owner[-1]:
                add("F06", f"boot/generation mismatch at sequence {s['sequence']}")
                break
        tumbling = [0] * (DURATION_US[seconds] // WINDOW_US)
        for s in inside:
            tumbling[(s["published_us"] - start) // WINDOW_US] += 1
        if list(measurement["window_results"]) != tumbling or measurement.get("results") != len(
            inside
        ):
            add("F06", "native counts differ from the samples")
        times = [s["published_us"] for s in inside]
        low = min_window_count(times, start, end)
        windows = {"tumbling_min": min(tumbling), "any_start_min": low}
        if low is None or low < MIN_RECOGNITIONS:
            add("F02", f"window minimum {low}")
        total, longest = hold_stats(holds, start, end)
        windows.update(natural_hold_us=total, longest_hold_us=longest)
        if total > HOLD_BUDGET_US[seconds]:
            add("F03", f"natural hold {total}")
        if longest >= CONTINUOUS_HOLD_US:
            add("F04", f"continuous hold {longest}")
        if any(not _is_int(s["age_high_us"]) or s["age_high_us"] > AGE_LIMIT_US for s in samples):
            add("F05", "age_high_us over 500000")
        generations = [g for _at, g in epochs]
        pending = (report.get("session_final") or {}).get("tracking_auto_recovery_pending")
        recovery_reason = (report.get("session_final") or {}).get("reason")
        if (
            any(b != a + 1 for a, b in zip(generations, generations[1:], strict=False))
            or measurement["tracking_hold_events"] != measurement["tracking_automatic_recoveries"]
            or measurement["tracking_hold_events"] != len(holds)
            or pending is not False
            or recovery_reason is not None
        ):
            add("F07", "recovery is not a new-generation ACK recovery")
    except (KeyError, TypeError, ValueError, IndexError) as error:
        add("F06", f"schema: {type(error).__name__}")
    return {"codes": _ordered(codes, FAIL_ORDER), "notes": notes, "windows": windows}


def _ordered(codes: list[str], order: list[str]) -> list[str]:
    return [c for c in order if c in codes]


def abort_codes(
    *,
    marker: str | None,
    service_result: str | None,
    report: dict | None,
    child_exit: int | None,
    child_signalled: bool,
    guardian_stopped_child: bool,
) -> list[str]:
    """Abort codes in recorded order: the guardian marker, then the child's end state."""
    codes: list[str] = []
    if marker and marker.startswith("abort "):
        codes += [c for c in marker.split(" ", 1)[1].split(",") if c in ABORT_ORDER]
    if marker is None:
        codes.append("A10" if service_result == "timeout" else "A06")
    reason = report.get("reason") if isinstance(report, dict) else None
    if child_exit == 20 and reason in ONLINE_REASONS:
        codes.append(ONLINE_REASONS[reason])
    elif (child_exit not in (0, 3, None) or child_signalled) and not guardian_stopped_child:
        codes.append("A07")
    return list(dict.fromkeys(codes))  # recorded order: the first one is the primary reason


def final_result(aborts: list[str], failures: list[str], *, restore_ok: bool) -> dict:
    """Aborts outrank failures; a pass with a failed restore is not a milestone."""
    if aborts:
        return {"result": "aborted", "primary": aborts[0], "milestone": False}
    if failures:
        return {"result": "failed", "primary": failures[0], "milestone": False}
    if not restore_ok:
        return {"result": "passed_restore_failed", "primary": None, "milestone": False}
    return {"result": "passed", "primary": None, "milestone": True}
