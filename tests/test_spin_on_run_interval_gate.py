import json
import os
from pathlib import Path

import pytest

from scripts.spin_on_run import interval_gate as ig
from scripts.spin_on_run import live_checker as lc

START = 1_000_000_000
NORMAL = 200_000


def samples_from(gaps, start=START, generations=None):
    """Published results whose successive differences are `gaps` (µs).

    `generations[i]` is the generation of result i (default 1 for all).
    """
    times = [start]
    for gap in gaps:
        times.append(times[-1] + gap)
    generations = generations or [1] * len(times)
    return [
        {
            "boot_id": 9,
            "generation": generations[i],
            "sequence": i + 1,
            "published_us": t,
            "age_high_us": 300_000,
            "compute_us": 1,
        }
        for i, t in enumerate(times)
    ]


def report_from(samples, seconds=600, start=START, **extra):
    return {
        "reason": "completed",
        "continuous_tracking_gate_passed": True,
        "samples": samples,
        "measurement": {"start_us": start, "end_us": start + seconds * 1_000_000},
        **extra,
    }


# --- percentile -------------------------------------------------------------------


def test_percentile_is_exact_linear_interpolation():
    assert ig.percentile([], 99) is None
    assert ig.percentile([7], 99) == 7
    assert ig.percentile(list(range(101)), 99) == 99  # k = 99, no interpolation
    # n = 4: k = 3 * 0.99 = 2.97 -> 20 + (40 - 20) * 0.97 = 39.4 = 197/5, order-independent
    assert str(ig.percentile([10, 20, 20, 40], 99)) == "197/5"
    assert ig.percentile([40, 20, 10, 20], 99) == ig.percentile([10, 20, 20, 40], 99)


# --- boundary of the p99 gate ------------------------------------------------------


@pytest.mark.parametrize(
    ("tail", "gate"),
    [
        ((250_000, 250_000), "pass"),  # p99 is exactly 250,000
        ((250_000, 250_099), "fail"),  # 250,000.99: only the interpolation puts it over
        ((250_000, 250_100), "fail"),  # 250,001
        ((249_999, 250_000), "pass"),
    ],
)
def test_p99_boundary_at_the_exact_threshold(tail, gate):
    # n = 2700: k = 2699 * 99 / 100 = 2672 + 1/100, so the 99th percentile lies between
    # sorted (zero based) values 2672 and 2673. Put those two at the tail values and keep
    # everything above them at least as large.
    gaps = [NORMAL] * 2672 + list(tail) + [max(tail)] * 26
    assert len(gaps) == 2700
    out = ig.evaluate(report_from(samples_from(gaps)), 600)
    assert out["n"] == 2700
    assert out["gate"] == gate, out["p99_us_exact"]


def test_every_interval_exactly_250ms_passes_and_one_microsecond_more_fails():
    for gap, gate in ((250_000, "pass"), (250_001, "fail")):
        out = ig.evaluate(report_from(samples_from([gap] * 2700), seconds=700), 600)
        assert out["n"] == 2700 and out["gate"] == gate and out["p99_us"] == float(gap)


# --- minimum count -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "count", "gate"),
    [
        (600, 2699, "unjudgeable"),
        (600, 2700, "pass"),
        (3600, 16_199, "unjudgeable"),
        (3600, 16_200, "pass"),
    ],
)
def test_minimum_interval_count(seconds, count, gate):
    out = ig.evaluate(report_from(samples_from([NORMAL] * count), seconds=seconds + 100), seconds)
    assert out["n"] == count and out["min_intervals"] == seconds * 9 // 2
    assert out["gate"] == gate


def test_a_failing_p99_with_too_few_intervals_is_still_unjudgeable():
    out = ig.evaluate(report_from(samples_from([400_000] * 100), seconds=600), 600)
    assert out["gate"] == "unjudgeable" and out["target"] == "not_met"


# --- exclusion rule (N2) -----------------------------------------------------------


def two_generation_samples(gap):
    gaps = [NORMAL] * 5 + [gap] + [NORMAL] * 5
    return samples_from(gaps, generations=[1] * 6 + [2] * 6)


