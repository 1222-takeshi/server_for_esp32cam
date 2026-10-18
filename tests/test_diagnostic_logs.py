import json
import threading
import time

import pytest

from script.diagnostic_logs import DiagnosticLog
from script.log_runner import LogBudget


def wait_for(check):
    deadline = time.monotonic() + 3
    while not check() and time.monotonic() < deadline:
        time.sleep(.01)
    assert check()


def test_normal_records_are_immutable_and_private(tmp_path):
    log = DiagnosticLog(tmp_path / "diagnostics")
    event = {"boot_id": 1, "sequence": 2, "detections": []}
    try:
        assert log.submit(event)
        event["sequence"] = 999
        wait_for(lambda: log.status()["written_records"] == 1)
    finally:
        log.close()
    path = log.directory / "runtime.log"
    record = json.loads(path.read_text())
    assert record["type"] == "diagnostic"
    assert record["event"]["sequence"] == 2
    assert path.stat().st_mode & 0o077 == 0


def test_quiet_diagnostics_expire_without_another_frame(tmp_path):
    now = [0.0]
    log = DiagnosticLog(tmp_path / "diagnostics", duration_seconds=1, clock=lambda: now[0])
    try:
        now[0] = 2
        wait_for(lambda: log.status()["state"] == "expired")
        assert not log.submit({"sequence": 1})
    finally:
        log.close()


def test_session_byte_limit_stops_only_diagnostics(tmp_path):
    log = DiagnosticLog(tmp_path / "diagnostics", byte_limit=512)
    try:
        for i in range(100):
            log.submit({"sequence": i})
        wait_for(lambda: log.status()["state"] == "byte_limit")
        assert 0 < log.status()["written_bytes"] <= 512
        assert sum(p.stat().st_size for p in log.directory.glob("runtime.log*")) <= 512
        assert not log.submit({"sequence": 101})
    finally:
        log.close()


def test_stalled_writer_has_a_bounded_queue_and_close(tmp_path, monkeypatch):
    release, started = threading.Event(), threading.Event()
    original = LogBudget._append
    def stalled(self, payload):
        started.set()
        release.wait(timeout=10)
        original(self, payload)
    monkeypatch.setattr(LogBudget, "_append", stalled)
    log = DiagnosticLog(tmp_path / "diagnostics")
    try:
        assert log.submit({"sequence": 1})
        assert started.wait(timeout=1)
        before = time.monotonic()
        for i in range(100):
            log.submit({"sequence": i})
        assert time.monotonic() - before < 1
        assert log.status()["queued"] <= 8
        assert log.status()["queue_dropped"] > 0
        import script.diagnostic_logs as module
        monkeypatch.setattr(module, "_bounded_copy", lambda _: pytest.fail("encoded while full"))
        assert not log.submit({"sequence": 999})
        before = time.monotonic()
        log.close()
        assert time.monotonic() - before < 1.5
    finally:
        release.set()
        log.close()


def test_disk_failure_oversize_and_invalid_data_are_observable(tmp_path, monkeypatch):
    def failed(self, payload):
        raise OSError("disk full")
    monkeypatch.setattr(LogBudget, "_append", failed)
    log = DiagnosticLog(tmp_path / "diagnostics")
    try:
        assert not log.submit({"message": "x" * 8192})
        assert not log.submit({"value": float("nan")})
        assert log.submit({"sequence": 1})
        wait_for(lambda: log.status()["state"] == "write_error")
        assert log.status()["errors"] == 1
        assert log.status()["oversized"] == log.status()["invalid"] == 1
        assert not log.submit({"sequence": 2})
    finally:
        log.close()


