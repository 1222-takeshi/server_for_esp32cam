"""Boundary and lifecycle tests for the portable live observer."""

from __future__ import annotations

import json
import hashlib
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from script import live_observer


def args(tmp_path: Path, *extra: str):
    key = tmp_path / "key.txt"
    key.write_text("private-token\n", encoding="utf-8")
    key.chmod(0o600)
    return live_observer.parse_args([
        "--key-file", str(key), "--output-dir", str(tmp_path / "new-run"),
        "--duration-seconds", "1", *extra,
    ])


def test_private_key_and_invalid_arguments(tmp_path: Path) -> None:
    parsed = args(tmp_path)
    assert live_observer.read_key(parsed.key_file) == "private-token"
    assert "private-token" not in repr(parsed)
    for option, value in (("--duration-seconds", "0"), ("--duration-seconds", "3601"),
                          ("--warmup-seconds", "61"), ("--port", "0")):
        with pytest.raises(SystemExit):
            args(tmp_path, option, value)
    with pytest.raises(SystemExit):
        args(tmp_path, "--key", "private-token")
    key = tmp_path / "key.txt"
    key.chmod(0o644)
    with pytest.raises(ValueError):
        live_observer.read_key(key)
    key.chmod(0o600)
    key.write_text("two\nlines\n", encoding="utf-8")
    with pytest.raises(ValueError):
        live_observer.read_key(key)


def test_jsonl_caps_and_first_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_observer, "EVENT_LIMIT", 2)
    writer = live_observer.BoundedJsonl(tmp_path / "events.jsonl", byte_limit=1000)
    writer.event({"status": "clock"})
    assert not writer.first_accepted.is_set()
    writer.event({"status": "accepted", "sequence": 1})
    assert writer.first_accepted.is_set()
    assert writer.first_accepted_ns is not None
    with pytest.raises(live_observer.OutputLimit):
        writer.event({"status": "accepted", "sequence": 2})
    writer.close()
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 2
    assert stat.S_IMODE((tmp_path / "events.jsonl").stat().st_mode) == 0o600
    small = live_observer.BoundedJsonl(tmp_path / "small.jsonl", byte_limit=8)
    with pytest.raises(live_observer.OutputLimit):
        small.event({"status": "accepted"})
    small.close()


class FakeReceiver:
    def __init__(self, sink, order: list[str]):
        self.sink = sink
        self.order = order
        self.failed = False

    def close(self):
        self.order.append("receiver.close")


class FakeServer:
    def __init__(self, receiver: FakeReceiver, order: list[str], *, emit=True, overflow=False):
        self.receiver = receiver
        self.order = order
        self.emit = emit
        self.overflow = overflow
        self.stop = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.order.append("server.exit")

    def serve_forever(self):
        if self.emit:
            self.receiver.sink({"status": "accepted", "sequence": 1})
            if self.overflow:
                try:
                    self.receiver.sink({"status": "accepted", "sequence": 2})
                except live_observer.OutputLimit:
                    self.receiver.failed = True
        self.stop.wait(timeout=3)

    def shutdown(self):
        self.order.append("server.shutdown")
        self.stop.set()


