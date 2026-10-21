import copy
import hashlib
import json

import pytest

from scripts import summarize_operating_audit as audit


def sample(index):
    identity = {"pid": 10, "start_ticks": "20", "commit": "a" * 40}
    return {"elapsed_seconds": index * 60 + .1, "state": "active", "reason": None,
            "wlan_power_save": "off", "deployment": identity, "deployment_after": identity,
            "deployment_changed_during_sample": False, "camera_boot_id": "123",
            "requested_mode": "eco", "effective_mode": "eco", "model_sha256": "f" * 64,
            "managed_log_bytes": 100 + index, "vitals": {"temperature_c": 50},
            "resources": {"paused": False, "sample": {"db": 1000, "wal": index, "free": 9000}},
            "service_process_write_bytes_delta": index * 100, "observed_exited_processes": 0,
            "host_write_bytes_delta": index * 1000}


def write_run(path, rows, *, complete=False):
    manifest = {"duration_seconds": 86400, "period_seconds": 60,
                "wlan_power_save_recorded_per_sample": True}
    (path / "manifest.json").write_text(json.dumps(manifest))
    (path / "samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    if complete:
        (path / "result.json").write_text(json.dumps({
            "completed": True, "elapsed_seconds": 86400.2,
            "samples": sum("error" not in row for row in rows),
            "errors": sum("error" in row for row in rows)}))
    return path


def test_complete_observation_reports_metrics_without_accepting_normal_load(tmp_path):
    write_run(tmp_path, [sample(i) for i in range(1441)], complete=True)
    original = (tmp_path / "samples.jsonl").read_bytes()
    result = audit.summarize(tmp_path)
    assert result["observation_completed"] is True
    assert result["normal_load_24h_accepted"] is False
    assert result["tracking_24h_accepted"] is False
    assert result["watts_measured"] is False
    assert result["state_counts"] == {"active": 1441}
    assert result["requested_effective_mode_counts"] == {"eco/eco": 1441}
    assert result["observed_maximums"] == {
        "db": 1000, "wal": 1440, "managed_log": 1540, "temperature_c": 50}
    assert result["service_write_bytes_lower_bound"] == 144000
    assert result["whole_host_write_bytes_at_last_sample"] == 1440000
    assert result["evidence_sha256"]["samples.jsonl"] == hashlib.sha256(original).hexdigest()
    assert (tmp_path / "samples.jsonl").read_bytes() == original


def test_partial_and_premature_completion_never_become_complete(tmp_path):
    write_run(tmp_path, [sample(0), sample(1)], complete=True)
    assert audit.summarize(tmp_path)["observation_completed"] is False
    (tmp_path / "result.json").unlink()
    assert audit.summarize(tmp_path)["observation_completed"] is False
    output = tmp_path / "summary.json"
    assert audit.main(["--run", str(tmp_path), "--output", str(output),
                       "--require-complete"]) == 3
    assert not output.exists()


@pytest.mark.parametrize("kind", ["late_start", "catch_up", "concentrated", "future_sample"])
def test_row_count_cannot_hide_missing_time_coverage(tmp_path, kind):
    rows = [sample(i) for i in range(1441)]
    if kind == "late_start":
        for row in rows:
            row["elapsed_seconds"] += 3600
    elif kind == "catch_up":
        for i in range(500, 562):
            rows[i]["elapsed_seconds"] = 500 * 60 + 3600 + i - 500
    elif kind == "concentrated":
        for i in range(1440):
            rows[i]["elapsed_seconds"] = i + .1
    else:
        rows[-1]["elapsed_seconds"] = 86401
    write_run(tmp_path, rows, complete=True)
    if kind == "future_sample":
        with pytest.raises(ValueError, match="predates"):
            audit.summarize(tmp_path)
    else:
        result = json.loads((tmp_path / "result.json").read_text())
        result["elapsed_seconds"] = rows[-1]["elapsed_seconds"] + .1
        (tmp_path / "result.json").write_text(json.dumps(result))
        summary = audit.summarize(tmp_path)
        assert summary["observer_result_completed"] is True
        assert summary["sampling_coverage_complete"] is False
        assert summary["observation_completed"] is False


@pytest.mark.parametrize("delay,complete", [(60, True), (60.001, False)])
def test_summary_coverage_jitter_boundary_is_fixed(tmp_path, delay, complete):
    rows = [sample(i) for i in range(1441)]
    for row in rows:
        row["elapsed_seconds"] = round(row["elapsed_seconds"] - .1 + delay, 3)
    write_run(tmp_path, rows, complete=True)
    result = json.loads((tmp_path / "result.json").read_text())
    result["elapsed_seconds"] = rows[-1]["elapsed_seconds"] + .1
    (tmp_path / "result.json").write_text(json.dumps(result))
    assert audit.summarize(tmp_path)["observation_completed"] is complete


@pytest.mark.parametrize("early,complete", [(.001, True), (.002, False)])
def test_exact_millisecond_early_boundary_at_final_sample(tmp_path, early, complete):
    rows = [sample(i) for i in range(1441)]
    rows[-1]["elapsed_seconds"] = round(86400 - early, 3)
    write_run(tmp_path, rows, complete=True)
    assert audit.summarize(tmp_path)["observation_completed"] is complete


@pytest.mark.parametrize("payload", [None, [], "completed"])
def test_non_object_completion_marker_is_invalid(tmp_path, payload):
    write_run(tmp_path, [sample(0)])
    (tmp_path / "result.json").write_text(json.dumps(payload))
    assert audit.main(["--run", str(tmp_path), "--output", str(tmp_path / "summary.json"),
                       "--require-complete"]) == 2
    assert not (tmp_path / "summary.json").exists()


def test_errors_and_missing_vitals_are_unknown_instead_of_healthy(tmp_path):
    row = sample(2)
    row.update(state="degraded", reason="resources_unavailable", wlan_power_save="on")
    row["resources"] = {"paused": True, "sample": None}
    row["vitals"]["temperature_c"] = None
    write_run(tmp_path, [{"elapsed_seconds": .1, "error": "TimeoutError"}, row])
    result = audit.summarize(tmp_path)
    assert (result["success_rows"], result["error_rows"]) == (1, 1)
    assert result["wlan_power_save_counts"] == {"on": 1}
    assert result["wifi_unknown_error_rows"] == 1
    assert result["resource_paused_rows"] == result["resource_sample_missing_rows"] == 1
    assert result["temperature_missing_rows"] == 1
    assert result["observed_maximums"]["db"] is None
    assert result["minimum_free_bytes"] is None


def test_source_change_and_process_exits_remain_visible(tmp_path):
    first, second = sample(0), copy.deepcopy(sample(1))
    second["deployment_after"] = {"pid": 11, "start_ticks": "30", "commit": "b" * 40}
    second["deployment_changed_during_sample"] = True
    second["observed_exited_processes"] = 2
    second["camera_boot_id"] = "456"
    write_run(tmp_path, [first, second])
    result = audit.summarize(tmp_path)
    assert result["deployment_changed_rows"] == 1
    assert result["observed_exited_processes"] == 2
    assert result["camera_boot_counts"] == {"123": 1, "456": 1}


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(elapsed_seconds=-1),
    lambda row: row.update(elapsed_seconds=float("nan")),
    lambda row: row.update(managed_log_bytes=True),
    lambda row: row.update(wlan_power_save="unknown"),
    lambda row: row.update(deployment_changed_during_sample=True),
    lambda row: row.update(service_process_write_bytes_delta=-1),
])
def test_invalid_sample_is_rejected(tmp_path, mutation):
    row = sample(0)
    mutation(row)
    write_run(tmp_path, [row])
    with pytest.raises(ValueError):
        audit.summarize(tmp_path)


