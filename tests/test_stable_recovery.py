import json
import sys

import pytest

from scripts import check_stable_recovery as check


def report(seconds=600, injection=True):
    start = 20_000_000
    injected_at = start + 30_000_000
    periods = [[3, injected_at, injected_at + 10_000_000]] if injection else []
    return {
        "continuous_tracking_policy": True,
        "continuous_tracking_gate_passed": True,
        "live_validation_only": True,
        "physical_motor_control": False,
        "global_acceptance": False,
        "reason": "completed",
        "hold_injection_requested": injection,
        "measurement": {
            "start_us": start,
            "end_us": start + seconds * 1_000_000,
            "duration_us": seconds * 1_000_000,
            "boot_id": 2**64 - 1,
            "policy": "continuous-recovery-v1",
            "tracking_hold_statistics_scope": "full_tracking_phase",
            "tracking_holds": periods,
            "tracking_hold_events": len(periods),
            "tracking_automatic_recoveries": len(periods),
            "injected_hold": injection,
            "injected_hold_at": [injected_at, 3] if injection else None,
        },
        "samples": [
            {
                "boot_id": 2**64 - 1,
                "sequence": i + 1,
                "generation": 3 if injection and i >= 150 else 2,
                "published_us": start + i * 200_000,
                "age_high_us": 500_000,
            }
            for i in range(seconds * 5)
        ],
    }


def set_holds(data, holds):
    m = data["measurement"]
    m["tracking_holds"] = sorted(holds, key=lambda h: h[1])
    m["tracking_hold_events"] = m["tracking_automatic_recoveries"] = len(holds)
    for generation, began, _ in m["tracking_holds"]:
        for row in data["samples"]:
            if row["published_us"] >= began:
                row["generation"] = generation


@pytest.mark.parametrize("seconds,injection", [(600, True), (3600, True), (600, False)])
def test_fresh_five_hz_and_only_declared_injection_pass(seconds, injection):
    result = check.evaluate(report(seconds, injection))
    assert result["passed"] is True
    assert result["window_results"] == [50] * (seconds // 10)
    assert result["natural_hold_us"] == 0
    assert result["injected_hold_us"] == (10_000_000 if injection else 0)
    assert result["supplementary_only"] and result["global_acceptance"] is False


def test_mean_five_hz_cannot_hide_a_window_with_49_results():
    data = report()
    data["samples"][49]["published_us"] += 200_000
    result = check.evaluate(data)
    assert result["recognition_hz"] == 5
    assert result["window_results"][:2] == [49, 51]
    assert result["windows_below_50"] == 1
    assert result["passed"] is False


def test_natural_hold_budget_is_exact_at_one_percent_and_fails_one_microsecond_over():
    data = report(3600)
    start = data["measurement"]["start_us"]
    holds = data["measurement"]["tracking_holds"] + [
        [4, start + 50_000_000, start + 68_000_000],
        [5, start + 90_000_000, start + 108_000_000],
    ]
    set_holds(data, holds)
    assert check.evaluate(data)["passed"] is True
    holds[-1][2] += 1
    result = check.evaluate(data)
    assert result["natural_hold_us"] == 36_000_001
    assert result["natural_hold_within_one_percent"] is False


def test_hold_time_is_clipped_to_measurement_and_end_sample_is_excluded():
    data = report(3600)
    m = data["measurement"]
    set_holds(
        data,
        m["tracking_holds"]
        + [
            [1, m["start_us"] - 15_000_000, m["start_us"] + 5_000_000],
            [4, m["end_us"] - 5_000_000, m["end_us"] + 15_000_000],
        ],
    )
    data["samples"].append({**data["samples"][-1], "sequence": 18_001, "published_us": m["end_us"]})
    result = check.evaluate(data)
    assert result["results"] == 18_000
    assert result["natural_hold_us"] == 10_000_000
    assert result["passed"] is True


@pytest.mark.parametrize(
    "failure",
    [
        "boolean",
        "age",
        "order",
        "boot",
        "unrecovered",
        "scope",
        "duplicate_hold",
        "injection_missing",
        "injection_outside",
        "legacy",
    ],
)
def test_invalid_or_ambiguous_evidence_is_rejected(failure):
    data = report()
    m = data["measurement"]
    if failure == "boolean":
        data["samples"][0]["sequence"] = True
    elif failure == "age":
        data["samples"][0]["age_high_us"] += 1
    elif failure == "order":
        data["samples"][1]["sequence"] = 1
    elif failure == "boot":
        data["samples"][0]["boot_id"] -= 1
    elif failure == "unrecovered":
        m["tracking_automatic_recoveries"] = 0
    elif failure == "scope":
        m["tracking_hold_statistics_scope"] = "unknown"
    elif failure == "duplicate_hold":
        set_holds(data, m["tracking_holds"] * 2)
    elif failure == "injection_missing":
        m["injected_hold_at"][1] += 1
    elif failure == "injection_outside":
        m["injected_hold_at"][0] = m["start_us"] - 1
    elif failure == "legacy":
        data["continuous_tracking_gate_passed"] = False
    with pytest.raises(ValueError):
        check.evaluate(data)


def test_input_cap_duplicate_keys_and_nonfinite_json_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    with monkeypatch.context() as small:
        small.setattr(check, "REPORT_LIMIT", 8)
        path.write_bytes(b'{"a":12}')
        assert check.load_report(path) == {"a": 12}
        path.write_bytes(b'{"a":12} ')
        with pytest.raises(ValueError):
            check.load_report(path)
    for raw in ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}']:
        path.write_text(raw)
        with pytest.raises(ValueError):
            check.load_report(path)


def test_cli_outputs_only_verdict_and_never_rewrites_input(tmp_path, monkeypatch, capsys):
    path = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["check", str(path)])
    data = report()
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    assert check.main() == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert path.read_bytes() == original
    data["samples"][49]["published_us"] += 200_000
    path.write_text(json.dumps(data))
    assert check.main() == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False
    path.write_text('{"secret":"private-value"}')
    assert check.main() == 2
    assert capsys.readouterr().out.strip() == '{"error": "invalid_report"}'
