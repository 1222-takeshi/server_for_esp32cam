"""Offline S metrics and the frozen decision table (plan sections 5.4, 5.5, 9.1).

All ratios are exact `Fraction`s and every comparison is an exact rational
comparison. Missing, non-integer or non-finite inputs raise `MetricsError`,
which the caller reports as an indeterminate result instead of repairing it.
"""

from __future__ import annotations

from fractions import Fraction

from .common import (
    BLOCKS,
    FRAME_GAP_US,
    GAP_COUNT,
    MEASURED_ROWS,
    ORDER,
    ROWS_PER_RUN,
    WARMUP_ROWS,
)

TAIL_US = 200_000
RSS_GROWTH_MAX_KIB = 32_768
TEMP_DELTA_MAX_MILLIC = 3_000
TEMP_LIMIT_MILLIC = 80_000


class MetricsError(ValueError):
    """An input needed for a judgement is missing or malformed."""


class FieldError(MetricsError):
    """A required per-row field is missing, malformed or contradicts the run (plan O06).

    Other `MetricsError`s concern the pooled result only and make it V4.
    """


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or (positive and value <= 0) or value < 0:
        raise FieldError(f"{name} is not a usable integer")
    return value


def rank(q: int, n: int) -> int:
    return (q * n + 99) // 100


def nearest_rank(values: list[int], q: int) -> int:
    if not values:
        raise MetricsError("empty distribution")
    return sorted(values)[rank(q, len(values)) - 1]


