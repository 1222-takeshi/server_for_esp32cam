"""Bounded, standalone observer for the experimental ESP32-CAM live receiver."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from script.capture_receiver import create_receiver, serve_live

EVENT_LIMIT = 100_000
BYTE_LIMIT = 128 * 1024 * 1024
CONNECTION_WAIT_SECONDS = 180
REPO = Path(__file__).resolve().parents[1]


class OutputLimit(RuntimeError):
    """The event stream reached its configured count or byte cap."""


def _integer(low: int, high: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("expected an integer") from exc
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"expected {low}..{high}")
        return number
    return parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded ESP32-CAM live observer",
                                     allow_abbrev=False)
    parser.add_argument("--key-file", type=Path, required=True, help="private plaintext token file")
    parser.add_argument("--output-dir", type=Path, required=True, help="new private run directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_integer(1, 65535), default=8766)
    parser.add_argument("--camera-id", default="esp32cam-01")
    parser.add_argument("--duration-seconds", type=_integer(1, 3600), required=True)
    parser.add_argument("--warmup-seconds", type=_integer(0, 60), default=0)
    parser.add_argument("--model", type=Path, help="optional 320px cat ONNX model")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.camera_id):
        parser.error("camera ID must be 1..64 ASCII letters, digits, '-' or '_'")
    return args


def read_key(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("key file must be a private regular file")
        if not 0 < info.st_size <= 4096:
            raise ValueError("invalid key file size")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(4097)
    finally:
        if fd >= 0:
            os.close(fd)
    value = raw.removesuffix(b"\r\n").removesuffix(b"\n")
    if not value or len(value) > 4096 or any(byte < 33 or byte > 126 for byte in value):
        raise ValueError("key file must contain one nonempty printable ASCII token")
    return value.decode("ascii")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha(record: dict) -> str:
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _private_json(path: Path, content: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(content, output, indent=2, allow_nan=False)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class BoundedJsonl:
    def __init__(self, path: Path, *, byte_limit: int = BYTE_LIMIT):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)
        os.fchmod(fd, 0o600)
        self.handle = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()
        self.count = 0
        self.bytes = 0
        self.byte_limit = byte_limit
        self.closed = False
        self.limit_reached = False
        self.first_accepted = threading.Event()
        self.first_accepted_ns: int | None = None
        self.first_accepted_utc: str | None = None

    def event(self, payload: dict) -> None:
        # Receiver serializes sink calls. Never call receiver methods here.
        observed_ns, observed_utc = time.monotonic_ns(), _utc()
        record = {"kind": "receiver", "observed_monotonic_ns": observed_ns,
                  "observed_utc": observed_utc, **payload}
        line = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
        size = len(line.encode("utf-8"))
        with self.lock:
            if self.closed or self.count >= EVENT_LIMIT or self.bytes + size > self.byte_limit:
                self.limit_reached = True
                raise OutputLimit("JSONL cap reached or writer closed")
            self.handle.write(line)
            self.count += 1
            self.bytes += size
            if payload.get("status") == "accepted" and not self.first_accepted.is_set():
                self.first_accepted_ns = observed_ns
                self.first_accepted_utc = observed_utc
                self.first_accepted.set()

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self.handle.close()
                self.closed = True


def _monitor(receiver, server_thread: threading.Thread, writer: BoundedJsonl,
             duration_s: int, warmup_s: int, window: dict) -> str:
    ready_ns = time.monotonic_ns()
    window["listener_ready_monotonic_ns"] = ready_ns
    deadline_ns = ready_ns + int(CONNECTION_WAIT_SECONDS * 1_000_000_000)
    window["connection_deadline_monotonic_ns"] = deadline_ns
    while True:
        now = time.monotonic_ns()
        if window["first_accepted_monotonic_ns"] is None and writer.first_accepted.is_set():
            first = writer.first_accepted_ns
            if first is None or first > deadline_ns:
                return "connection_timeout"
            window["first_accepted_monotonic_ns"] = first
            window["first_accepted_utc"] = writer.first_accepted_utc
            window["measurement_start_monotonic_ns"] = first + warmup_s * 1_000_000_000
            window["measurement_end_monotonic_ns"] = (
                window["measurement_start_monotonic_ns"] + duration_s * 1_000_000_000
            )
        if writer.limit_reached:
            return "output_limit"
        if receiver.failed:
            return "sink_failed"
        if not server_thread.is_alive():
            return "server_stopped"
        if window["measurement_end_monotonic_ns"] is not None:
            if now >= window["measurement_end_monotonic_ns"]:
                return "duration_complete"
        elif now >= deadline_ns:
            return "connection_timeout"
        time.sleep(0.05)


def run(args: argparse.Namespace) -> int:
    output = args.output_dir.resolve(strict=False)
    if output == REPO or REPO in output.parents:
        raise ValueError("output directory must be outside the repository")
    if output.exists():
        raise FileExistsError(output)
    parent = output.parent.stat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid not in (0, os.geteuid())) or (
        parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX
    ):
        raise ValueError("output parent must be a trusted private or sticky directory")
    key = read_key(args.key_file)
    model_hash = None
    if args.model is not None:
        if not args.model.is_file() or args.model.suffix.lower() != ".onnx":
            raise ValueError("model must be an ONNX file")
        model_hash = _sha(args.model)
    source_sha = {name: _sha(REPO / name) for name in
                  ("script/live_observer.py", "script/capture_receiver.py",
                   "script/frame_protocol.py", "script/detector.py")}
    if args.model is not None:
        source_sha["script/onnx_detector.py"] = _sha(REPO / "script/onnx_detector.py")
    conditions = {"host": args.host, "port": args.port, "camera_id": args.camera_id,
                  "duration_seconds": args.duration_seconds,
                  "warmup_seconds": args.warmup_seconds,
                  "connection_wait_seconds": CONNECTION_WAIT_SECONDS,
                  "event_limit": EVENT_LIMIT, "byte_limit": BYTE_LIMIT,
                  "max_age_ms": 500, "mode": "onnx" if args.model else "capture",
                  "model_sha256": model_hash, "model_threshold": 0.5 if args.model else None,
                  "model_num_threads": 4 if args.model else None,
                  "credential_source": "private key file"}
    versions = {"python": sys.version.split()[0],
                "websockets": importlib.metadata.version("websockets"),
                "Pillow": importlib.metadata.version("Pillow")}
    if args.model is not None:
        versions.update({name: importlib.metadata.version(name)
                         for name in ("numpy", "onnxruntime")})
    window = {"listener_ready_monotonic_ns": None,
              "connection_deadline_monotonic_ns": None,
              "first_accepted_monotonic_ns": None, "first_accepted_utc": None,
              "measurement_start_monotonic_ns": None,
              "measurement_end_monotonic_ns": None}
    manifest = {"started_utc": _utc(), "conditions": conditions,
                "source_sha256": source_sha, "versions": versions, "window": window}
    os.mkdir(output, 0o700)
    _private_json(output / "manifest.json", manifest)
    writer = None
    receiver = None
    server = None
    thread = None
    cleaned = False
    outcome = "failed"
    error_type = None

    def stop_server() -> None:
        nonlocal cleaned, outcome, error_type
        if cleaned:
            return
        cleaned = True
        actions = (receiver.close if receiver is not None else None,
                   server.shutdown if server is not None else None,
                   (lambda: thread.join(timeout=2)) if thread is not None else None)
        for action in actions:
            if action is None:
                continue
            try:
                action()
            except Exception as exc:
                outcome = "teardown_failed"
                error_type = error_type or type(exc).__name__

    try:
        writer = BoundedJsonl(output / "events.jsonl")
        detector = None
        if args.model is not None:
            from script.onnx_detector import OnnxYoloDetector
            detector = OnnxYoloDetector(args.model, threshold=0.5, num_threads=4)
            if _sha(args.model) != model_hash:
                raise ValueError("model changed during load")
        receiver = create_receiver(
            camera_id=args.camera_id, device_api_key=key, detector=detector,
            event_sink=writer.event, max_age_ms=500,
            max_duration_s=CONNECTION_WAIT_SECONDS + args.warmup_seconds +
            args.duration_seconds + 10,
        )
        del key
        server = serve_live(receiver, host=args.host, port=args.port)
        candidate = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            candidate.start()
        except Exception:
            # websockets.Server.shutdown waits for serve_forever's socket_closed event.
            # A failed Thread.start leaves it unset; drive the closed-socket path here.
            server.socket.close()
            server.serve_forever()
            raise
        thread = candidate
        with server:
            try:
                outcome = _monitor(receiver, thread, writer, args.duration_seconds,
                                   args.warmup_seconds, window)
            finally:
                stop_server()
    except KeyboardInterrupt:
        outcome = "interrupted"
    except Exception as exc:
        if outcome != "teardown_failed":
            outcome = "failed"
        error_type = type(exc).__name__  # Never expose exception text or key.
    finally:
        stop_server()
        if writer is not None:
            try:
                writer.close()
            except Exception as exc:
                outcome = "teardown_failed"
                error_type = error_type or type(exc).__name__
        footer = {"outcome": outcome, "error_type": error_type, "finished_utc": _utc(),
                  "finished_monotonic_ns": time.monotonic_ns(),
                  "receiver_failed": receiver.failed if receiver is not None else None,
                  "events_recorded": writer.count if writer is not None else 0,
                  "jsonl_bytes": writer.bytes if writer is not None else 0,
                  "events_sha256": _sha(output / "events.jsonl") if writer is not None and writer.closed else None,
                  "final_window": dict(window)}
        footer["finalized_sha256"] = _canonical_sha(footer)
        _private_json(output / "footer.json", footer)
        manifest["window"] = window
        manifest["outcome"] = outcome
        _private_json(output / "manifest.json", manifest)
    print(json.dumps({"output_dir": str(output), "outcome": outcome}))
    return 0 if outcome == "duration_complete" else 1


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
