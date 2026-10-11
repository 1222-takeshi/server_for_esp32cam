import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts import summarize_observations as summary


START = datetime(2026, 10, 11, 3, 2, 49, tzinfo=timezone.utc)
END = START + timedelta(days=7)


def observation(at, *, ready="ready", camera="healthy", ready_error=None, camera_error=None):
    return {
        "timestamp_utc": at.isoformat(),
        "readiness": {"status": ready, "error": ready_error},
        "cameras": {"camera-a": {"status": camera, "error": camera_error}},
    }


def write_lines(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def full_window():
    return [observation(START + timedelta(seconds=300 * index)) for index in range(2016)]


def test_complete_window_passes_and_hashes_immutable_input(tmp_path):
    path = write_lines(tmp_path / "observations.jsonl", full_window())
    original = path.read_bytes()
    result = summary.summarize([path], START, END, ["camera-a"], now=END)
    assert result["status"] == "passed"
    assert result["counts"]["valid_samples"] == 2016
    assert result["counts"]["gap_count"] == 0
    assert result["sample_ratios"]["readiness_ready"] == 1
    assert result["sources"][0]["sha256"] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original
    assert "not time uptime" in result["interpretation"]


def test_two_rotated_files_are_combined_in_input_order(tmp_path):
    records = full_window()
    older = write_lines(tmp_path / "older.jsonl", records[:1000])
    current = write_lines(tmp_path / "current.jsonl", records[1000:])
    result = summary.summarize([older, current], START, END, ["camera-a"], now=END)
    assert result["status"] == "passed"
    assert [source["input_index"] for source in result["sources"]] == [0, 1]
    assert result["counts"]["valid_samples"] == 2016


def test_calendar_timer_phase_and_outside_window_are_accounted(tmp_path):
    # 12:02:49 first sample, then calendar timer at 12:05:00 and every 5 min.
    next_tick = START.replace(minute=5, second=0)
    records = [observation(START - timedelta(minutes=5)), observation(START)]
    tick = next_tick
    while tick < END:
        records.append(observation(tick))
        tick += timedelta(minutes=5)
    records.append(observation(END))
    path = write_lines(tmp_path / "phase.jsonl", records)
    result = summary.summarize([path], START, END, ["camera-a"], now=END)
    assert result["status"] == "passed"
    assert result["counts"]["outside_before"] == 1
    assert result["counts"]["outside_after"] == 1
    assert result["counts"]["duplicate_timestamps"] == 0


def test_missing_sample_fails_even_when_ratios_are_perfect(tmp_path):
    records = full_window()
    del records[100]
    path = write_lines(tmp_path / "gap.jsonl", records)
    result = summary.summarize([path], START, END, ["camera-a"], now=END)
    assert result["status"] == "failed"
    assert result["counts"]["gap_count"] == 1
    assert result["sample_ratios"]["readiness_ready"] == 1


def test_missing_start_boundary_fails(tmp_path):
    path = write_lines(tmp_path / "late.jsonl", full_window()[2:])
    result = summary.summarize([path], START, END, ["camera-a"], now=END)
    assert result["status"] == "failed"
    assert result["counts"]["gap_count"] == 1


@pytest.mark.parametrize("delay_seconds,expected_status", [(60, "passed"), (61, "failed")])
def test_fixed_sixty_second_jitter_boundary(tmp_path, delay_seconds, expected_status):
    records = full_window()
    records[100] = observation(START + timedelta(minutes=500, seconds=delay_seconds))
    path = write_lines(tmp_path / "jitter.jsonl", records)
    result = summary.summarize([path], START, END, ["camera-a"], now=END)
    assert result["status"] == expected_status


def test_duplicates_cannot_inflate_sample_ratios(tmp_path):
    records = full_window()
    records.extend(observation(START) for _ in range(100))
    path = write_lines(tmp_path / "duplicates.jsonl", records)
    result = summary.summarize([path], START, END, ["camera-a"], now=END)
    assert result["status"] == "failed"
    assert result["counts"]["duplicate_timestamps"] == 100
    assert result["counts"]["valid_samples"] == 2016


def test_monitor_failure_is_separate_from_camera_offline(tmp_path):
    records = full_window()
    records[3] = observation(START + timedelta(minutes=15), ready=None,
                             ready_error="unreachable", camera=None,
                             camera_error="unreachable")
    records[4] = observation(START + timedelta(minutes=20), camera="offline")
    path = write_lines(tmp_path / "failures.jsonl", records)
    result = summary.summarize([path], START, END, ["camera-a"], now=END)
    assert result["counts"]["readiness"]["monitor_failure"] == 1
    assert result["counts"]["cameras"]["camera-a"]["monitor_failure"] == 1
    assert result["counts"]["cameras"]["camera-a"]["offline"] == 1
    assert result["status"] == "passed"  # two failures in 2016 samples are below 1%


def test_fixed_sample_threshold_fails_above_one_percent(tmp_path):
    records = full_window()
    for index in range(21):
        records[index] = observation(START + timedelta(minutes=5 * index), ready="degraded")
    path = write_lines(tmp_path / "degraded.jsonl", records)
    result = summary.summarize([path], START, END, ["camera-a"], now=END)
    assert result["status"] == "failed"
    assert "readiness_sample_ratio_below_threshold" in result["reasons"]


def test_pending_and_short_window_never_pass(tmp_path):
    path = write_lines(tmp_path / "short.jsonl", [observation(START)])
    pending = summary.summarize([path], START, END, ["camera-a"], now=END - timedelta(seconds=1))
    short = summary.summarize([path], START, START + timedelta(days=1), ["camera-a"],
                              now=END)
    assert pending["status"] == "pending"
    assert "window_not_elapsed" in pending["reasons"]
    assert short["status"] == "failed"
    assert "window_shorter_than_seven_days" in short["reasons"]


def test_invalid_line_and_unexpected_status_fail_closed(tmp_path):
    path = write_lines(tmp_path / "invalid.jsonl", full_window())
    with path.open("ab") as stream:
        stream.write(b'{"timestamp_utc":"bad","secret":"hidden"}\n')
    result = summary.summarize([path], START, END, ["camera-a"], now=END)
    assert result["counts"]["invalid"] == 1
    assert result["status"] == "failed"
    assert "hidden" not in json.dumps(result)


def test_unexpected_api_error_is_invalid_without_copying_secret(tmp_path):
    records = full_window()
    records[10] = observation(START + timedelta(minutes=50), ready=None,
                              ready_error="credential-secret")
    path = write_lines(tmp_path / "secret.jsonl", records)
    result = summary.summarize([path], START, END, ["camera-a"], now=END)
    assert result["counts"]["invalid"] == 1
    assert result["status"] == "failed"
    assert "credential-secret" not in json.dumps(result)


def test_cli_refuses_to_overwrite_output(tmp_path, monkeypatch):
    path = write_lines(tmp_path / "input.jsonl", [observation(START)])
    output = tmp_path / "result.json"
    output.write_text("keep")
    monkeypatch.setattr(summary, "summarize", lambda *args, **kwargs: {"status": "failed"})
    args = ["--input", str(path), "--camera-id", "camera-a", "--start", START.isoformat(),
            "--end", END.isoformat(), "--output", str(output)]
    assert summary.main(args) == 2
    assert output.read_text() == "keep"


def test_cli_writes_reproducible_result(tmp_path, capsys):
    historical_start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    historical_end = historical_start + timedelta(days=7)
    path = write_lines(tmp_path / "input.jsonl", [
        observation(historical_start + timedelta(minutes=5 * index)) for index in range(2016)
    ])
    output = tmp_path / "result.json"
    args = ["--input", str(path), "--camera-id", "camera-a",
            "--start", historical_start.isoformat(), "--end", historical_end.isoformat(),
            "--output", str(output)]
    assert summary.main(args) == 0
    assert capsys.readouterr().out.strip() == "passed"
    result = json.loads(output.read_text())
    assert result["configuration"]["interval_seconds"] == 300
    assert result["status"] == "passed"


@pytest.mark.parametrize("value", ["2026-10-11T03:02:49", "2026-10-11T12:02:49+09:00", "bad"])
def test_non_utc_window_is_rejected(value):
    with pytest.raises(ValueError):
        summary.parse_utc(value)


def test_oversized_input_is_rejected(tmp_path):
    path = tmp_path / "huge.jsonl"
    path.write_bytes(b"x" * (summary.MAX_INPUT_BYTES + 1))
    with pytest.raises(ValueError, match="8 MiB"):
        summary.summarize([path], START, END, ["camera-a"], now=END)


def test_excessive_line_count_is_rejected_before_splitting(tmp_path):
    path = tmp_path / "many-lines.jsonl"
    path.write_bytes(b"\n" * (summary.MAX_INPUT_LINES + 1))
    with pytest.raises(ValueError, match="too many input lines"):
        summary.summarize([path], START, END, ["camera-a"], now=END)
