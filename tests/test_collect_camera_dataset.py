from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from PIL import Image

from scripts import collect_camera_dataset as collector

URL = "http://127.0.0.1:8080/stream/cameras/cam-1"


def jpeg(color: str) -> bytes:
    image = Image.new("RGB", (16, 12), color)
    stream = io.BytesIO()
    image.save(stream, format="JPEG")
    return stream.getvalue()


def part(data: bytes, *, length: int | None = None) -> bytes:
    return (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(data) if length is None else length).encode()
            + b"\r\n\r\n" + data + b"\r\n")


class Response(io.BytesIO):
    headers = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Opener:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        return Response(self.payload)


class Clock:
    def __init__(self, step=0.01):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def run_collect(tmp_path, payload: bytes, *, duration=5, max_images=2, clock=None,
                max_disk_bytes=50 * 1024 * 1024):
    clock = clock or Clock()
    return collector.collect(URL, "cam-1", tmp_path / "run", duration=duration,
                             sample_interval=1, max_images=max_images, opener=Opener(payload),
                             max_disk_bytes=max_disk_bytes,
                             clock=clock, sleep=clock.sleep,
                             utc_now=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))


@pytest.mark.parametrize("url", [
    "http://user:secret@127.0.0.1/stream/cameras/cam-1",
    "http://8.8.8.8/stream/cameras/cam-1",
    "http://127.0.0.1/stream/cameras/cam-1?token=secret",
    "http://127.0.0.1/stream/cameras/other",
    "file:///tmp/frames",
])
def test_url_rejects_unsafe_locations_and_credentials(url):
    with pytest.raises(ValueError):
        collector.validate_url(url, "cam-1")


def test_url_accepts_tailnet_and_rejects_invalid_camera():
    collector.validate_url("http://100.108.195.27:8080/stream/cameras/cam-1", "cam-1")
    with pytest.raises(ValueError):
        collector.validate_url(URL, "cam/1")
    with pytest.raises(urllib.error.HTTPError):
        collector.NoRedirect().redirect_request(
            urllib.request.Request(URL), None, 302, "", {}, "http://8.8.8.8")


def test_part_parser_accepts_exact_jpeg_and_rejects_partial_or_oversized():
    data = jpeg("red")
    response = Response(part(data))
    assert collector.read_frame(response, deadline=100, read_timeout=1, clock=lambda: 0) == data
    with pytest.raises(collector.StreamError, match="incomplete_part"):
        collector.read_frame(Response(part(data)[:-5]), deadline=100, read_timeout=1, clock=lambda: 0)
    with pytest.raises(collector.StreamError, match="frame_too_large"):
        collector.read_frame(Response(part(b"x", length=collector.MAX_FRAME_BYTES + 1)),
                             deadline=100, read_timeout=1, clock=lambda: 0)
    with pytest.raises(collector.StreamError, match="invalid_boundary"):
        collector.read_frame(Response(part(data).replace(b"--frame", b"--wrong", 1)),
                             deadline=100, read_timeout=1, clock=lambda: 0)


def test_trickled_header_stops_at_monotonic_deadline():
    with pytest.raises(collector.StreamError, match="duration_elapsed"):
        collector.read_frame(Response(part(jpeg("red"))), deadline=0.5,
                             read_timeout=1, clock=Clock(step=0.1))


def test_collect_saves_unique_unlabeled_images_and_diagnostics(tmp_path):
    first, second = jpeg("red"), jpeg("blue")
    inventory = run_collect(tmp_path, part(first) + part(first) + part(second),
                            duration=20, clock=Clock(step=0.03))
    assert len(inventory["samples"]) == 2
    assert inventory["counts"]["duplicate"] == 1
    assert inventory["image_bytes"] == len(first) + len(second)
    assert inventory["samples"][0]["label_status"] == "unreviewed"
    assert inventory["samples"][0]["capture_time_utc"] is None
    assert "boxes" not in inventory["samples"][0]
    assert inventory["samples"][0]["received_at_utc"] == "2026-10-11T00:00:00+00:00"
    assert (tmp_path / "run/images/frame-0001.jpg").read_bytes() == first
    assert (tmp_path / "run/images/frame-0001.jpg").stat().st_mode & 0o777 == 0o400
    assert json.loads((tmp_path / "run/inventory.json").read_text()) == inventory
    assert (tmp_path / "run/inventory.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "run").stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        run_collect(tmp_path, part(first))


def test_invalid_jpeg_and_incomplete_part_are_counted_without_false_labels(tmp_path):
    inventory = run_collect(tmp_path, part(b"not a jpeg") + part(jpeg("green"))[:-3], duration=3)
    assert inventory["samples"] == []
    assert inventory["counts"]["invalid_jpeg"] >= 1
    assert inventory["counts"]["incomplete_part"] >= 1
    assert "boxes" not in (tmp_path / "run/inventory.json").read_text()


def test_interval_skips_without_decoding(tmp_path, monkeypatch):
    first, second = jpeg("red"), jpeg("blue")
    original = collector.jpeg_diagnostic
    decoded = []

    def record_decode(data):
        decoded.append(data)
        return original(data)

    monkeypatch.setattr(collector, "jpeg_diagnostic", record_decode)
    inventory = run_collect(tmp_path, part(first) + part(second), duration=1,
                            clock=Clock(step=0.001))
    assert inventory["counts"]["interval_skipped"] >= 1
    assert second not in decoded


def test_disk_budget_stops_before_second_save(tmp_path, monkeypatch):
    first, second = jpeg("red"), jpeg("blue")
    monkeypatch.setattr(collector, "INVENTORY_RESERVE_BYTES", 2 * 1024 * 1024 - len(first))
    inventory = run_collect(tmp_path, part(first) + part(second), duration=20,
                            clock=Clock(step=0.03), max_disk_bytes=2 * 1024 * 1024)
    assert len(inventory["samples"]) == 1
    assert inventory["counts"]["stream_error"] == 1
    assert inventory["image_bytes"] <= 2 * 1024 * 1024


def test_local_http_mjpeg_integration(tmp_path):
    frame = jpeg("yellow")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            self.wfile.write(part(frame))
            self.wfile.flush()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/stream/cameras/cam-1"
        result = collector.collect(url, "cam-1", tmp_path / "http-run", duration=3,
                                   sample_interval=1, max_images=1)
        assert len(result["samples"]) == 1
        assert result["counts"]["stream_error"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
