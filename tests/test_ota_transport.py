"""Loopback-only tests. No device, firmware update, credentials or network config."""

import errno
import hashlib
import json
import logging
import socket
import sys
from types import ModuleType, SimpleNamespace

import pytest
from script import ota_transport as t


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "image.bin"
    path.write_bytes(b"private-image-content" * 16)
    path.chmod(0o400)
    return path


def uploader(callback):
    module = ModuleType("fake_uploader")
    module.socket, module.sys, module.logging = socket, sys, logging
    module.PROGRESS, module.TIMEOUT = True, 99
    module.serve = lambda *args: callback(module, *args)
    return module


def upload(module, image, **extra):
    options = {
        "local_host": "127.0.0.1",
        "remote_host": "127.0.0.1",
        "remote_port": 3232,
        "password": "private-password",
        "image": image,
        "expected_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
    }
    return t.upload(module, **{**options, **extra})


def exchange(module, remote, local, remote_port, port, password, filename):
    assert remote == local == "127.0.0.1" and remote_port == 3232
    assert password == "private-password" and filename.endswith("image.bin")
    # Reservation remains held before the stock uploader calls bind/listen.
    with socket.socket() as contender, pytest.raises(OSError) as occupied:
        contender.bind((local, port))
    assert occupied.value.errno == errno.EADDRINUSE
    listener = module.socket.socket(module.socket.AF_INET, module.socket.SOCK_STREAM)
    listener.bind((local, port))
    listener.listen(1)
    listener.settimeout(1)
    with socket.create_connection((local, port), timeout=1) as client:
        connection, _ = listener.accept()
        connection.sendall(b"private-image-content")
        assert client.recv(100) == b"private-image-content"
        client.sendall(b"OK")
        assert connection.recv(100) == b"OK"
        connection.shutdown(socket.SHUT_WR)
        connection.close()
        assert client.recv(1) == b""
    listener.close()
    return 0


def test_two_uploads_avoid_occupied_port_and_time_wait_and_restore_module(image):
    module = uploader(exchange)
    original = vars(module).copy()
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        fixed_port = busy.getsockname()[1]
        first, second = upload(module, image), upload(module, image)
    assert first["ok"] and second["ok"]
    assert first["listen_port"] != second["listen_port"]
    assert fixed_port not in {first["listen_port"], second["listen_port"]}
    assert not first["events"] and not second["events"]
    assert vars(module) == original
    assert socket.socket is original["socket"].socket


def test_timeouts_have_bounded_metadata_without_payload(image):
    def timeouts(module, *args):
        udp = module.socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(0.001)
        for _ in range(12):
            with pytest.raises(TimeoutError):
                udp.recv(1)
        return 1

    report = upload(uploader(timeouts), image)
    assert not report["ok"] and len(report["events"]) == 8 and report["events_lost"] == 4
    assert all(
        e == {"phase": "udp_recv", "kind": "timeout", "errno": None} for e in report["events"]
    )
    assert len(json.dumps(report)) < 2048


@pytest.mark.parametrize(
    "failure", [RuntimeError("private-password"), SystemExit(1), SystemExit(0)]
)
def test_stock_output_and_exception_text_are_discarded_and_state_restored(image, capsys, failure):
    def failing(module, *args):
        module.sys.stderr.write("private-password private-image-content")
        module.sys.stdout.write("private-password")
        module.logging.error("%s", "private-password")
        raise failure

    module = uploader(failing)
    original = vars(module).copy()
    report = upload(module, image)
    assert not report["ok"] and report["events"]
    assert "private" not in json.dumps(report)
    assert capsys.readouterr() == ("", "")
    assert vars(module) == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_sha256", "wrong"),
        ("remote_port", True),
        ("remote_port", 0),
        ("local_host", "private-password"),
        ("remote_host", "::1"),
        ("password", ""),
    ],
)
def test_invalid_configuration_never_invites_device(image, field, value):
    module = uploader(lambda *args: pytest.fail("uploader started"))
    report = upload(module, image, **{field: value})
    assert not report["ok"] and report["listen_port"] is None
    assert report["events"][-1]["phase"] == "validate"
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize("kind", ["writable", "empty", "oversize", "symlink"])
def test_invalid_image_never_invites_device(image, kind):
    image.chmod(0o600)
    if kind == "empty":
        image.write_bytes(b"")
    if kind == "oversize":
        image.write_bytes(b"x" * (2 * 1024**2 + 1))
    if kind != "writable":
        image.chmod(0o400)
    if kind == "symlink":
        link = image.with_name("link.bin")
        link.symlink_to(image)
        image = link
    report = upload(uploader(lambda *a: pytest.fail("uploader started")), image)
    assert not report["ok"] and report["listen_port"] is None


def test_reservation_error_keeps_errno_but_not_message(image, monkeypatch):
    def unavailable(*args):
        raise OSError(errno.EADDRNOTAVAIL, "private-password")

    monkeypatch.setattr(
        t,
        "socket",
        SimpleNamespace(socket=unavailable, AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM),
    )
    report = upload(uploader(lambda *a: pytest.fail("uploader started")), image)
    assert report["events"] == [{"phase": "reserve", "kind": "error", "errno": errno.EADDRNOTAVAIL}]
    assert "private" not in json.dumps(report)


def test_rejected_bind_records_phase_and_closes_reserved_listener(image):
    def mismatched(module, *args):
        stream = module.socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        stream.bind(("private-password", 123))

    report = upload(uploader(mismatched), image)
    assert not report["ok"] and report["events"][-1]["phase"] == "tcp_bind"
    assert "private" not in json.dumps(report)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", report["listen_port"]))


def test_socket_budget_closes_all_resources(image):
    sockets = []

    def excess(module, *args):
        sockets.extend(module.socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(17))

    report = upload(uploader(excess), image)
    assert not report["ok"] and sockets
    assert all(s.fileno() == -1 for s in sockets)


def test_transmitted_snapshot_survives_source_replacement_and_mutation(image):
    original = image.read_bytes()

    def changed(module, remote, local, remote_port, port, password, filename):
        image.unlink()
        image.write_bytes(b"replacement")
        image.write_bytes(b"changed again")
        assert module.os.path.getsize(filename) == len(original)
        with module.open(filename, "rb") as first:
            assert first.read() == original
        with module.open(filename, "rb") as second:
            assert second.read() == original
        return 0

    assert upload(uploader(changed), image)["ok"]


@pytest.mark.parametrize("phase", ["listen", "accept", "sendto", "sendall"])
def test_socket_failure_restores_state_closes_resources_and_hides_text(image, phase):
    class FailedSocket:
        closed = False

        def close(self):
            self.closed = True

        def __getattr__(self, name):
            def fail(*args):
                raise OSError(errno.EIO, "private-password")

            return fail

    raw = FailedSocket()

    def failed(module, *args):
        proxy = module.socket.wrap(raw, "tcp" if phase != "sendto" else "udp")
        getattr(proxy, phase)()

    module = uploader(failed)
    original = vars(module).copy()
    report = upload(module, image)
    assert not report["ok"] and raw.closed and vars(module) == original
    assert report["events"][-1]["phase"].endswith(phase)
    assert report["events"][-1]["errno"] == errno.EIO
    assert "private" not in json.dumps(report)
