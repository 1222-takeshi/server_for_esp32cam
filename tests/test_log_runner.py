import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from script.log_runner import LogBudget, run


requires_waitid = pytest.mark.skipif(not hasattr(os, "waitid"), reason="collector needs POSIX waitid")


def entries(directory):
    return [json.loads(line) for path in directory.glob("runtime.log*")
            for line in path.read_text().splitlines()]


def test_rotation_has_a_byte_and_generation_bound(tmp_path):
    budget = LogBudget(tmp_path / "logs", max_bytes=1024, backups=2, line_bytes=256,
                       records_per_minute=1000)
    for i in range(100):
        budget.line(f"{i}:" + "x" * 1000)
    files = list(budget.directory.glob("runtime.log*"))
    assert len(files) == 3
    assert all(p.stat().st_size <= 1024 for p in files)
    assert all(p.stat().st_mode & 0o077 == 0 for p in files)
    assert all(len(json.dumps(row, ensure_ascii=False).encode()) < 256 for row in entries(budget.directory))
    assert budget.truncated == 100


def test_minute_budget_and_summary_preserve_suppression_counts(tmp_path):
    now = [0.0]
    budget = LogBudget(tmp_path / "logs", records_per_minute=2, clock=lambda: now[0])
    for _ in range(5):
        budget.line(f"error {_}")
    assert len(entries(budget.directory)) == 2
    now[0] = 60
    budget.tick()
    summary = entries(budget.directory)[-1]
    assert summary["type"] == "runtime.summary"
    assert summary["suppressed"] == 3
    budget.line("recovered")
    assert entries(budget.directory)[-1]["message"] == "recovered"


@pytest.mark.parametrize("values", [{"max_bytes": 0}, {"backups": -1},
                                    {"line_bytes": 100}, {"records_per_minute": 0}])
def test_invalid_budgets_are_rejected_before_starting(values, tmp_path):
    with pytest.raises(ValueError):
        LogBudget(tmp_path / "logs", **values)


def test_existing_source_data_and_symlinks_are_not_managed(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "events.jsonl"
    source.write_text("immutable")
    with pytest.raises(ValueError):
        LogBudget(raw)
    managed = tmp_path / "logs"
    managed.mkdir()
    (managed / "runtime.log").symlink_to(source)
    with pytest.raises(ValueError):
        LogBudget(managed)
    assert source.read_text() == "immutable"


@requires_waitid
def test_storage_failure_does_not_stop_draining_or_child(tmp_path, monkeypatch):
    budget = LogBudget(tmp_path / "logs")
    def full_disk(_payload):
        raise OSError("disk full")
    monkeypatch.setattr(budget, "_append", full_disk)
    assert run([sys.executable, "-c", "print('hello'); raise SystemExit(7)"], budget) == 7
    assert budget.write_errors > 0


@requires_waitid
def test_burst_and_unterminated_long_lines_are_bounded(tmp_path):
    budget = LogBudget(tmp_path / "logs", records_per_minute=3)
    code = "import os; os.write(1,b'x'*200000+b'\\n'); print('tail')"
    assert run([sys.executable, "-c", code], budget) == 0
    rows = entries(budget.directory)
    assert any(row.get("message") == "tail" for row in rows)
    assert all(len(json.dumps(row).encode()) < 8192 for row in rows)
    assert budget.truncated >= 1


def test_duplicate_collectors_cannot_rotate_the_same_directory(tmp_path):
    owner = LogBudget(tmp_path / "logs")
    with pytest.raises(ValueError, match="active collector"):
        LogBudget(owner.directory)
    owner.line("still running")
    assert entries(owner.directory)[0]["message"] == "still running"


def test_simultaneous_first_start_cannot_bypass_the_collector_lock(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    original = os.open
    def simultaneous(path, *args):
        if str(path).endswith(".esp32cam-runtime-logs"):
            barrier.wait(timeout=5)
        return original(path, *args)
    monkeypatch.setattr(os, "open", simultaneous)
    def create():
        try:
            return LogBudget(tmp_path / "logs")
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: create(), range(2)))
    assert sum(result is not None for result in outcomes) == 1