@pytest.mark.parametrize("field", ["elapsed_seconds", "service_process_write_bytes_delta",
                                   "host_write_bytes_delta", "observed_exited_processes"])
def test_duplicate_time_or_regressed_counters_rejected(tmp_path, field):
    first, second = sample(1), sample(2)
    first["observed_exited_processes"] = 2
    second["observed_exited_processes"] = 3
    second[field] = first[field] if field == "elapsed_seconds" else first[field] - 1
    write_run(tmp_path, [first, second])
    with pytest.raises(ValueError):
        audit.summarize(tmp_path)


def test_result_count_mismatch_and_oversize_are_rejected(tmp_path, monkeypatch):
    write_run(tmp_path, [sample(0)], complete=True)
    result_path = tmp_path / "result.json"
    result = json.loads(result_path.read_text())
    result["samples"] = 2
    result_path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="counters"):
        audit.summarize(tmp_path)
    monkeypatch.setattr(audit, "MAX_BYTES", 4)
    with pytest.raises(ValueError, match="byte limit"):
        audit.summarize(tmp_path)


@pytest.mark.parametrize("name", ["manifest.json", "samples.jsonl", "result.json", "summary.json"])
def test_cli_preserves_inputs_and_existing_output(tmp_path, name):
    write_run(tmp_path, [sample(0)])
    target = tmp_path / name
    if name == "summary.json":
        target.write_text("keep me")
    before = target.read_bytes() if target.exists() else None
    assert audit.main(["--run", str(tmp_path), "--output", str(target)]) == 2
    assert (target.read_bytes() if target.exists() else None) == before