def test_raw_data_and_duplicate_collectors_are_protected(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir(mode=0o700)
    source = raw / "events.jsonl"
    source.write_text("immutable")
    with pytest.raises(ValueError):
        DiagnosticLog(raw)
    assert source.read_text() == "immutable"
    log = DiagnosticLog(tmp_path / "diagnostics")
    try:
        with pytest.raises(ValueError, match="active collector"):
            DiagnosticLog(log.directory)
    finally:
        log.close()
    replacement = DiagnosticLog(log.directory)
    replacement.close()


def test_previous_managed_sessions_share_the_32mib_generation_bound(tmp_path):
    directory = tmp_path / "diagnostics"
    first = DiagnosticLog(directory)
    first.close()
    for suffix, size in [("", 9 * 1024**2), (".1", 8 * 1024**2),
                         (".2", 8 * 1024**2), (".3", 8 * 1024**2), (".4", 8 * 1024**2)]:
        # Sparse files exercise logical quota without writing a large fixture.
        with (directory / ("runtime.log" + suffix)).open("wb") as handle:
            handle.truncate(size)
    second = DiagnosticLog(directory)
    try:
        assert second.submit({"sequence": 1})
        wait_for(lambda: second.status()["written_records"] == 1)
        files = list(directory.glob("runtime.log*"))
        assert len(files) == 4
        assert all(path.stat().st_size <= 8 * 1024**2 for path in files)
        assert sum(path.stat().st_size for path in files) <= 32 * 1024**2
    finally:
        second.close()


def test_runtime_and_diagnostic_directories_cannot_be_mixed(tmp_path):
    runtime = LogBudget(tmp_path / "runtime")
    runtime.line("preserve operational logs")
    runtime._lock_file.close()
    original = (runtime.directory / "runtime.log").read_bytes()
    with pytest.raises(ValueError):
        DiagnosticLog(runtime.directory)
    assert (runtime.directory / "runtime.log").read_bytes() == original
    diagnostic = DiagnosticLog(tmp_path / "diagnostics")
    diagnostic.close()
    with pytest.raises(ValueError):
        LogBudget(diagnostic.directory)


def test_preflight_bounds_strings_containers_depth_and_integers(tmp_path, monkeypatch):
    import script.diagnostic_logs as module
    original = json.dumps
    def bounded_encode(value, *args, **kwargs):
        if type(value) is str:
            assert len(value) <= 8192
        return original(value, *args, **kwargs)
    monkeypatch.setattr(module.json, "dumps", bounded_encode)
    deep = []
    for _ in range(100):
        deep = [deep]
    log = DiagnosticLog(tmp_path / "diagnostics")
    try:
        for value in ("x" * 100000, [None] * 100000, deep, 2**10000):
            assert not log.submit({"value": value})
        assert log.status()["oversized"] == 4
        assert log.status()["written_records"] == 0
    finally:
        log.close()


def test_concurrent_encoding_is_dropped_without_waiting(tmp_path, monkeypatch):
    import script.diagnostic_logs as module
    started, release = threading.Event(), threading.Event()
    original = module._bounded_copy
    def stalled(event):
        started.set()
        release.wait(timeout=5)
        return original(event)
    monkeypatch.setattr(module, "_bounded_copy", stalled)
    log = DiagnosticLog(tmp_path / "diagnostics")
    producer = threading.Thread(target=lambda: log.submit({"sequence": 1}))
    producer.start()
    try:
        assert started.wait(timeout=1)
        start = time.monotonic()
        assert not log.submit({"sequence": 2})
        assert time.monotonic() - start < .5
        assert log.status()["busy_dropped"] == 1
    finally:
        release.set()
        producer.join(timeout=1)
        log.close()


@pytest.mark.parametrize("settings", [{"duration_seconds": 0}, {"duration_seconds": 601},
                                     {"duration_seconds": True}, {"duration_seconds": 1.5},
                                     {"byte_limit": 0}, {"byte_limit": 33 * 1024**2},
                                     {"byte_limit": True}])
def test_invalid_limits_fail_before_directory_creation(tmp_path, settings):
    with pytest.raises(ValueError):
        DiagnosticLog(tmp_path / "diagnostics", **settings)
    assert not (tmp_path / "diagnostics").exists()


def test_gevent_process_is_rejected_before_thread_or_file_creation(tmp_path, monkeypatch):
    import script.diagnostic_logs as module
    monkeypatch.setattr(module.monkey, "is_module_patched", lambda _: True)
    with pytest.raises(ValueError, match="native"):
        DiagnosticLog(tmp_path / "diagnostics")
    assert not (tmp_path / "diagnostics").exists()