def median(values: list[Fraction | int]) -> Fraction:
    if not values:
        raise MetricsError("empty median")
    ordered = sorted(Fraction(v) for v in values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def ratio(numerator: Fraction | int, denominator: Fraction | int) -> Fraction:
    if denominator == 0:
        raise MetricsError("zero denominator")
    return Fraction(numerator) / Fraction(denominator)


def run_metrics(rows: list[dict], clk_tck: int, *, gap_us: int = FRAME_GAP_US) -> dict:
    """Per-run values from the 85 rows of one run."""
    if len(rows) != ROWS_PER_RUN:
        raise FieldError("row count")
    _integer(clk_tck, "SC_CLK_TCK", positive=True)
    infer, total, hwm = [], [], []
    for index, row in enumerate(rows):
        usage = row.get("usage")
        if not isinstance(usage, dict):
            raise FieldError("usage missing")
        _integer(usage.get("cpu_ticks"), "cpu_ticks")
        _integer(row.get("wall_us"), "wall_us")
        if index >= WARMUP_ROWS:
            infer.append(_integer(row.get("infer_us"), "infer_us", positive=True))
            total.append(_integer(row.get("total_us"), "total_us", positive=True))
            hwm.append(_integer(usage.get("hwm_kib"), "hwm_kib"))
        else:
            _integer(row.get("infer_us"), "infer_us", positive=True)
            _integer(row.get("total_us"), "total_us", positive=True)
    first, last = rows[WARMUP_ROWS - 1], rows[ROWS_PER_RUN - 1]
    if _integer(last.get("sequence"), "sequence") - _integer(first.get("sequence"), "sequence") != (
        MEASURED_ROWS
    ):
        raise FieldError("interval is not 80 frames")
    span = last["wall_us"] - first["wall_us"]
    # The plan's bound, plus the tighter one that also detects a missing gap when
    # processing alone already exceeds 80 x 40 ms: the interval covers the 80
    # processed frames and their 80 sleeps, each of at least `gap_us`.
    if span < GAP_COUNT * gap_us or span < sum(total) + GAP_COUNT * gap_us:
        raise FieldError("frame gap missing")
    delta = last["usage"]["cpu_ticks"] - first["usage"]["cpu_ticks"]
    if delta < 0:
        raise FieldError("cpu ticks decreased")
    return {
        "infer": infer,
        "total": total,
        "p95_infer": nearest_rank(infer, 95),
        "cpu_us_per_frame": Fraction(delta * 1_000_000, clk_tck * MEASURED_ROWS),
        "replay_hz": Fraction(MEASURED_ROWS * 1_000_000, span),
        "peak_rss_kib": max(hwm),
    }


def _mean(values: list[int]) -> Fraction:
    return Fraction(sum(values), len(values))


def evaluate(runs: list[dict], clk_tck: int, *, gap_us: int = FRAME_GAP_US) -> dict:
    """`runs` are `{"condition": "A"|"B", "rows": [...]}` in execution order."""
    if "".join(r["condition"] for r in runs) != ORDER:
        raise MetricsError("run order")
    per = [run_metrics(r["rows"], clk_tck, gap_us=gap_us) for r in runs]
    pooled = {c: [] for c in "AB"}
    totals = {c: [] for c in "AB"}
    for run, m in zip(runs, per, strict=True):
        pooled[run["condition"]] += m["infer"]
        totals[run["condition"]] += m["total"]
    if any(len(v) != 8 * MEASURED_ROWS for v in pooled.values()):
        raise MetricsError("pooled sample size")
    a, b = pooled["A"], pooled["B"]
    block_p99 = {"A": [], "B": []}
    improvements, noise = [], []
    for k, pattern in enumerate(BLOCKS):
        members = per[4 * k : 4 * k + 4]
        values = {c: [] for c in "AB"}
        p95 = []
        for cond, member in zip(pattern, members, strict=True):
            values[cond] += member["infer"]
            if cond == "A":
                p95.append(member["p95_infer"])
        if any(len(v) != 2 * MEASURED_ROWS for v in values.values()):
            raise MetricsError("block composition")
        pa, pb = nearest_rank(values["A"], 99), nearest_rank(values["B"], 99)
        block_p99["A"].append(pa)
        block_p99["B"].append(pb)
        improvements.append(1 - ratio(pb, pa))
        noise.append(ratio(2 * abs(p95[0] - p95[1]), p95[0] + p95[1]))

    def condition_mean(key: str, cond: str) -> Fraction:
        picked = [m[key] for r, m in zip(runs, per, strict=True) if r["condition"] == cond]
        return sum(picked, Fraction(0)) / len(picked)

    def peak(cond: str) -> int:
        return max(
            m["peak_rss_kib"] for r, m in zip(runs, per, strict=True) if r["condition"] == cond
        )

    return {
        "pooled_p99_ratio": ratio(nearest_rank(b, 99), nearest_rank(a, 99)),
        "pooled_p95_ratio": ratio(nearest_rank(b, 95), nearest_rank(a, 95)),
        "pooled_mean_infer_ratio": ratio(_mean(b), _mean(a)),
        "pooled_mean_total_ratio": ratio(_mean(totals["B"]), _mean(totals["A"])),
        "pooled_max_ratio": ratio(max(b), max(a)),
        "tail_count_a": sum(v > TAIL_US for v in a),
        "tail_count_b": sum(v > TAIL_US for v in b),
        "cpu_ratio": ratio(
            condition_mean("cpu_us_per_frame", "B"), condition_mean("cpu_us_per_frame", "A")
        ),
        "replay_hz_ratio": ratio(
            condition_mean("replay_hz", "B"), condition_mean("replay_hz", "A")
        ),
        "rss_growth_kib": peak("B") - peak("A"),
        "block_p99_a": block_p99["A"],
        "block_p99_b": block_p99["B"],
        "block_improvements": improvements,
        "block_median_imp": median(improvements),
        "blocks_b_lt_a": sum(x < y for x, y in zip(block_p99["B"], block_p99["A"], strict=True)),
        "noise_n": median(noise),
    }


def temperature_summary(samples: list[dict], windows: list[tuple[str, float, float]]) -> dict:
    """`samples`: `{t, temp_millic, throttled}`; `windows`: (condition, start, end)."""
    if not samples:
        raise MetricsError("no vitals")
    peaks: dict = {}
    for cond, start, end in windows:
        inside = [s["temp_millic"] for s in samples if start <= s["t"] <= end]
        if inside:
            peaks[cond] = max(inside + [peaks.get(cond, inside[0])])
    return {
        "temp_max_millic": max(s["temp_millic"] for s in samples),
        "throttled_all_zero": all(s["throttled"] == 0 for s in samples),
        "temp_a_max": peaks.get("A"),
        "temp_b_max": peaks.get("B"),
    }


def is_worse(m: dict) -> bool:
    return (
        m["pooled_p99_ratio"] >= Fraction(105, 100)
        or m["pooled_p95_ratio"] > Fraction(105, 100)
        or m["pooled_mean_infer_ratio"] > Fraction(105, 100)
        or m["cpu_ratio"] > Fraction(125, 100)
        or (
            m["tail_count_b"] * 100 > m["tail_count_a"] * 125
            and m["tail_count_b"] - m["tail_count_a"] >= 3
        )
    )


def is_improvement(m: dict, temp: dict, *, quality_ok: bool, environment_ok: bool) -> bool:
    if temp["temp_a_max"] is None or temp["temp_b_max"] is None:
        raise MetricsError("no temperature inside a condition")
    threshold = max(Fraction(1, 10), 2 * m["noise_n"])
    return (
        quality_ok
        and m["pooled_p99_ratio"] <= Fraction(90, 100)
        and m["blocks_b_lt_a"] == 4
        and m["block_median_imp"] >= threshold
        and m["pooled_p95_ratio"] <= Fraction(103, 100)
        and m["pooled_mean_infer_ratio"] <= Fraction(103, 100)
        and m["pooled_mean_total_ratio"] <= Fraction(103, 100)
        and m["pooled_max_ratio"] <= Fraction(110, 100)
        and m["tail_count_b"] <= m["tail_count_a"]
        and m["cpu_ratio"] <= Fraction(125, 100)
        and m["replay_hz_ratio"] >= Fraction(97, 100)
        and m["rss_growth_kib"] <= RSS_GROWTH_MAX_KIB
        and temp["temp_max_millic"] < TEMP_LIMIT_MILLIC
        and temp["throttled_all_zero"]
        and temp["temp_b_max"] - temp["temp_a_max"] <= TEMP_DELTA_MAX_MILLIC
        and environment_ok
    )


STOP_RESULT_ROW = {
    "O02": "V1",
    "O04": "V1",
    "O05": "V1",
    "O06": "V1",
    "O07": "V1",
    "O08": "V1",
    "O01": "V2",
    "O03": "V3",
}


def final_verdict(
    codes: list[str],
    metrics: dict | None,
    temp: dict | None,
    *,
    quality_ok: bool,
    environment_ok: bool,
) -> dict:
    """First matching row of the decision table; the stop-code set is the only input."""
    unknown = [c for c in codes if c not in STOP_RESULT_ROW]
    if unknown:
        raise MetricsError(f"unknown stop code {unknown}")
    present = {STOP_RESULT_ROW[c] for c in codes}
    if "V1" in present:
        return {"id": "V1", "result": "indeterminate"}
    if "V2" in present:
        return {"id": "V2", "result": "worse_quality_mismatch"}
    if "V3" in present:
        return {"id": "V3", "result": "indeterminate_thermal"}
    if metrics is None or temp is None:
        return {"id": "V4", "result": "indeterminate"}
    try:
        worse = is_worse(metrics)
        improved = not worse and is_improvement(
            metrics, temp, quality_ok=quality_ok, environment_ok=environment_ok
        )
    except (MetricsError, KeyError, TypeError):
        return {"id": "V4", "result": "indeterminate"}
    if worse:
        return {"id": "V5", "result": "worse"}
    if improved:
        return {"id": "V6", "result": "improvement"}
    return {"id": "V7", "result": "indeterminate_intermediate"}


def printable(metrics: dict) -> dict:
    """JSON-safe copy: each rational as `{"exact": "n/d", "approx": float}`."""

    def convert(value: object) -> object:
        if isinstance(value, Fraction):
            return {"exact": f"{value.numerator}/{value.denominator}", "approx": float(value)}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    return {key: convert(value) for key, value in metrics.items()}