def test_written_bytes_and_identical_errors_are_limited(tmp_path):
    budget = LogBudget(tmp_path / "logs", line_bytes=256, bytes_per_minute=1024)
    for i in range(50):
        budget.line(f"{i}:" + "x" * 100)
    budget.line("last error")
    budget.line("last error")
    assert sum(p.stat().st_size for p in budget.directory.glob("runtime.log*")) <= 1024
    assert budget.suppressed > 0


def test_lowered_budgets_apply_to_all_existing_managed_generations(tmp_path):
    previous = LogBudget(tmp_path / "logs", max_bytes=4096, line_bytes=256, backups=3)
    for i in range(60):
        previous.line(f"{i}:" + "x" * 100)
    previous._lock_file.close()
    smaller = LogBudget(previous.directory, max_bytes=1024, line_bytes=256, backups=1)
    assert sum(p.stat().st_size for p in smaller.directory.glob("runtime.log*")) <= 2048
    smaller.line("new budget")
    assert len(list(smaller.directory.glob("runtime.log*"))) <= 2


@requires_waitid
def test_exited_leader_cannot_leave_a_running_grandchild(tmp_path):
    finished = tmp_path / "grandchild-finished"
    grandchild = f"import pathlib,time; time.sleep(5); pathlib.Path({str(finished)!r}).touch()"
    leader = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{grandchild!r}])"
    budget = LogBudget(tmp_path / "logs")
    assert run([sys.executable, "-c", leader], budget) == 0
    time.sleep(3)
    assert not finished.exists()


@requires_waitid
def test_a_stalled_disk_writer_cannot_backpressure_the_child(tmp_path, monkeypatch):
    budget = LogBudget(tmp_path / "logs")
    release = threading.Event()
    append = budget._append
    def stalled(payload):
        release.wait(timeout=10)
        append(payload)
    monkeypatch.setattr(budget, "_append", stalled)
    started = time.monotonic()
    try:
        assert run([sys.executable, "-c", "import os; os.write(1,b'x\\n'*2000)"], budget) == 0
        assert time.monotonic() - started < 5
        assert budget.queue_dropped > 0
    finally:
        release.set()


@pytest.mark.parametrize("ignore_and_close", [False, True])
@requires_waitid
def test_wrapper_forwards_termination_and_bounds_shutdown(tmp_path, ignore_and_close):
    ready = tmp_path / "ready"
    before_ready = "signal.signal(signal.SIGTERM,signal.SIG_IGN); os.close(1); os.close(2);" if ignore_and_close else ""
    command = [sys.executable, "-m", "script.log_runner", "--directory", str(tmp_path / "logs"),
               "--", sys.executable, "-c",
               f"import os,signal,pathlib,time; {before_ready} pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"]
    process = subprocess.Popen(command)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        os.kill(process.pid, signal.SIGTERM)
        expected = signal.SIGKILL if ignore_and_close else signal.SIGTERM
        assert process.wait(timeout=10) == 128 + expected
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_unsupported_waitid_fails_before_child_start(tmp_path, monkeypatch):
    import script.log_runner as runner
    monkeypatch.delattr(runner.os, "waitid", raising=False)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: pytest.fail("child started"))
    with pytest.raises(ValueError, match="waitid"):
        runner.run(["unused"], LogBudget(tmp_path / "logs"))


@requires_waitid
def test_sigint_during_child_start_is_preserved(tmp_path, monkeypatch):
    original = subprocess.Popen
    def interrupted_start(*args, **kwargs):
        signal.raise_signal(signal.SIGINT)
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", interrupted_start)
    assert run([sys.executable, "-c", "import time; time.sleep(60)"],
               LogBudget(tmp_path / "logs")) == 128 + signal.SIGINT
