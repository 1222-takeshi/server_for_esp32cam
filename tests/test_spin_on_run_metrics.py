from fractions import Fraction

import pytest

from scripts.spin_on_run import metrics, quality
from scripts.spin_on_run.common import ORDER

CLK = 100


def make_rows(infer, *, cpu_per_frame_ticks=15, gap=40_000, hwm=100_000, total_extra=500):
    """85 rows; `infer` gives the 80 measured values (warmup rows use 150000)."""
    rows, wall, ticks = [], 0, 0
    for i in range(85):
        value = 150_000 if i < 5 else infer[i - 5]
        wall += value + total_extra + (gap if i else 0)
        ticks += cpu_per_frame_ticks
        rows.append(
            {
                "sequence": i + 1,
                "infer_us": value,
                "total_us": value + total_extra,
                "wall_us": wall,
                "usage": {"cpu_ticks": ticks, "hwm_kib": hwm},
            }
        )
    return rows


def spread(base, tail=None):
    values = [base + (i % 10) * 100 for i in range(80)]
    if tail:
        values[10] = tail
        values[11] = tail
    return values


def make_runs(a_infer, b_infer, **b_args):
    return [
        {
            "condition": c,
            "rows": make_rows(a_infer if c == "A" else b_infer, **(b_args if c == "B" else {})),
        }
        for c in ORDER
    ]


def test_nearest_rank_matches_the_frozen_rank_table():
    assert [
        metrics.rank(q, n) for q, n in [(95, 640), (99, 640), (99, 160), (95, 80), (95, 240)]
    ] == [
        608,
        634,
        159,
        76,
        228,
    ]
    values = list(range(1, 641))
    assert metrics.nearest_rank(values, 99) == 634
    assert metrics.nearest_rank([5], 99) == 5
    with pytest.raises(metrics.MetricsError):
        metrics.nearest_rank([], 99)


def test_median_even_odd_and_exact():
    assert metrics.median([3, 1, 2]) == 2
    assert metrics.median([1, 2, 3, 10]) == Fraction(5, 2)


def test_run_metrics_cpu_replay_and_rss_use_the_exact_intervals():
    rows = make_rows(spread(150_000))
    got = metrics.run_metrics(rows, CLK)
    # 80 frames x 15 ticks at 100 Hz = 12 s -> 150000 us per frame.
    assert got["cpu_us_per_frame"] == Fraction(150_000)
    span = rows[84]["wall_us"] - rows[4]["wall_us"]
    assert got["replay_hz"] == Fraction(80_000_000, span)
    assert got["peak_rss_kib"] == 100_000 and len(got["infer"]) == 80


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows.pop(),
        lambda rows: rows[10].pop("infer_us"),
        lambda rows: rows[10].__setitem__("infer_us", 0),
        lambda rows: rows[10].__setitem__("infer_us", 1.5),
        lambda rows: rows[10].__setitem__("total_us", True),
        lambda rows: rows[10]["usage"].pop("cpu_ticks"),
        lambda rows: rows[84].__setitem__("sequence", 99),
        lambda rows: rows[84]["usage"].__setitem__("cpu_ticks", 0),
    ],
)
def test_missing_or_malformed_inputs_are_not_repaired(mutate):
    rows = make_rows(spread(150_000))
    mutate(rows)
    with pytest.raises(metrics.MetricsError):
        metrics.run_metrics(rows, CLK)


def test_gap_must_be_present_in_the_interval():
    for base in (1_000, 150_000):  # also caught when processing exceeds 80 x 40 ms
        rows = make_rows(spread(base), gap=0)
        with pytest.raises(metrics.MetricsError):
            metrics.run_metrics(rows, CLK)
    rows = make_rows(spread(150_000), gap=40_000)
    assert metrics.run_metrics(rows, CLK)["replay_hz"] > 0
    with pytest.raises(metrics.MetricsError):
        metrics.run_metrics(rows, 0)