@pytest.mark.parametrize(
    ("emit", "overflow", "deadline", "expected"),
    [(True, False, 180, "duration_complete"),
     (False, False, 0.05, "connection_timeout"),
     (True, True, 180, "output_limit")],
)
def test_run_windows_and_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    emit: bool, overflow: bool, deadline: float, expected: str,
) -> None:
    order: list[str] = []
    parsed = args(tmp_path)
    monkeypatch.setattr(live_observer, "CONNECTION_WAIT_SECONDS", deadline)
    if overflow:
        monkeypatch.setattr(live_observer, "EVENT_LIMIT", 1)

    def make_receiver(**kwargs):
        receiver = FakeReceiver(kwargs["event_sink"], order)
        return receiver

    def make_server(receiver, host, port):
        assert (host, port) == ("127.0.0.1", 8766)
        return FakeServer(receiver, order, emit=emit, overflow=overflow)

    monkeypatch.setattr(live_observer, "create_receiver", make_receiver)
    monkeypatch.setattr(live_observer, "serve_live", make_server)
    assert live_observer.run(parsed) == (0 if expected == "duration_complete" else 1)
    output = parsed.output_dir
    footer = json.loads((output / "footer.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert footer["outcome"] == expected
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert order.index("receiver.close") < order.index("server.shutdown") < order.index("server.exit")
    assert manifest["conditions"]["connection_wait_seconds"] == deadline
    if emit:
        assert manifest["window"]["first_accepted_monotonic_ns"] is not None
    else:
        assert manifest["window"]["first_accepted_monotonic_ns"] is None
    assert "private-token" not in (output / "manifest.json").read_text()
    assert "private-token" not in (output / "events.jsonl").read_text()


def test_existing_output_dir_is_rejected(tmp_path: Path) -> None:
    parsed = args(tmp_path)
    parsed.output_dir.mkdir()
    with pytest.raises(FileExistsError):
        live_observer.run(parsed)


def test_model_mode_loads_adapter_and_records_sha(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from script import onnx_detector

    model = tmp_path / "cat.onnx"
    model.write_bytes(b"model-fixture")
    parsed = args(tmp_path, "--model", str(model))
    order: list[str] = []
    detector = object()
    monkeypatch.setattr(live_observer, "CONNECTION_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(live_observer.importlib.metadata, "version", lambda name: "test")
    monkeypatch.setattr(onnx_detector, "OnnxYoloDetector", lambda *a, **kw: detector)

    def make_receiver(**kwargs):
        assert kwargs["detector"] is detector
        return FakeReceiver(kwargs["event_sink"], order)

    monkeypatch.setattr(live_observer, "create_receiver", make_receiver)
    monkeypatch.setattr(live_observer, "serve_live",
                        lambda receiver, host, port: FakeServer(receiver, order, emit=False))
    assert live_observer.run(parsed) == 1
    manifest = json.loads((parsed.output_dir / "manifest.json").read_text())
    assert manifest["conditions"]["model_sha256"] == live_observer._sha(model)
    assert manifest["conditions"]["model_threshold"] == 0.5


def test_footer_hashes_closed_events_and_final_window(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    parsed = args(tmp_path)
    monkeypatch.setattr(live_observer, "create_receiver",
                        lambda **kw: FakeReceiver(kw["event_sink"], order))
    monkeypatch.setattr(live_observer, "serve_live",
                        lambda receiver, host, port: FakeServer(receiver, order))
    assert live_observer.run(parsed) == 0
    output = parsed.output_dir
    footer = json.loads((output / "footer.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert footer["events_sha256"] == hashlib.sha256((output / "events.jsonl").read_bytes()).hexdigest()
    assert footer["final_window"] == manifest["window"]
    finalized = footer.pop("finalized_sha256")
    canonical = json.dumps(footer, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode()
    assert finalized == hashlib.sha256(canonical).hexdigest()


def test_late_accepted_frame_does_not_start_measurement(monkeypatch: pytest.MonkeyPatch,
                                                        tmp_path: Path) -> None:
    writer = live_observer.BoundedJsonl(tmp_path / "events.jsonl")
    writer.first_accepted_ns = 181_000_000_000
    writer.first_accepted_utc = "late"
    writer.first_accepted.set()
    ticks = iter((0, 182_000_000_000))
    monkeypatch.setattr(live_observer.time, "monotonic_ns", lambda: next(ticks))
    receiver = FakeReceiver(writer.event, [])
    thread = threading.current_thread()
    window = {"first_accepted_monotonic_ns": None, "measurement_end_monotonic_ns": None}
    assert live_observer._monitor(receiver, thread, writer, 1, 0, window) == "connection_timeout"
    assert window["measurement_end_monotonic_ns"] is None
    writer.close()


def test_model_mutation_during_load_is_rejected(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from script import onnx_detector
    model = tmp_path / "cat.onnx"
    model.write_bytes(b"before")
    parsed = args(tmp_path, "--model", str(model))
    monkeypatch.setattr(live_observer.importlib.metadata, "version", lambda name: "test")

    def mutate(*unused_args, **unused_kwargs):
        model.write_bytes(b"after")
        return object()

    monkeypatch.setattr(onnx_detector, "OnnxYoloDetector", mutate)
    assert live_observer.run(parsed) == 1
    footer = json.loads((parsed.output_dir / "footer.json").read_text())
    assert footer["outcome"] == "failed"
    assert footer["error_type"] == "ValueError"


def test_thread_start_failure_real_server_exits(tmp_path: Path) -> None:
    parsed = args(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    code = """import json, sys
from script import live_observer as module
module.threading.Thread.start = lambda self: (_ for _ in ()).throw(RuntimeError('start failed'))
parsed = module.parse_args(['--key-file', sys.argv[1], '--output-dir', sys.argv[2],
                            '--duration-seconds', '1', '--port', sys.argv[3]])
sys.exit(module.run(parsed))
"""
    completed = subprocess.run([sys.executable, "-c", code, str(parsed.key_file),
                                str(parsed.output_dir), str(port)],
                               capture_output=True, text=True, timeout=5, check=False)
    assert completed.returncode == 1
    footer = json.loads((parsed.output_dir / "footer.json").read_text())
    assert footer["outcome"] == "failed"
    assert footer["error_type"] == "RuntimeError"
    assert (parsed.output_dir / "manifest.json").exists()


def test_rejects_untrusted_output_parent(tmp_path: Path) -> None:
    parsed = args(tmp_path)
    tmp_path.chmod(0o777)
    try:
        with pytest.raises(ValueError, match="parent"):
            live_observer.run(parsed)
    finally:
        tmp_path.chmod(0o700)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [("receiver", "sink_failed"), ("thread", "server_stopped"),
     ("serve_live", "failed"), ("shutdown", "teardown_failed")],
)
def test_failure_outcomes_still_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, expected: str,
) -> None:
    parsed = args(tmp_path)
    order: list[str] = []

    def make_receiver(**kwargs):
        receiver = FakeReceiver(kwargs["event_sink"], order)
        receiver.failed = failure == "receiver"
        return receiver

    class FailureServer(FakeServer):
        def serve_forever(self):
            if failure != "thread":
                super().serve_forever()

        def shutdown(self):
            super().shutdown()
            if failure == "shutdown":
                raise RuntimeError("teardown failure")

    def make_server(receiver, host, port):
        if failure == "serve_live":
            raise RuntimeError("listener failure")
        return FailureServer(receiver, order, emit=False)

    monkeypatch.setattr(live_observer, "create_receiver", make_receiver)
    monkeypatch.setattr(live_observer, "serve_live", make_server)
    assert live_observer.run(parsed) == 1
    footer = json.loads((parsed.output_dir / "footer.json").read_text())
    manifest = json.loads((parsed.output_dir / "manifest.json").read_text())
    assert footer["outcome"] == expected
    assert manifest["outcome"] == expected
    assert footer["events_sha256"] == live_observer._sha(parsed.output_dir / "events.jsonl")
    assert "receiver.close" in order


def test_rejects_other_users_sticky_output_parent(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    parsed = args(tmp_path)
    real_stat = Path.stat

    def parent_stat(path, *positional, **kwargs):
        if path == tmp_path:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o1777, st_uid=os.geteuid() + 1000)
        return real_stat(path, *positional, **kwargs)

    monkeypatch.setattr(Path, "stat", parent_stat)
    with pytest.raises(ValueError, match="parent"):
        live_observer.run(parsed)
    assert not parsed.output_dir.exists()
