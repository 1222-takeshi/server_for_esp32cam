"""Bounded private Unix RPC. Clients use ordinary (gevent-compatible) socket IO."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import socketserver
import stat
import struct
import time
from pathlib import Path
from threading import BoundedSemaphore

REQUEST_LIMIT = 1024
JSON_LIMIT = 64 * 1024
REPLY_LIMIT = 128 * 1024
PREFIX = struct.Struct(">I")
IDENTITY = b"esp32cam-operating-ipc-v1\n"


def _read(connection, count, deadline):
    data = bytearray()
    while len(data) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Control request timed out")
        connection.settimeout(remaining)
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise OSError("Incomplete control message")
        data.extend(chunk)
    return bytes(data)


def call(path, payload):
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > REQUEST_LIMIT:
        raise ValueError("Control request too large")
    deadline = time.monotonic() + .5
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(.5)
        connection.connect(str(path))
        connection.sendall(PREFIX.pack(len(raw)) + raw)
        length, = PREFIX.unpack(_read(connection, PREFIX.size, deadline))
        if length > REPLY_LIMIT:
            raise ValueError("Control reply too large")
        return _read(connection, length, deadline)


def call_json(path, payload):
    raw = call(path, payload)
    if len(raw) > JSON_LIMIT:
        raise ValueError("Control JSON too large")
    result = json.loads(raw)
    if type(result) is not dict:
        raise ValueError("Invalid control reply")
    return result


def _prepare(path):
    """Only a private managed directory may reclaim its own stale socket."""
    if not path.is_absolute() or path.name != "control.sock":
        raise ValueError("Use an absolute private-directory/control.sock path")
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Control directory must be owned, private and not a symlink")
    allowed = {".operating-ipc", ".lock", path.name}
    if any(entry.name not in allowed for entry in parent.iterdir()):
        raise ValueError("Control directory contains unrelated data")
    marker = parent / ".operating-ipc"
    if not marker.exists() and not marker.is_symlink():
        if list(parent.iterdir()):
            raise ValueError("Control directory is not managed")
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(IDENTITY)
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode) or source.read(64) != IDENTITY:
            raise ValueError("Invalid control directory marker")
    lock = os.open(parent / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Invalid control directory lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Refusing to replace unrelated control path")
            path.unlink()
        return lock
    except BaseException:
        os.close(lock)
        raise


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            deadline = time.monotonic() + .5
            length, = PREFIX.unpack(_read(self.request, PREFIX.size, deadline))
            if not 0 < length <= REQUEST_LIMIT:
                return
            request = json.loads(_read(self.request, length, deadline))
            if type(request) is not dict:
                return
            reply = self.server.dispatch(request)
            is_binary = type(reply) is bytes
            raw = reply if is_binary else json.dumps(reply, allow_nan=False).encode()
            if len(raw) > (REPLY_LIMIT if is_binary else JSON_LIMIT):
                raise ValueError("Control reply too large")
            self.request.settimeout(.5)
            self.request.sendall(PREFIX.pack(len(raw)) + raw)
        except (OSError, ValueError, TypeError, RecursionError):
            return  # No payloads, credentials, or per-request errors in logs.


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 4

    def __init__(self, path, dispatch):
        from gevent.monkey import is_module_patched
        if is_module_patched("threading"):
            raise RuntimeError("ControlServer requires a native process")
        self.path, self.dispatch = Path(path), dispatch
        self.slots = BoundedSemaphore(4)
        self.lock_fd = _prepare(self.path)
        self.inode = None
        try:
            super().__init__(str(self.path), _Handler)
            self.inode = self.path.lstat().st_ino
            self.path.chmod(0o600)
        except BaseException:
            self.server_close()
            raise

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def server_close(self):
        super().server_close()
        try:
            info = self.path.lstat()
            if self.inode is not None and stat.S_ISSOCK(info.st_mode) and info.st_ino == self.inode:
                self.path.unlink()
        except FileNotFoundError:
            pass
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
