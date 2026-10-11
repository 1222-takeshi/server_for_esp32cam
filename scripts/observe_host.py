#!/usr/bin/env python3
"""Append one bounded, credential-free host observation to a JSONL file."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import urlopen


def fetch_json(url: str) -> dict[str, object]:
    try:
        with urlopen(url, timeout=2) as response:  # noqa: S310 - loopback URL validated by CLI
            payload = response.read(4097)
            if len(payload) > 4096:
                return {"error": "response_too_large"}
        data = json.loads(payload)
        return data if isinstance(data, dict) else {"error": "invalid_response"}
    except HTTPError as exc:
        if exc.code == 503:
            try:
                payload = exc.read(4097)
                data = json.loads(payload) if len(payload) <= 4096 else None
                if isinstance(data, dict) and data.get("status") == "degraded":
                    return data
            except (ValueError, UnicodeDecodeError):
                pass
        return {"error": "http_status", "http_status": exc.code}
    except (URLError, TimeoutError, OSError):
        return {"error": "unreachable"}
    except (ValueError, UnicodeDecodeError):
        return {"error": "invalid_response"}


def memory_available_mb() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def observe(base_url: str, camera_ids: list[str], storage_path: Path) -> dict[str, object]:
    ready = fetch_json(f"{base_url}/healthz/ready")
    readiness = {
        "status": ready.get("status") if ready.get("status") in {"ready", "degraded"} else None,
        "error": ready.get("error"),
        "http_status": ready.get("http_status"),
    }
    for field in ("db", "storage", "disk_free_mb", "inference_worker"):
        if field in ready and type(ready[field]) in (str, int):
            readiness[field] = ready[field]

    cameras = {}
    for camera_id in camera_ids:
        result = fetch_json(f"{base_url}/api/cameras/{quote(camera_id, safe='')}/connection")
        cameras[camera_id] = {
            "status": result.get("status") if result.get("status") in {"healthy", "stale", "offline"} else None,
            "seconds_since_contact": result.get("seconds_since_contact")
            if type(result.get("seconds_since_contact")) is int else None,
            "error": result.get("error"),
            "http_status": result.get("http_status"),
        }

    try:
        disk = shutil.disk_usage(storage_path)
        disk_free_mb = disk.free // (1024 * 1024)
        disk_free_percent = round(100 * disk.free / disk.total, 2)
    except (OSError, ZeroDivisionError):
        disk_free_mb = disk_free_percent = None
    try:
        load_1m = round(os.getloadavg()[0], 3)
    except OSError:
        load_1m = None
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "readiness": readiness,
        "cameras": cameras,
        "host": {
            "disk_free_mb": disk_free_mb,
            "disk_free_percent": disk_free_percent,
            "load_1m": load_1m,
            "cpu_count": os.cpu_count(),
            "memory_available_mb": memory_available_mb(),
        },
    }


def append_bounded(path: Path, sample: dict[str, object], max_bytes: int) -> None:
    import fcntl

    line = (json.dumps(sample, separators=(",", ":"), sort_keys=True) + "\n").encode()
    if len(line) > max_bytes:
        raise ValueError("sample exceeds file limit")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and path.stat().st_size + len(line) > max_bytes:
            path.replace(path.with_suffix(path.suffix + ".1"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--camera-id", action="append", default=[])
    parser.add_argument("--storage-path", type=Path, default=Path("uploads"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024)
    args = parser.parse_args(argv)
    try:
        parsed = urlsplit(args.base_url)
        port = parsed.port
    except ValueError:
        parser.error("--base-url must be a valid local HTTP origin")
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not port or parsed.username or parsed.password or parsed.path or parsed.query
            or parsed.fragment):
        parser.error("--base-url must be a local HTTP origin with an explicit port")
    if not args.camera_id or len(args.camera_id) > 32 or any(
        not c or len(c) > 64 or "/" in c for c in args.camera_id
    ):
        parser.error("provide 1-32 camera IDs, each 1-64 characters without slashes")
    if args.max_bytes < 4096:
        parser.error("--max-bytes must be at least 4096")
    try:
        append_bounded(args.output, observe(args.base_url, args.camera_id, args.storage_path), args.max_bytes)
    except (OSError, ValueError):
        print("observation could not be saved", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