def test_only_a_cross_generation_gap_over_500ms_is_excluded():
    gaps, excluded = ig.normal_intervals(two_generation_samples(500_001), START, START + 10**10)
    assert len(gaps) == 10 and 500_001 not in gaps
    assert excluded["cross_generation_gap_gt500ms"] == 1


def test_a_cross_generation_gap_of_500ms_or_less_is_kept():
    gaps, excluded = ig.normal_intervals(two_generation_samples(500_000), START, START + 10**10)
    assert 500_000 in gaps and len(gaps) == 11
    assert excluded["cross_generation_gap_gt500ms"] == 0


def test_a_same_generation_gap_over_500ms_is_kept():
    samples = samples_from([NORMAL] * 5 + [900_000] + [NORMAL] * 5)
    gaps, excluded = ig.normal_intervals(samples, START, START + 10**10)
    assert 900_000 in gaps and len(gaps) == 11
    assert excluded["cross_generation_gap_gt500ms"] == 0


def test_results_outside_the_window_and_duplicate_keys_are_not_intervals():
    samples = samples_from([NORMAL] * 10)
    samples.append(dict(samples[3]))  # duplicate (boot_id, sequence)
    samples.insert(0, {**samples[0], "sequence": 99, "published_us": START - 5_000_000})
    gaps, _ = ig.normal_intervals(samples, START, START + 1_000_000)
    assert gaps == [NORMAL] * 4  # five results lie in [START, START + 1 s)


def test_results_are_ordered_by_published_time_before_pairing():
    samples = list(reversed(samples_from([NORMAL, 300_000, NORMAL])))
    gaps, _ = ig.normal_intervals(samples, START, START + 10**9)
    assert gaps == [NORMAL, 300_000, NORMAL]


# --- statistics, target A, derivation ----------------------------------------------


def test_counts_over_each_threshold_and_target_a():
    tail = (250_001, 300_000, 300_001, 350_000, 350_001)
    gaps = [NORMAL] * (2700 - len(tail)) + list(tail)
    out = ig.evaluate(report_from(samples_from(gaps), seconds=700), 600)
    assert out["over_us"] == {"gt250": 5, "gt300": 3, "gt350": 1}  # strictly greater
    assert out["max_us"] == 350_001 and out["gate"] == "pass"
    assert out["target"] == "not_met"  # intervals over 300 ms exist although the gate passes
    ok = [NORMAL] * 2699 + [300_000]
    met = ig.evaluate(report_from(samples_from(ok), seconds=700), 600)
    assert met["target"] == "met" and met["over_us"]["gt300"] == 0


def test_target_a_requires_the_p99_condition_too():
    out = ig.evaluate(report_from(samples_from([280_000] * 2700), seconds=800), 600)
    assert out["gate"] == "fail" and out["target"] == "not_met"
    assert out["over_us"]["gt300"] == 0


def test_report_carries_derivation_and_native_counters():
    report = report_from(
        samples_from([NORMAL] * 2700),
        receiver={"queue_busy": 3, "dropped": 5},
        worker={"dropped": 45},
    )
    out = ig.evaluate(report, 600)
    assert (out["queue_busy"], out["receiver_dropped"], out["worker_dropped"]) == (3, 5, 45)
    assert "319.5" in out["derivation"] and "300 ms" in out["derivation"]
    json.dumps(out)  # serialisable as it is
    bare = ig.evaluate(report_from(samples_from([NORMAL] * 2700)), 600)
    assert bare["queue_busy"] is bare["receiver_dropped"] is bare["worker_dropped"] is None


def test_unreadable_samples_in_a_completed_report_are_unjudgeable_not_a_crash():
    out = ig.evaluate({"reason": "completed", "samples": "x", "measurement": {}}, 600)
    assert out["applicable"] and out["gate"] == "unjudgeable" and out["error"] == "schema"


# --- scope and priority in the live checker ------------------------------------------


