#!/usr/bin/env python3
"""Collect bounded, unlabeled JPEGs from a camera's live MJPEG stream."""
from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

from script.validation import is_valid_camera_id

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_HEADER_BYTES = 1024
INVENTORY_RESERVE_BYTES = 1024 * 1024
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
LOCAL_V4 = tuple(ipaddress.ip_network(block) for block in
                 ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class StreamError(Exception):
    """A bounded MJPEG part was malformed or incomplete."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(request.full_url, code, "redirect_rejected", headers, fp)


def validate_url(url: str, camera_id: str) -> None:
    if not is_valid_camera_id(camera_id):
        raise ValueError("Invalid camera ID")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("Expected HTTP(S) URL without credentials")
    if parsed.query or parsed.fragment or parsed.path != f"/stream/cameras/{camera_id}":
        raise ValueError("Expected exact camera stream path without query or fragment")
    try:
        host = parsed.hostname
        address = ipaddress.ip_address(host) if host != "localhost" else ipaddress.ip_address("127.0.0.1")
        _ = parsed.port
    except (ValueError, TypeError) as exc:
        raise ValueError("Expected literal local/Tailscale IP or localhost") from exc
    if not (address.is_loopback or address in TAILSCALE_V4 or address in TAILSCALE_V6
            or any(address in network for network in LOCAL_V4)):
        raise ValueError("Stream host must be local or Tailscale")


def _set_socket_timeout(response, clock, deadline: float, read_timeout: float) -> None:
    # urllib HTTPResponse exposes the underlying socket here. Fake streams used
    # by tests omit it; every read also checks the monotonic deadline.
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(max(0.01, min(read_timeout, deadline - clock())))


def _read1(response, size: int, clock, deadline: float, read_timeout: float) -> bytes:
    if clock() >= deadline:
        raise StreamError("duration_elapsed")
    _set_socket_timeout(response, clock, deadline, read_timeout)
    return response.read1(size)


def _line(response, clock, deadline: float, read_timeout: float) -> bytes:
    line = bytearray()
    while len(line) <= MAX_HEADER_BYTES:
        part = _read1(response, 1, clock, deadline, read_timeout)
        if not part:
            raise StreamError("incomplete_part")
        line.extend(part)
        if line.endswith(b"\r\n"):
            return bytes(line)
    raise StreamError("header_too_large")


def _exact(response, size: int, clock, deadline: float, read_timeout: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = _read1(response, min(65536, size - len(data)), clock, deadline, read_timeout)
        if not chunk:
            raise StreamError("incomplete_part")
        data.extend(chunk)
    return bytes(data)


def read_frame(response, *, clock=time.monotonic, deadline: float, read_timeout: float) -> bytes:
    boundary = _line(response, clock, deadline, read_timeout)
    if boundary != b"--frame\r\n":
        raise StreamError("invalid_boundary")
    headers: dict[bytes, bytes] = {}
    for _ in range(8):
        line = _line(response, clock, deadline, read_timeout)
        if line == b"\r\n":
            break
        if b":" not in line:
            raise StreamError("invalid_header")
        key, value = line[:-2].split(b":", 1)
        key = key.strip().lower()
        if key in headers:
            raise StreamError("duplicate_header")
        headers[key] = value.strip().lower()
    else:
        raise StreamError("too_many_headers")
    if headers.get(b"content-type") != b"image/jpeg":
        raise StreamError("invalid_content_type")
    length = headers.get(b"content-length", b"")
    if not length.isdigit() or len(length) > 9:
        raise StreamError("invalid_content_length")
    size = int(length)
    if size < 1 or size > MAX_FRAME_BYTES:
        raise StreamError("frame_too_large")
    frame = _exact(response, size, clock, deadline, read_timeout)
    if _exact(response, 2, clock, deadline, read_timeout) != b"\r\n":
        raise StreamError("invalid_frame_terminator")
    return frame


def jpeg_diagnostic(data: bytes) -> dict[str, object]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or image.width * image.height > 8_000_000:
                raise ValueError("invalid_jpeg")
            width, height = image.size
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            grayscale = ImageOps.exif_transpose(image).convert("L").resize((32, 32))
            values = grayscale.tobytes()
    return {"width": width, "height": height,
            "mean_luma": round(sum(values) / len(values), 1),
            "dark_pixel_fraction": round(sum(value < 40 for value in values) / len(values), 3)}


def _save_frame(path: Path, frame: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(frame)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o400)


def collect(url: str, camera_id: str, output: Path, *, duration: float = 600,
            sample_interval: float = 60, max_images: int = 10, max_disk_bytes: int = 50 * 1024 * 1024,
            read_timeout: float = 10, lighting: str = "unknown", clock=time.monotonic,
            utc_now=lambda: datetime.now(timezone.utc), opener=None, sleep=time.sleep) -> dict:
    validate_url(url, camera_id)
    if not (1 <= duration <= 3600 and 1 <= sample_interval <= 3600 and 1 <= max_images <= 1000
            and 2 * 1024 * 1024 <= max_disk_bytes <= 1024 * 1024 * 1024
            and 0.1 <= read_timeout <= 30 and lighting in {"day", "night", "unknown"}):
        raise ValueError("Collection limits are out of range")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    output.chmod(0o700)
    (output / "images").mkdir(mode=0o700)
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    start = clock()
    deadline = start + duration
    next_sample = start
    samples: list[dict] = []
    hashes: set[str] = set()
    counts = {"invalid_jpeg": 0, "duplicate": 0, "interval_skipped": 0,
              "stream_error": 0, "incomplete_part": 0, "frame_too_large": 0}
    image_bytes = 0
    attempts = 0
    while clock() < deadline and len(samples) < max_images:
        attempts += 1
        try:
            request = urllib.request.Request(url, headers={"Accept": "multipart/x-mixed-replace"})
            with opener.open(request, timeout=min(read_timeout, max(0.1, deadline - clock()))) as response:
                content_type = response.headers.get("Content-Type", "").lower().replace(" ", "")
                if not content_type.startswith("multipart/x-mixed-replace;") or "boundary=frame" not in content_type:
                    raise StreamError("invalid_stream_type")
                while clock() < deadline and len(samples) < max_images:
                    frame = read_frame(response, clock=clock, deadline=deadline, read_timeout=read_timeout)
                    received = utc_now().isoformat()
                    if clock() < next_sample:
                        counts["interval_skipped"] += 1
                        continue
                    try:
                        diagnostic = jpeg_diagnostic(frame)
                    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
                            Image.DecompressionBombWarning):
                        counts["invalid_jpeg"] += 1
                        continue
                    digest = hashlib.sha256(frame).hexdigest()
                    if digest in hashes:
                        counts["duplicate"] += 1
                        continue
                    if image_bytes + len(frame) > max_disk_bytes - INVENTORY_RESERVE_BYTES:
                        raise StreamError("disk_budget_reached")
                    name = f"frame-{len(samples) + 1:04d}.jpg"
                    _save_frame(output / "images" / name, frame)
                    samples.append({"path": f"images/{name}", "sha256": digest,
                                    "received_at_utc": received, "capture_time_utc": None,
                                    "camera_id": camera_id, "label_status": "unreviewed",
                                    "lighting_declared": lighting, "diagnostic_only": diagnostic})
                    hashes.add(digest)
                    image_bytes += len(frame)
                    next_sample = clock() + sample_interval
        except StreamError as exc:
            if exc.reason == "duration_elapsed":
                break
            counts[exc.reason if exc.reason in counts else "stream_error"] += 1
            if exc.reason == "disk_budget_reached":
                break
        except (urllib.error.URLError, OSError, TimeoutError, socket.timeout):
            counts["stream_error"] += 1
        if clock() < deadline and len(samples) < max_images:
            sleep(min(1.0, max(0.0, deadline - clock())))
    inventory = {"version": 1, "kind": "unlabeled_camera_inventory", "camera_id": camera_id,
                 "capture_time_status": "unknown; received_at_utc is collector time",
                 "lighting_declared": lighting, "limits": {"duration_seconds": duration,
                 "sample_interval_seconds": sample_interval, "max_images": max_images,
                 "max_disk_bytes": max_disk_bytes, "max_frame_bytes": MAX_FRAME_BYTES,
                 "read_timeout_seconds": read_timeout}, "connection_attempts": attempts,
                 "image_bytes": image_bytes, "counts": counts, "samples": samples}
    with (output / "inventory.json").open("x", encoding="utf-8") as stream:
        json.dump(inventory, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    (output / "inventory.json").chmod(0o600)
    return inventory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Exact local/Tailscale stream URL; never saved")
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--output", type=Path, required=True, help="New private directory")
    parser.add_argument("--duration-seconds", type=float, default=600)
    parser.add_argument("--sample-interval-seconds", type=float, default=60)
    parser.add_argument("--max-images", type=int, default=10)
    parser.add_argument("--max-disk-mib", type=int, default=50)
    parser.add_argument("--read-timeout-seconds", type=float, default=10)
    parser.add_argument("--lighting", choices=("day", "night", "unknown"), default="unknown")
    args = parser.parse_args(argv)
    try:
        result = collect(args.url, args.camera_id, args.output,
                         duration=args.duration_seconds, sample_interval=args.sample_interval_seconds,
                         max_images=args.max_images, max_disk_bytes=args.max_disk_mib * 1024 * 1024,
                         read_timeout=args.read_timeout_seconds, lighting=args.lighting)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Collection failed: {type(exc).__name__}\n")
    print(json.dumps({"output": str(args.output), "saved": len(result["samples"]),
                      "counts": result["counts"]}, sort_keys=True))
    return 0 if result["samples"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
