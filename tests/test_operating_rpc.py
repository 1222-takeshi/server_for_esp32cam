import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread

import pytest

from script.operating_rpc import ControlServer, JSON_LIMIT, PREFIX, REPLY_LIMIT, call, call_json


@pytest.fixture
def control_path():
    # Keep paths below macOS AF_UNIX's short pathname limit too.
    with tempfile.TemporaryDirectory(prefix="ec-rpc-", dir="/tmp") as directory:
        yield Path(directory) / "ipc/control.sock"


def start(path, dispatch):
    server = ControlServer(path, dispatch)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def finish(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_json_binary_boundaries_and_private_permissions(control_path):
    def dispatch(payload):
        if payload["op"] == "json":
            return {"mode": "eco", "motor": "hold"}
        return b"x" * payload["bytes"]

    server, thread = start(control_path, dispatch)
    try:
        assert control_path.stat().st_mode & 0o777 == 0o600
        assert control_path.parent.stat().st_mode & 0o777 == 0o700
        assert call_json(control_path, {"op": "json"})["motor"] == "hold"
        assert len(call(control_path, {"op": "binary", "bytes": REPLY_LIMIT})) == REPLY_LIMIT
        with pytest.raises(OSError):
            call(control_path, {"op": "binary", "bytes": REPLY_LIMIT + 1})
        with pytest.raises(ValueError):
            call(control_path, {"op": "x" * 1024})
        with pytest.raises(ValueError):
            call_json(control_path, {"op": "binary", "bytes": JSON_LIMIT + 1})
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(1)
            raw.connect(str(control_path))
            raw.sendall(PREFIX.pack(1025))
            assert raw.recv(1) == b""  # Rejected before reading attacker body.
    finally:
        finish(server, thread)
    assert not control_path.exists()
    # Managed marker/lock allow a new worker to bind after clean shutdown.
    replacement, replacement_thread = start(control_path, lambda _: {})
    finish(replacement, replacement_thread)


def test_four_handlers_and_overload_never_spawn_unbounded_threads(control_path):
    release, four = Event(), Event()
    entered = []

    def dispatch(payload):
        entered.append(payload)
        if len(entered) == 4:
            four.set()
        assert release.wait(2)
        return {"ok": True}

    server, thread = start(control_path, dispatch)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            running = [pool.submit(call_json, control_path, {"number": i}) for i in range(4)]
            assert four.wait(1)
            with pytest.raises(OSError):
                call_json(control_path, {"number": 5})
            assert len(entered) == 4
            release.set()
            assert all(job.result(timeout=1)["ok"] for job in running)
    finally:
        release.set()
        finish(server, thread)


def test_raw_paths_symlinks_and_duplicate_servers_are_protected(control_path):
    control_path.parent.mkdir(mode=0o700)
    raw = control_path.parent / "baseline.jpg"
    raw.write_bytes(b"immutable")
    with pytest.raises(ValueError, match="unrelated"):
        ControlServer(control_path, lambda _: {})
    assert raw.read_bytes() == b"immutable"
    raw.unlink()  # This test owns its synthetic fixture, not operational data.
    server, thread = start(control_path, lambda _: {})
    try:
        with pytest.raises(BlockingIOError):
            ControlServer(control_path, lambda _: {})
        assert call_json(control_path, {}) == {}
    finally:
        finish(server, thread)
    control_path.write_bytes(b"not a socket")
    with pytest.raises(ValueError, match="unrelated"):
        ControlServer(control_path, lambda _: {})
    assert control_path.read_bytes() == b"not a socket"
    control_path.unlink()
    target = control_path.parent / "target"
    target.write_bytes(b"immutable")
    # Remove unrelated target from the directory while retaining the symlink's
    # external destination to exercise the path-type guard directly.
    external = control_path.parent.parent / "external"
    target.rename(external)
    control_path.symlink_to(external)
    with pytest.raises(ValueError):
        ControlServer(control_path, lambda _: {})
    assert external.read_bytes() == b"immutable"


def test_stale_managed_socket_is_reclaimed(control_path):
    server, thread = start(control_path, lambda _: {})
    finish(server, thread)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(control_path))
    stale.close()
    replacement, thread = start(control_path, lambda _: {"ok": True})
    try:
        assert call_json(control_path, {}) == {"ok": True}
    finally:
        finish(replacement, thread)


def test_gevent_client_yields_during_slow_native_reply(control_path):
    release = Event()

    def dispatch(_):
        release.wait(.2)
        return {"ok": True}

    server, thread = start(control_path, dispatch)
    code = '''from gevent import monkey; monkey.patch_all()
import gevent,sys
from script.operating_rpc import call_json,ControlServer
ticks=[]
def tick():
    for _ in range(10):
        gevent.sleep(.01);ticks.append(1)
def invoke():
    reply=call_json(sys.argv[1],{})
    assert len(ticks)==10
    return reply
job=gevent.spawn(invoke)
ticker=gevent.spawn(tick)
gevent.joinall([job,ticker],raise_error=True)
assert job.value=={"ok":True} and len(ticks)==10
try: ControlServer(sys.argv[1],lambda _: {})
except RuntimeError: pass
else: raise AssertionError("native server accepted patched process")
'''
    try:
        result = subprocess.run([sys.executable, "-c", code, str(control_path)],
                                capture_output=True, timeout=3)
        assert result.returncode == 0, result.stderr.decode()
    finally:
        release.set()
        finish(server, thread)