def test_identical_conditions_are_not_an_improvement():
    runs = make_runs(spread(150_000), spread(150_000))
    m = metrics.evaluate(runs, CLK)
    assert m["pooled_p99_ratio"] == 1 and m["blocks_b_lt_a"] == 0 and m["noise_n"] == 0
    assert metrics.is_worse(m) is False


def test_clear_improvement_and_exact_boundaries():
    temp = {
        "temp_max_millic": 70_000,
        "throttled_all_zero": True,
        "temp_a_max": 65_000,
        "temp_b_max": 68_000,
    }
    a = spread(150_000, tail=300_000)
    b = spread(150_000, tail=150_000)
    m = metrics.evaluate(make_runs(a, b), CLK)
    assert m["pooled_p99_ratio"] <= Fraction(9, 10) and m["blocks_b_lt_a"] == 4
    assert metrics.is_improvement(m, temp, quality_ok=True, environment_ok=True)
    assert not metrics.is_improvement(m, temp, quality_ok=False, environment_ok=True)
    assert not metrics.is_improvement(m, temp, quality_ok=True, environment_ok=False)
    # Exactly +3000 m degC is allowed, +3001 is not.
    assert metrics.is_improvement(
        m, {**temp, "temp_b_max": 68_000}, quality_ok=True, environment_ok=True
    )
    assert not metrics.is_improvement(
        m, {**temp, "temp_b_max": 68_001}, quality_ok=True, environment_ok=True
    )
    assert not metrics.is_improvement(
        m, {**temp, "temp_max_millic": 80_000}, quality_ok=True, environment_ok=True
    )
    assert not metrics.is_improvement(
        m, {**temp, "throttled_all_zero": False}, quality_ok=True, environment_ok=True
    )


def test_worse_conditions_are_individually_decisive():
    base = metrics.evaluate(make_runs(spread(150_000), spread(150_000)), CLK)
    assert not metrics.is_worse(base)
    for key, value in [
        ("pooled_p99_ratio", Fraction(105, 100)),
        ("pooled_p95_ratio", Fraction(105, 100) + Fraction(1, 10_000)),
        ("pooled_mean_infer_ratio", Fraction(106, 100)),
        ("cpu_ratio", Fraction(126, 100)),
    ]:
        assert metrics.is_worse({**base, key: value}), key
    assert not metrics.is_worse({**base, "pooled_p95_ratio": Fraction(105, 100)})
    assert not metrics.is_worse({**base, "cpu_ratio": Fraction(125, 100)})
    # Tail rule needs both the 1.25x factor and a difference of at least 3.
    assert metrics.is_worse({**base, "tail_count_a": 8, "tail_count_b": 11})
    assert not metrics.is_worse({**base, "tail_count_a": 8, "tail_count_b": 10})
    assert not metrics.is_worse({**base, "tail_count_a": 0, "tail_count_b": 2})


def test_noise_guard_scales_the_required_block_improvement():
    runs = make_runs(spread(150_000, tail=300_000), spread(150_000, tail=150_000))
    m = metrics.evaluate(runs, CLK)
    temp = {"temp_max_millic": 1, "throttled_all_zero": True, "temp_a_max": 1, "temp_b_max": 1}
    noisy = {**m, "noise_n": Fraction(3, 10)}  # 2N = 0.6 > the observed improvement
    assert not metrics.is_improvement(noisy, temp, quality_ok=True, environment_ok=True)


