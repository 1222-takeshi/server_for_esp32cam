"""Bounded stdout/stderr collection outside the recognition/request process."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import queue
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

_MARKER = ".esp32cam-runtime-logs"
_IDENTITY = "esp32cam-runtime-logs-v1\n"
_INPUT_LIMIT = 8192


class LogBudget:
    def __init__(self, directory: Path, *, max_bytes=10 * 1024 * 1024, backups=2,
                 line_bytes=8192, records_per_minute=60, bytes_per_minute=64 * 1024, clock=time.monotonic):
        values = (max_bytes, backups, line_bytes, records_per_minute, bytes_per_minute)
        if (any(type(value) is not int for value in values)
                or not 512 <= max_bytes <= 1024**3 or not 0 <= backups <= 9
                or not 256 <= line_bytes <= min(max_bytes, _INPUT_LIMIT)
                or not 1 <= records_per_minute <= 10000
                or not line_bytes <= bytes_per_minute <= 10 * 1024 * 1024):
            raise ValueError("Invalid log budget")
        self.directory = Path(directory)
        self.max_bytes, self.backups, self.line_bytes = max_bytes, backups, line_bytes
        self.limit, self.clock = records_per_minute, clock
        self.byte_limit, self.used_bytes = bytes_per_minute, 0
        self._seen = set()
        self._lock_file = None
        self.window = clock()
        self.used = self.suppressed = self.truncated = self.write_errors = 0
        self.queue_dropped = 0
        try:
            self._prepare()
        except OSError:
            self.write_errors += 1  # Storage failure must not prevent child startup.

    def _prepare(self):
        directory = self.directory
        if directory.is_symlink():
            raise ValueError("Log directory must not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Log directory must be privately owned")
        files = list(directory.iterdir())
        for path in files:
            valid = (path.name in {_MARKER, "runtime.log", ".writer.lock"}
                     or re.fullmatch(r"runtime\.log\.[1-9]", path.name))
            if not valid or path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("Directory contains unmanaged data")
        marker = directory / _MARKER
        if files and not marker.exists():
            raise ValueError("Existing data is not a managed log directory")
        if marker.exists():
            if marker.stat().st_size != len(_IDENTITY) or marker.read_text() != _IDENTITY:
                raise ValueError("Invalid managed log directory")
        else:
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise ValueError("Concurrent log directory initialization; retry startup") from exc
            with os.fdopen(fd, "w") as handle:
                handle.write(_IDENTITY)
        if self._lock_file is None:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            handle = os.fdopen(os.open(directory / ".writer.lock", flags, 0o600), "r+b")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.close()
                raise ValueError("Log directory already has an active collector") from exc
            self._lock_file = handle
        for old in directory.glob("runtime.log*"):
            generation = 0 if old.name == "runtime.log" else int(old.suffix[1:])
            if generation > self.backups or old.stat().st_size > self.max_bytes:
                old.unlink()

    def _append(self, payload):
        self._prepare()
        path = self.directory / "runtime.log"
        size = path.stat().st_size if path.exists() else 0
        if size + len(payload) > self.max_bytes:
            if self.backups:
                for number in range(self.backups, 0, -1):
                    target = self.directory / f"runtime.log.{number}"
                    previous = path if number == 1 else self.directory / f"runtime.log.{number - 1}"
                    if previous.exists():
                        previous.replace(target)
            elif path.exists():
                path.unlink()
        # A lowered generation limit also removes only previously managed logs.
        for old in self.directory.glob("runtime.log.*"):
            if int(old.suffix[1:]) > self.backups:
                old.unlink()
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags, 0o600), "ab") as handle:
            handle.write(payload)

    def _record(self, fields, *, limited=False):
        fields = {"timestamp": datetime.now(timezone.utc).isoformat(), **fields}
        encode = lambda: (json.dumps(fields, ensure_ascii=False) + "\n").encode()  # noqa: E731
        payload = encode()
        if len(payload) > self.line_bytes:
            self.truncated += 1
            fields["message"] = str(fields.get("message", ""))[:(self.line_bytes - 160) // 6]
            fields["truncated"] = True
            payload = encode()
        if len(payload) > self.line_bytes:
            payload = (json.dumps({"type": fields["type"], "truncated": True}) + "\n").encode()
        if limited:
            if self.used_bytes + len(payload) > self.byte_limit:
                self.suppressed += 1
                return
            self.used_bytes += len(payload)
        try:
            self._append(payload)
        except (OSError, ValueError):
            self.write_errors += 1

    def tick(self, *, final=False):
        if final or self.clock() - self.window >= 60:
            self._record({"type": "runtime.summary", "suppressed": self.suppressed,
                          "queue_dropped": self.queue_dropped, "truncated": self.truncated,
                          "write_errors": self.write_errors})
            self.window, self.used, self.used_bytes = self.clock(), 0, 0
            self._seen.clear()

    def line(self, message, *, truncated=False):
        self.tick()
        identity = hashlib.sha256(message.encode("utf-8")).digest()
        if self.used >= self.limit or identity in self._seen:
            self.suppressed += 1
            return
        self.used += 1
        self._seen.add(identity)
        if truncated:
            self.truncated += 1
        self._record({"type": "output", "message": message}, limited=True)


def run(command, budget):
    if not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED", "WNOHANG", "P_PID")):
        raise ValueError("Log collection requires POSIX waitid/WNOWAIT support")
    pending = queue.Queue(maxsize=64)  # <=64 * 8KiB input bytes plus bounded metadata.
    stopped = threading.Event()

    def write_logs():
        while not stopped.is_set() or not pending.empty():
            try:
                data, truncated = pending.get(timeout=.2)
            except queue.Empty:
                budget.tick()
                continue
            budget.line(data.decode("utf-8", "replace"), truncated=truncated)
        budget.tick(final=True)

    writer = threading.Thread(target=write_logs, daemon=True)
    writer.start()
    child = None
    stopping_at = None
    first_signal = None

    def forward(signum, _frame):
        nonlocal stopping_at, first_signal
        first_signal = first_signal or signum
        stopping_at = stopping_at or time.monotonic()
        if child is not None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signum)

    def exited():
        # Leave the leader unreaped until group cleanup: its PID/PGID cannot
        # be reused by an unrelated process while descendants are terminated.
        return os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None

    handlers = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True, bufsize=0)
        if stopping_at is not None:
            forward(first_signal, None)
        buffer = bytearray()
        truncated = False
        exited_at = None

        def submit():
            nonlocal truncated
            try:
                pending.put_nowait((bytes(buffer), truncated))
            except queue.Full:
                budget.queue_dropped += 1
            buffer.clear()
            truncated = False

        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                now = time.monotonic()
                if stopping_at is not None and now - stopping_at > 5 and not exited():
                    forward(signal.SIGKILL, None)
                if exited():
                    exited_at = exited_at or now
                    if now - exited_at > 2:
                        break  # A descendant holding stdout must not hang collection forever.
                if not selector.select(.1):
                    continue
                data = os.read(child.stdout.fileno(), 16384)
                if not data:
                    break
                for part in data.splitlines(keepends=True):
                    content = part[:-1] if part.endswith(b"\n") else part
                    remaining = _INPUT_LIMIT - len(buffer)
                    buffer.extend(content[:remaining])
                    truncated |= len(content) > remaining
                    if part.endswith(b"\n"):
                        submit()
            if buffer or truncated:
                submit()
        while not exited():
            if stopping_at is not None and time.monotonic() - stopping_at > 5:
                forward(signal.SIGKILL, None)
            time.sleep(.05)
    finally:
        if child is not None:
            forward(signal.SIGTERM, None)
            deadline = time.monotonic() + 5
            while not exited() and time.monotonic() < deadline:
                time.sleep(.05)
            # Also stop descendants after the leader exits, even if stdout was
            # redirected or the leader died before its workers. PGID is pinned.
            time.sleep(.1)
            forward(signal.SIGKILL, None)
            returncode = child.wait(timeout=2)
            child.stdout.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        stopped.set()
        writer.join(timeout=1)  # A blocked disk writer cannot block recognition shutdown.
    return returncode if returncode >= 0 else 128 - returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--backups", type=int, default=2)
    parser.add_argument("--line-bytes", type=int, default=8192)
    parser.add_argument("--records-per-minute", type=int, default=60)
    parser.add_argument("--bytes-per-minute", type=int, default=64 * 1024)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a child command is required after --")
    try:
        budget = LogBudget(args.directory, max_bytes=args.max_bytes, backups=args.backups, line_bytes=args.line_bytes,
                           records_per_minute=args.records_per_minute, bytes_per_minute=args.bytes_per_minute)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        return run(command, budget)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