def test_changing_input_is_rejected_and_new_output_is_written_once(tmp_path, monkeypatch):
    write_run(tmp_path, [sample(0)])
    original = audit.read_bounded
    calls = 0

    def changing(path, limit):
        nonlocal calls
        data = original(path, limit)
        if path.name == "samples.jsonl":
            calls += 1
            if calls == 2:
                return data + b" "
        return data

    monkeypatch.setattr(audit, "read_bounded", changing)
    with pytest.raises(ValueError, match="changed"):
        audit.summarize(tmp_path)
    monkeypatch.setattr(audit, "read_bounded", original)
    target = tmp_path / "summary.json"
    assert audit.main(["--run", str(tmp_path), "--output", str(target)]) == 0
    assert json.loads(target.read_text())["observation_completed"] is False
    assert audit.main(["--run", str(tmp_path), "--output", str(target)]) == 2


@pytest.mark.parametrize("phase", ["write", "flush", "close", "fsync"])
def test_io_failure_never_publishes_partial_json(tmp_path, monkeypatch, phase):
    write_run(tmp_path, [sample(0)])
    factory = audit.tempfile.NamedTemporaryFile

    class BrokenFile:
        def __init__(self, **kwargs):
            self.inner = factory(**kwargs)
            self.name = self.inner.name

        def __enter__(self):
            return self

        def write(self, data):
            if phase == "write":
                self.inner.write(data[:5])
                raise OSError("full")
            return self.inner.write(data)

        def flush(self):
            if phase == "flush":
                raise OSError("full")
            return self.inner.flush()

        def fileno(self):
            return self.inner.fileno()

        def __exit__(self, *args):
            self.inner.__exit__(*args)
            if phase == "close":
                raise OSError("full")

    monkeypatch.setattr(audit.tempfile, "NamedTemporaryFile", BrokenFile)
    if phase == "fsync":
        def fail_sync(fd):
            raise OSError("full")
        monkeypatch.setattr(audit.os, "fsync", fail_sync)
    target = tmp_path / "summary.json"
    assert audit.main(["--run", str(tmp_path), "--output", str(target)]) == 2
    assert not target.exists()
    assert not list(tmp_path.glob(".audit-summary-*"))