def test_decision_table_priority_and_examples():
    ok = {"temp_max_millic": 1, "throttled_all_zero": True, "temp_a_max": 1, "temp_b_max": 1}
    m = metrics.evaluate(make_runs(spread(150_000), spread(150_000)), CLK)
    f = lambda codes, **kw: metrics.final_verdict(  # noqa: E731
        codes, kw.get("m", m), kw.get("t", ok), quality_ok=True, environment_ok=True
    )["id"]
    assert f(["O02", "O01"]) == "V1"  # A mismatch beats B mismatch
    assert f(["O01"]) == "V2"
    assert f(["O01", "O03"]) == "V2"
    assert f(["O01", "O06"]) == "V1"
    assert f(["O03"]) == "V3"
    assert f(["O03", "O06"]) == "V1"
    assert f(["O01", "O08"]) == "V1"
    assert f([], m=None) == "V4"
    assert f([]) == "V7"
    assert f([], m={**m, "pooled_p99_ratio": Fraction(2)}) == "V5"
    with pytest.raises(metrics.MetricsError):
        f(["O99"])


def test_temperature_windows_use_only_run_samples():
    samples = [
        {"t": 0, "temp_millic": 50_000, "throttled": 0},
        {"t": 5, "temp_millic": 90_000 - 20_000, "throttled": 0},  # between runs
        {"t": 11, "temp_millic": 60_000, "throttled": 0},
        {"t": 21, "temp_millic": 63_000, "throttled": 0},
    ]
    got = metrics.temperature_summary(samples, [("A", 10, 15), ("B", 20, 25)])
    assert (got["temp_a_max"], got["temp_b_max"], got["temp_max_millic"]) == (
        60_000,
        63_000,
        70_000,
    )
    assert metrics.temperature_summary(samples, [("A", 10, 15)])["temp_b_max"] is None
    with pytest.raises(metrics.MetricsError):
        metrics.temperature_summary([], [])


def test_printable_keeps_exact_rationals():
    out = metrics.printable({"x": Fraction(1, 3), "y": [Fraction(1, 2)], "z": 4})
    assert out["x"]["exact"] == "1/3" and out["y"][0]["approx"] == 0.5 and out["z"] == 4


GOLDEN = (
    '{"input_sha256":"a","raw_sha256":"b","detections":[{"score":0.5,"x1":0.25}],"runtime_build":"r",'
    '"sequence":1,"boot_id":2,"captured_us":3,"sent_us":4,"mode_generation":1,"replay_only":true,'
    '"freshness_checked":false,"actuation_state":"hold","infer_us":9}'
)


def test_quality_comparison_is_typed_and_float_exact():
    golden = quality.parse_line(GOLDEN)
    same = quality.parse_line(
        GOLDEN.replace('"infer_us":9', '"infer_us":777,"intra_op_spinning":true')
    )
    assert quality.mismatch(same, golden) is None  # extra fields are ignored
    for text, field in [
        (GOLDEN.replace("0.5", "0.5000001"), "detections"),
        (GOLDEN.replace('"sequence":1', '"sequence":1.0'), "sequence"),
        (GOLDEN.replace('"replay_only":true', '"replay_only":1'), "replay_only"),
        (GOLDEN.replace('"boot_id":2,', ""), "boot_id"),
        (GOLDEN.replace('"hold"', '"move"'), "actuation_state"),
    ]:
        assert quality.mismatch(quality.parse_line(text), golden) == field
    assert quality.parse_line('{"a":1.0}')["a"] != quality.parse_line('{"a":1}')["a"]
    with pytest.raises(ValueError):
        quality.parse_line('{"a":NaN}')
    with pytest.raises(ValueError):
        quality.parse_line("[1]")


def test_first_mismatch_reports_row_and_short_or_long_runs():
    golden = [quality.parse_line(GOLDEN)] * 3
    assert quality.first_mismatch(golden, golden) is None
    bad = [golden[0], quality.parse_line(GOLDEN.replace('"hold"', '"x"')), golden[2]]
    assert quality.first_mismatch(bad, golden) == (1, "actuation_state")
    assert quality.first_mismatch(golden[:2], golden) == (2, "<missing row>")
    assert quality.first_mismatch(golden + golden[:1], golden) == (3, "<extra row>")
    with pytest.raises(ValueError):
        quality.load_golden(GOLDEN + "\n")
