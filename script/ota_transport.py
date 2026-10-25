"""Bounded diagnostics and a reserved ephemeral listener for an isolated OTA child.

The caller verifies the uploader module before import. A bounded immutable RAM
snapshot binds the verified bytes to transmission. Use only an isolated child.
"""

from collections import deque
from contextlib import ExitStack
import hashlib
import ipaddress
from io import BytesIO
from pathlib import Path
import socket
import stat
from threading import Lock
import time
from types import SimpleNamespace

_LOCK = Lock()
_PHASES = {"bind", "listen", "accept", "sendto", "recv", "sendall"}
_MISSING = object()
_IMAGE = "verified-image.bin"


class _Trace:
    def __init__(self):
        self.events = deque(maxlen=8)
        self.total = 0
        self.phase = "validate"

    def note(self, phase, error):
        number = getattr(error, "errno", None)
        number = number if type(number) is int and 0 <= number <= 4095 else None
        self.total += 1
        self.events.append(
            {
                "phase": phase,
                "kind": "timeout"
                if isinstance(error, TimeoutError)
                else "reported"
                if error is None
                else "error",
                "errno": number,
            }
        )


class _Socket:
    def __init__(self, raw, owner, kind, bound=False):
        self.raw, self.owner, self.kind, self.bound = raw, owner, kind, bound

    def __getattr__(self, name):
        target = getattr(self.raw, name)
        if name not in _PHASES:
            return target

        def call(*args, **kwargs):
            self.owner.trace.phase = f"{self.kind}_{name}"
            try:
                if name == "bind" and self.bound:
                    if args != (self.raw.getsockname(),) or kwargs:
                        raise ValueError("reserved address mismatch")
                    return None
                result = target(*args, **kwargs)
                if name == "accept":
                    connection, peer = result
                    return self.owner.wrap(connection, "tcp"), peer
                return result
            except (OSError, ValueError) as error:
                self.owner.trace.note(self.owner.trace.phase, error)
                raise

        return call


class _Sockets:
    def __init__(self, listener, stack, trace):
        self.listener, self.stack, self.trace = listener, stack, trace
        self.used, self.count = False, 1

    def __getattr__(self, name):
        return getattr(socket, name)

    def wrap(self, raw, kind, bound=False):
        if raw is self.listener:
            return _Socket(raw, self, kind, bound)
        if self.count >= 16:
            raw.close()
            raise ValueError("socket budget exceeded")
        self.count += 1
        self.stack.callback(raw.close)
        return _Socket(raw, self, kind, bound)

    def socket(self, family, kind, *args, **kwargs):
        if (
            args
            or kwargs
            or family != socket.AF_INET
            or kind not in {socket.SOCK_STREAM, socket.SOCK_DGRAM}
        ):
            raise ValueError("unsupported socket")
        if kind == socket.SOCK_STREAM:
            if self.used:
                raise ValueError("listener already consumed")
            self.used = True
            return self.wrap(self.listener, "tcp", bound=True)
        return self.wrap(socket.socket(family, kind, *args, **kwargs), "udp")


class _Sink:
    def write(self, data):
        return len(data)

    def flush(self):
        pass


class _Quiet:
    def __init__(self, trace):
        self.trace = trace

    def __getattr__(self, name):
        def discard(*args, **kwargs):
            if name in {"error", "exception", "critical"}:
                self.trace.note(self.trace.phase, None)

        return discard


def upload(espota, *, local_host, remote_host, remote_port, password, image, expected_sha256):
    """Use the stock invitation/auth/transfer protocol; return metadata, never text."""
    begun, trace, port, code = time.monotonic(), _Trace(), None, 1
    phase = "validate"
    with _LOCK, ExitStack() as stack:
        try:
            local_host = str(ipaddress.IPv4Address(local_host))
            remote_host = str(ipaddress.IPv4Address(remote_host))
            if type(remote_port) is not int or not 1 <= remote_port <= 65535:
                raise ValueError("invalid remote port")
            if not isinstance(password, str) or not 0 < len(password) <= 512:
                raise ValueError("invalid credential")
            image = Path(image)
            info = image.stat()
            if image.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222:
                raise ValueError("image must be immutable")
            if not 0 < info.st_size <= 2 * 1024**2:
                raise ValueError("invalid image size")
            with image.open("rb") as source:
                payload = source.read(2 * 1024**2 + 1)
            if (
                not 0 < len(payload) <= 2 * 1024**2
                or hashlib.sha256(payload).hexdigest() != expected_sha256
            ):
                raise ValueError("image hash mismatch")
            phase = "reserve"
            trace.phase = phase
            listener = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            listener.bind((local_host, 0))
            port = listener.getsockname()[1]
            old = {
                name: getattr(espota, name, _MISSING)
                for name in ("socket", "sys", "logging", "PROGRESS", "TIMEOUT", "open", "os")
            }

            def restore():
                for name, value in old.items():
                    if value is _MISSING:
                        vars(espota).pop(name, None)
                    else:
                        setattr(espota, name, value)

            stack.callback(restore)
            opened = 0

            def open_image(name, mode="rb"):
                nonlocal opened
                if name != _IMAGE or mode != "rb" or opened >= 4:
                    raise ValueError("unexpected image access")
                opened += 1
                return stack.enter_context(BytesIO(payload))

            def image_size(name):
                if name != _IMAGE:
                    raise ValueError("unexpected image path")
                return len(payload)

            espota.open = open_image
            espota.os = SimpleNamespace(path=SimpleNamespace(getsize=image_size))
            espota.socket = _Sockets(listener, stack, trace)
            espota.sys = SimpleNamespace(stderr=_Sink(), stdout=_Sink(), exit=old["sys"].exit)
            espota.logging, espota.PROGRESS, espota.TIMEOUT = _Quiet(trace), False, 10
            phase = "uploader"
            trace.phase = phase
            result = espota.serve(remote_host, local_host, remote_port, port, password, _IMAGE)
            code = result if type(result) is int and 0 <= result <= 255 else 1
        except SystemExit as error:
            trace.note("uploader_exit", error)
            code = error.code if type(error.code) is int and 1 <= error.code <= 255 else 1
        except Exception as error:
            trace.note(trace.phase, error)
    return {
        "schema": 1,
        "ok": code == 0,
        "exit_code": code,
        "listen_port": port,
        "elapsed_ms": int((time.monotonic() - begun) * 1000),
        "last_phase": trace.phase,
        "events": list(trace.events),
        "events_lost": max(0, trace.total - 8),
    }
