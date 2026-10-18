"""Optional, bounded diagnostic output for the native recognition process."""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from gevent import monkey

from script.log_runner import LogBudget


class _Limit(RuntimeError):
    pass


class _Oversized(ValueError):
    pass


def _bounded_copy(event):
    """Snapshot plain JSON data with fixed work before whole-record encoding."""
    remaining, nodes = 8192 - 256, 1024

    def consume(size):
        nonlocal remaining
        remaining -= size
        if remaining < 0:
            raise _Oversized

    def copy(value, depth=0):
        nonlocal nodes
        nodes -= 1
        if nodes < 0 or depth > 6:
            raise _Oversized
        kind = type(value)
        if kind is dict:
            if len(value) > nodes:
                raise _Oversized
            consume(2 + max(0, len(value) - 1))
            result = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("Expected string keys")
                result[copy(key, depth + 1)] = copy(item, depth + 1)
                consume(1)
            return result
        if kind is list:
            if len(value) > nodes:
                raise _Oversized
            consume(2 + max(0, len(value) - 1))
            return [copy(item, depth + 1) for item in value]
        if kind is str:
            if len(value) > remaining:  # Never encode an arbitrarily large string.
                raise _Oversized
            consume(len(json.dumps(value, ensure_ascii=False).encode()))
        elif value is None or kind is bool:
            consume(5)
        elif kind is int:
            if value.bit_length() > 64:
                raise _Oversized
            consume(len(str(value)))
        elif kind is float and math.isfinite(value):
            consume(len(repr(value)))
        else:
            raise ValueError("Expected finite plain JSON values")
        return value

    if type(event) is not dict:
        raise ValueError("Expected a diagnostic object")
    return copy(event)


class _DiagnosticBudget(LogBudget):
    marker_name = ".esp32cam-diagnostic-logs"
    directory_identity = "esp32cam-diagnostic-logs-v1\n"

    def __init__(self, directory, deadline, byte_limit, clock):
        self.deadline, self.session_byte_limit = deadline, byte_limit
        self.attempted_bytes = self.written_bytes = 0
        super().__init__(directory, max_bytes=8 * 1024**2, backups=3, clock=clock)
        if self.write_errors:
            self.close()
            raise OSError("Diagnostic storage is unavailable")

    def write(self, payload):
        if self.clock() >= self.deadline:
            raise _Limit("expired")
        if self.attempted_bytes + len(payload) > self.session_byte_limit:
            raise _Limit("byte_limit")
        self.attempted_bytes += len(payload)  # Includes potentially partial failed writes.
        self._append(payload)
        self.written_bytes += len(payload)

    def close(self):
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None


class DiagnosticLog:
    def __init__(self, directory: Path, *, duration_seconds=600, byte_limit=32 * 1024**2,
                 clock=time.monotonic):
        if (type(duration_seconds) is not int or not 1 <= duration_seconds <= 600
                or type(byte_limit) is not int or not 512 <= byte_limit <= 32 * 1024**2):
            raise ValueError("Invalid diagnostic limits")
        if monkey.is_module_patched("threading"):
            raise ValueError("Diagnostics require an unpatched native process")
        self.directory = Path(directory)
        self.clock, self.deadline = clock, clock() + duration_seconds
        self._budget = _DiagnosticBudget(directory, self.deadline, byte_limit, clock)
        self._queue = queue.Queue(maxsize=8)  # <=8*8KiB queued + one active record.
        self._lock, self._stopped = threading.Lock(), threading.Event()
        self._encode_lock = threading.Lock()  # At most one bounded encoding in progress.
        self._state = "running"
        self._counts = {"written_records": 0, "queue_dropped": 0, "discarded": 0,
                        "oversized": 0, "invalid": 0, "errors": 0, "busy_dropped": 0}
        self._thread = threading.Thread(target=self._run, daemon=True, name="esp32cam-diagnostics")
        try:
            self._thread.start()
        except BaseException:
            self._budget.close()
            raise

    def _finish(self, reason):
        with self._lock:
            if self._state == "running":
                self._state = reason
            self._stopped.set()

    def submit(self, event: dict) -> bool:
        if self.clock() >= self.deadline:
            self._finish("expired")
        if self._stopped.is_set():
            return False
        if not self._encode_lock.acquire(blocking=False):
            with self._lock:
                self._counts["busy_dropped"] += 1
            return False
        try:
            return self._submit(event)
        finally:
            self._encode_lock.release()

    def _submit(self, event):
        with self._lock:
            if self._stopped.is_set():
                return False
            if self._queue.full():
                self._counts["queue_dropped"] += 1
                return False
        try:
            record = {"timestamp": datetime.now(timezone.utc).isoformat(),
                      "type": "diagnostic", "event": _bounded_copy(event)}
            payload = (json.dumps(record, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")) + "\n").encode()
        except _Oversized:
            with self._lock:
                self._counts["oversized"] += 1
            return False
        except (TypeError, ValueError, RuntimeError):
            with self._lock:
                self._counts["invalid"] += 1
            return False
        with self._lock:
            if self._stopped.is_set():
                return False
            if len(payload) > 8192:
                self._counts["oversized"] += 1
                return False
            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                self._counts["queue_dropped"] += 1
                return False
        return True

    def _run(self):
        try:
            while not self._stopped.is_set():
                if self.clock() >= self.deadline:
                    self._finish("expired")
                    break
                try:
                    payload = self._queue.get(timeout=.1)
                except queue.Empty:
                    continue
                if self._stopped.is_set():
                    with self._lock:
                        self._counts["discarded"] += 1
                    break
                try:
                    self._budget.write(payload)
                except _Limit as exc:
                    with self._lock:
                        self._counts["discarded"] += 1
                    self._finish(str(exc))
                except Exception:  # Diagnostic failure must never stop recognition.
                    with self._lock:
                        self._counts["errors"] += 1
                    self._finish("write_error")
                else:
                    with self._lock:
                        self._counts["written_records"] += 1
        finally:
            self._budget.close()
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                with self._lock:
                    self._counts["discarded"] += 1

    def status(self) -> dict:
        with self._lock:
            return {"state": self._state, **self._counts, "queued": self._queue.qsize(),
                    "written_bytes": self._budget.written_bytes,
                    "attempted_bytes": self._budget.attempted_bytes}

    def close(self, *, wait=True):
        self._finish("closed")
        if wait:
            self._thread.join(timeout=1)  # Disk stalls cannot hold up normal recognition/shutdown.
