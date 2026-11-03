"""Published-interval criterion for the live trial (v2 spec D5; criteria-proposal sections 1-3).

Computed after the run from the parsed `native.json` only. The gate (C) is
`p99 <= 250 ms` over the normal intervals N2 with a minimum sample count; the
target (A, report only) adds `max <= 300 ms`. Percentiles use linear
interpolation on exact rationals so a float rounding cannot move a boundary.
"""

from __future__ import annotations

from fractions import Fraction

GATE_P99_US = 250_000
TARGET_MAX_US = 300_000
CROSS_GENERATION_EXCLUDE_US = 500_000
# Startup/teardown gaps of a report without a measurement interval; used only by the
# regression reproduction of saved runs, never by a live trial (which always has one).
NO_WINDOW_EDGE_GAP_US = 4_500_000
THRESHOLDS_US = (250_000, 300_000, 350_000)
DERIVATION = (
    "freshness derivation: 500 ms - 180.5 ms (observed maximum of the age term B over "
    "stretched intervals) = 319.5 ms, so the limit is below 319.5 ms and is truncated to "
    "300 ms (criteria-proposal section 1)"
)


def min_intervals(seconds: int) -> int:
    """4.5 intervals per second: 2,700 for 600 s, 16,200 for 3,600 s."""
    return seconds * 9 // 2


def percentile(values: list[int], p_num: int, p_den: int = 100) -> Fraction | None:
    """Linear-interpolated percentile `k = (n - 1) * p`, exact. None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    index, rest = divmod((len(ordered) - 1) * p_num, p_den)
    upper = ordered[min(index + 1, len(ordered) - 1)]
    return Fraction(ordered[index]) + Fraction((upper - ordered[index]) * rest, p_den)


def normal_intervals(
    samples: list[dict], start: int | None, end: int | None
) -> tuple[list[int], dict[str, int]]:
    """N2: the intervals D (microseconds) and what was left out.

    Unique results (key `(boot_id, sequence)`, first kept) inside `[start, end)` in
    `published_us` order; each adjacent pair is one interval. Only a pair of different
    generations with D over 500 ms is left out (the hold-onset gap); a same-generation
    D of any size and a cross-generation D of at most 500 ms stay in. With no window
    (`start` None) all results are used and gaps of 4.5 s or more are also left out.
    """
    seen: set[tuple[int, int]] = set()
    unique = []
    for sample in samples:
        key = (sample["boot_id"], sample["sequence"])
        if key not in seen:
            seen.add(key)
            unique.append(sample)
    if start is not None:
        unique = [s for s in unique if start <= s["published_us"] < end]
    unique.sort(key=lambda s: (s["published_us"], s["sequence"]))
    excluded = {"cross_generation_gap_gt500ms": 0, "edge_gap_no_measurement_window": 0}
    gaps: list[int] = []
    for first, second in zip(unique, unique[1:], strict=False):
        gap = second["published_us"] - first["published_us"]
        same = (first["boot_id"], first["generation"]) == (second["boot_id"], second["generation"])
        if start is None and gap >= NO_WINDOW_EDGE_GAP_US:
            excluded["edge_gap_no_measurement_window"] += 1
        elif not same and gap > CROSS_GENERATION_EXCLUDE_US:
            excluded["cross_generation_gap_gt500ms"] += 1
        else:
            gaps.append(gap)
    return gaps, excluded


def _count(report: dict, *path: str) -> int | None:
    node: object = report
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if type(node) is int else None


def summarize(gaps: list[int], seconds: int) -> dict:
    """Statistics, gate (C) and target (A) for already computed intervals."""
    n = len(gaps)
    p99 = percentile(gaps, 99)
    over = {f"gt{limit // 1000}": sum(1 for g in gaps if g > limit) for limit in THRESHOLDS_US}
    needed = min_intervals(seconds)
    gate = "unjudgeable" if n < needed else "pass" if p99 <= GATE_P99_US else "fail"
    met = p99 is not None and p99 <= GATE_P99_US and over["gt300"] == 0
    return {
        "n": n,
        "min_intervals": needed,
        "p99_us": None if p99 is None else float(p99),
        "p99_us_exact": None if p99 is None else f"{p99.numerator}/{p99.denominator}",
        "max_us": max(gaps) if gaps else None,
        "over_us": over,
        "gate": gate,
        "target": "met" if met else "not_met",
    }


def evaluate(report: object, seconds: int) -> dict:
    """The `interval-gate.json` body for a finished run.

    Applies to a report with reason `completed`; any other report returns
    `{"applicable": False}`. A completed report whose samples or measurement cannot be
    read has gate `unjudgeable` (the schema fault itself is F06).
    """
    if not isinstance(report, dict) or report.get("reason") != "completed":
        return {"applicable": False}
    out: dict = {
        "applicable": True,
        "seconds": seconds,
        "gate_rule": "p99 <= 250 ms over N2, at least 4.5 x seconds intervals",
        "target_rule": "p99 <= 250 ms and no interval over 300 ms (report only)",
        "derivation": DERIVATION,
        "queue_busy": _count(report, "receiver", "queue_busy"),
        "receiver_dropped": _count(report, "receiver", "dropped"),
        "worker_dropped": _count(report, "worker", "dropped"),
    }
    measurement, samples = report.get("measurement"), report.get("samples")
    try:
        start, end = measurement["start_us"], measurement["end_us"]
        if type(start) is not int or type(end) is not int or not isinstance(samples, list):
            raise TypeError("window")
        gaps, excluded = normal_intervals(samples, start, end)
    except (KeyError, TypeError, ValueError):
        return {**out, "gate": "unjudgeable", "target": "not_met", "n": 0, "error": "schema"}
    return {**out, **summarize(gaps, seconds), "excluded": excluded}