def live_report(seconds=600, gap_at=lambda i: 190_000):
    """A report that meets every existing condition; results fill the whole window."""
    end = START + seconds * 1_000_000
    times, i = [START], 0
    while times[-1] + gap_at(i) < end:
        times.append(times[-1] + gap_at(i))
        i += 1
    samples = [
        {
            "boot_id": 9,
            "generation": 1,
            "sequence": n + 1,
            "published_us": t,
            "age_high_us": 300_000,
            "compute_us": 1,
        }
        for n, t in enumerate(times)
    ]
    tumbling = [0] * (seconds // 10)
    for s in samples:
        tumbling[(s["published_us"] - START) // 10_000_000] += 1
    return {
        "reason": "completed",
        "continuous_tracking_gate_passed": True,
        "finite_live_gate_passed": False,
        "hold_injection_requested": False,
        "session_final": {"reason": None, "tracking_auto_recovery_pending": False},
        "samples": samples,
        "measurement": {
            "start_us": START,
            "end_us": end,
            "results": len(samples),
            "window_results": tumbling,
            "generation_epochs": [[START, 1]],
            "tracking_holds": [],
            "tracking_hold_events": 0,
            "tracking_automatic_recoveries": 0,
            "injected_hold": False,
        },
    }


def slow_every_40th(i):
    return 280_000 if i % 40 == 0 else 190_000  # 2.5% of the intervals are over 250 ms


def test_a_clean_run_is_judged_and_passes():
    out = lc.check_report(live_report(), 600)
    assert out["codes"] == [] and out["interval"]["gate"] == "pass"
    assert out["interval_unjudgeable"] is False
    assert lc.check_report(live_report(3600), 3600)["codes"] == []


def test_f08_is_the_only_new_code_for_a_run_that_meets_every_existing_condition():
    out = lc.check_report(live_report(gap_at=slow_every_40th), 600)
    assert out["codes"] == ["F08"], out["notes"]
    assert out["interval"]["gate"] == "fail" and out["interval"]["target"] == "not_met"


def test_f08_follows_the_existing_codes():
    report = live_report(gap_at=slow_every_40th)
    report["samples"][5]["age_high_us"] = 600_000
    assert lc.check_report(report, 600)["codes"] == ["F05", "F08"]
    assert lc.FAIL_ORDER[-1] == "F08" and lc.FAIL_ORDER[:7] == [f"F0{n}" for n in range(1, 8)]


def test_too_few_intervals_is_unjudgeable_and_not_a_failure_code():
    out = lc.check_report(live_report(gap_at=lambda i: 230_000), 600)  # ~2,608 intervals
    assert out["interval"]["gate"] == "unjudgeable" and out["interval_unjudgeable"] is True
    assert "F08" not in out["codes"]


def test_the_gate_applies_only_to_a_completed_run():
    for reason in ("ack_lost", "protection_violation", "clock_unresolved", None):
        report = live_report(gap_at=slow_every_40th)
        report["reason"] = reason
        out = lc.check_report(report, 600)
        assert "F08" not in out["codes"] and out["interval"] is None, reason
        assert out["interval_unjudgeable"] is False
    assert ig.evaluate(None, 600) == {"applicable": False}
    assert ig.evaluate({"reason": "mode_degraded"}, 600) == {"applicable": False}
    refused = {"reason": "clock_unresolved", "measurement": None}
    assert lc.check_report(refused, 600)["interval"] is None


def test_the_3600_second_gate_uses_its_own_minimum():
    assert lc.check_report(live_report(3600), 3600)["interval"]["min_intervals"] == 16_200
    out = lc.check_report(live_report(3600, gap_at=lambda i: 230_000), 3600)
    assert out["interval_unjudgeable"] is True  # 15,652 intervals


def test_final_result_priority_with_unjudgeable_intervals():
    f = lc.final_result
    assert f(["A11"], ["F08"], restore_ok=True, interval_unjudgeable=True)["result"] == "aborted"
    started = f([], [], restore_ok=True, not_started=True, interval_unjudgeable=True)
    assert started["result"] == "not_started"
    failed = f([], ["F08"], restore_ok=True, interval_unjudgeable=True)
    assert failed["result"] == "failed" and failed["primary"] == "F08"
    assert f([], [], restore_ok=True, interval_unjudgeable=True) == {
        "result": "interval_unjudgeable",
        "primary": "interval_unjudgeable",
        "milestone": False,
    }
    broken = f([], [], restore_ok=False, interval_unjudgeable=True)
    assert broken["result"] == "interval_unjudgeable_restore_failed"
    assert broken["milestone"] is False
    assert f([], [], restore_ok=True)["result"] == "passed"  # the default is unchanged


# --- saved runs: reproduce criteria-scoring.json per_run_N2 --------------------------

WORK = Path(
    os.environ.get(
        "ESP32CAM_INTERVAL_STABILITY_DIR",
        "/Users/takeshimiura/.local/share/esp32cam-training/hold-stability-573-20261028"
        "/spin-on-run-v1-work/interval-stability-20261103",
    )
)
SCORING = WORK / "out" / "criteria-scoring.json"


@pytest.mark.skipif(not SCORING.exists(), reason="saved interval-stability data not present")
@pytest.mark.parametrize("run", ["affinity", "capture", "isolate", "mss", "quickack", "raw-clock"])
def test_saved_runs_reproduce_the_proposal_n2_values(run):
    # affinity, capture and mss stopped early and carry no measurement interval, so the
    # proposal scored every result and dropped only the start/teardown gaps of 4.5 s or
    # more (start=None). isolate, quickack and raw-clock have a window: results outside
    # it are not intervals (60, 103 and 59 of them). The live gate never sees a
    # window-less report (it applies only to a completed run); the window-less path
    # exists to reproduce these three.
    expected = json.loads(SCORING.read_text())["per_run_N2"][run]
    report = json.loads((WORK / "data" / run / "native.json").read_text())
    measurement = report["measurement"]
    window = (measurement["start_us"], measurement["end_us"]) if measurement else (None, None)
    gaps, excluded = ig.normal_intervals(report["samples"], *window)
    summary = ig.summarize(gaps, 600)
    assert summary["n"] == expected["n"]
    assert summary["p99_us"] / 1000 == pytest.approx(expected["p99"], abs=1e-6)
    assert summary["max_us"] / 1000 == pytest.approx(expected["max"], abs=1e-6)
    assert summary["over_us"]["gt250"] == expected["n_gt250"]
    assert summary["over_us"]["gt300"] == expected["n_gt300"]
    assert summary["over_us"]["gt350"] == expected["n_gt350"]
    left_out = expected["excluded"]
    assert excluded["cross_generation_gap_gt500ms"] == left_out[
        "cross_generation_gap_gt500ms(hold onset)"
    ]
    assert excluded["edge_gap_no_measurement_window"] == left_out.get(
        "edge_gap_ge_4.5s_no_measurement_window", 0
    )
    judged = summary["gate"] != "unjudgeable"
    assert judged == expected["sufficient_sample_ge_2700"]
    if judged:  # candidate A1 is the same p99 <= 250 ms test as the gate
        assert (summary["gate"] == "pass") == expected["scores"]["A"]["A1"]
    scores = expected["scores"]["A"]
    assert (summary["target"] == "met") == (scores["A1"] and scores["A2"])
    if measurement and report["reason"] == "completed":
        full = ig.evaluate(report, 600)
        assert full["n"] == expected["n"] and full["gate"] == summary["gate"]
        assert full["worker_dropped"] == report["worker"]["dropped"]
        assert full["receiver_dropped"] == report["receiver"]["dropped"]
        assert full["queue_busy"] == report["receiver"]["queue_busy"]


# --- the recover guard's post-run check writes the report and feeds the final result ---


def test_post_run_writes_interval_gate_json_and_the_unjudgeable_flag(tmp_path):
    from tests.test_spin_on_run_recover import guard

    slow = live_report(gap_at=slow_every_40th)
    g, d = guard(tmp_path / "slow", report=slow)
    info = g.post_run()
    assert "F08" in info["failure_codes"] and info["interval_unjudgeable"] is False
    written = json.loads((d / "interval-gate.json").read_text())
    assert written["gate"] == "fail" and written["n"] >= 2700

    few = live_report(gap_at=lambda i: 230_000)
    g, d = guard(tmp_path / "few", report=few)
    info = g.post_run()
    assert info["interval_unjudgeable"] is True and "F08" not in info["failure_codes"]
    assert json.loads((d / "interval-gate.json").read_text())["gate"] == "unjudgeable"

    early = {**live_report(), "reason": "ack_lost"}
    g, d = guard(tmp_path / "early", report=early)
    g.post_run()
    assert not (d / "interval-gate.json").exists()
