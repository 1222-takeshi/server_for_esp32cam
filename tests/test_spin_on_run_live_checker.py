import copy

import pytest

from scripts.spin_on_run import live_checker as lc

START = 1_000_000_000


def good_report(seconds=600, holds=None, step=190_000):
    end = START + seconds * 1_000_000
    holds = holds or []
    samples = [
        {
            "boot_id": 9,
            "generation": 1 + sum(1 for h in holds if h[2] <= p),
            "sequence": n + 1,
            "published_us": p,
            "age_high_us": 300_000,
            "compute_us": 1,
        }
        for n, p in enumerate(range(START, end, step))
    ]
    tumbling = [0] * (seconds // 10)
    for s in samples:
        tumbling[(s["published_us"] - START) // 10_000_000] += 1
    epochs = [[START, 1]] + [[h[2], 2 + i] for i, h in enumerate(holds)]
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
            "generation_epochs": epochs,
            "tracking_holds": holds,
            "tracking_hold_events": len(holds),
            "tracking_automatic_recoveries": len(holds),
            "injected_hold": False,
        },
    }


def test_a_clean_run_passes_and_reports_the_window_minimum():
    out = lc.check_report(good_report(), 600)
    assert out["codes"] == [] and out["windows"]["tumbling_min"] >= 50
    assert out["windows"]["any_start_min"] >= 50
    assert lc.check_report(good_report(3600), 3600)["codes"] == []


def test_planned_example_with_one_natural_hold_passes():
    holds = [[1, START + 200_000_000, START + 203_000_000]]
    report = good_report(holds=holds)
    # No results during the hold, as in a real hold.
    report["samples"] = [
        s for s in report["samples"] if not (holds[0][1] <= s["published_us"] < holds[0][2])
    ]
    for i, s in enumerate(report["samples"]):
        s["sequence"] = i + 1
        s["generation"] = 2 if s["published_us"] >= holds[0][2] else 1
    tumbling = [0] * 60
    for s in report["samples"]:
        tumbling[(s["published_us"] - START) // 10_000_000] += 1
    report["measurement"]["window_results"] = tumbling
    report["measurement"]["results"] = len(report["samples"])
    out = lc.check_report(report, 600)
    assert out["windows"]["natural_hold_us"] == 3_000_000
    assert "F03" not in out["codes"] and "F04" not in out["codes"]
    assert "F07" not in out["codes"] and "F01" not in out["codes"]


def test_unreadable_report_is_f01_and_f06():
    for bad in (None, [], "x"):
        assert lc.check_report(bad, 600)["codes"] == ["F01", "F06"]


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda r: r.update(reason="ack_lost"), "F01"),
        (lambda r: r.update(continuous_tracking_gate_passed=False), "F01"),
        (lambda r: r["samples"][5].update(age_high_us=500_001), "F05"),
        (lambda r: r["samples"][5].update(age_high_us=500_000.0), "F05"),
        (lambda r: r["samples"][6].update(sequence=r["samples"][5]["sequence"]), "F06"),
        (lambda r: r["samples"][6].update(boot_id=1), "F06"),
        (lambda r: r["measurement"]["window_results"].__setitem__(0, 99), "F06"),
        (lambda r: r.update(hold_injection_requested=True), "F06"),
        (lambda r: r["session_final"].update(reason="frames_expired"), "F07"),
        (lambda r: r["session_final"].update(tracking_auto_recovery_pending=True), "F07"),
        (lambda r: r["measurement"].update(tracking_hold_events=1), "F07"),
    ],
)
def test_each_failure_code_is_individually_detected(edit, code):
    report = copy.deepcopy(good_report())
    edit(report)
    assert code in lc.check_report(report, 600)["codes"]


def test_hold_budget_and_continuous_limit_boundaries():
    def run(holds):
        r = good_report(holds=[])
        r["measurement"]["tracking_holds"] = holds
        r["measurement"]["tracking_hold_events"] = len(holds)
        r["measurement"]["tracking_automatic_recoveries"] = len(holds)
        return lc.check_report(r, 600)["codes"]

    a = START + 10_000_000
    assert "F03" not in run([[1, a, a + 6_000_000]])
    assert "F03" in run([[1, a, a + 6_000_001]])
    assert "F04" not in run([[1, a, a + 14_999_999]])
    assert "F04" in run([[1, a, a + 15_000_000]])
    # Two holds that sum to more than the budget; neither is continuous.
    assert "F03" in run([[1, a, a + 3_000_000], [2, a + 5_000_000, a + 8_000_001]])


def test_hold_started_before_the_interval_counts_its_whole_length_for_f04():
    holds = [[1, START - 14_000_000, START + 1_000_000]]
    total, longest = lc.hold_stats(holds, START, START + 600_000_000)
    assert (total, longest) == (1_000_000, 15_000_000)
    assert lc.hold_stats([[1, START - 5, START]], START, START + 600_000_000) == (0, 0)


def test_any_start_window_is_stricter_than_the_tumbling_windows():
    start, end = 0, 30_000_000
    times = [i * 1_000 for i in range(50)] + [19_900_000 + i * 1_000 for i in range(50)]
    times += list(range(20_000_000, 30_000_000, 200_000))
    assert lc.min_window_count(times, start, end) < 50
    assert lc.min_window_count([], 0, 5_000_000) is None
    dense = list(range(0, 30_000_000, 100_000))
    assert lc.min_window_count(dense, 0, 30_000_000) == 100


def test_window_count_matches_brute_force():
    import random

    rng = random.Random(1)
    times = sorted(rng.sample(range(0, 25_000_000, 1_000), 700))
    brute = min(sum(1 for p in times if t <= p < t + 10_000_000) for t in range(0, 15_000_001, 500))
    assert lc.min_window_count(times, 0, 25_000_000) <= brute


def test_field_map_documents_the_gate_field_difference():
    assert lc.FIELD_MAP["$.finite_live_gate_passed"] == "continuous_tracking_gate_passed"
    assert lc.FIELD_MAP["$.reason"] == "reason"


def test_abort_codes_follow_the_planned_exceptions():
    kw = {
        "marker": "completed",
        "service_result": "success",
        "report": {"reason": "completed"},
        "child_exit": 0,
        "child_signalled": False,
        "guardian_stopped_child": False,
    }
    assert lc.abort_codes(**kw) == []
    assert lc.abort_codes(**{**kw, "marker": None}) == ["A06"]
    assert lc.abort_codes(**{**kw, "marker": None, "service_result": "timeout"}) == ["A10"]
    online = {**kw, "report": {"reason": "window_below_50"}, "child_exit": 20}
    assert lc.abort_codes(**online) == ["A13"]  # planned stop is not A07
    assert lc.abort_codes(**{**online, "report": {"reason": "ack_lost"}}) == ["A07"]
    assert lc.abort_codes(**{**kw, "child_exit": 2}) == ["A07"]
    assert lc.abort_codes(**{**kw, "child_exit": None, "child_signalled": True}) == ["A07"]
    stopped = {**kw, "marker": "abort A01", "child_signalled": True, "guardian_stopped_child": True}
    assert lc.abort_codes(**stopped) == ["A01"]
    assert lc.abort_codes(**{**kw, "marker": "abort A05,A01"}) == ["A05", "A01"]
    assert lc.abort_codes(**{**kw, "child_exit": 3}) == []  # nonzero gate result handled as F01


def test_final_result_priority_and_the_milestone():
    f = lc.final_result
    assert f(["A11"], ["F01"], restore_ok=True)["result"] == "aborted"
    assert f([], ["F02", "F01"], restore_ok=True)["primary"] == "F02"
    assert f([], [], restore_ok=False) == {
        "result": "passed_restore_failed",
        "primary": None,
        "milestone": False,
    }
    assert f([], [], restore_ok=True)["milestone"] is True
