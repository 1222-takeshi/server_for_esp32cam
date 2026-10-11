"""Collect a bounded Open Images V7 validation Cat development set."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import signal
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from PIL import Image

SOURCES = {
    "classes": ("https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions-boxable.csv", 100_000),
    "boxes": ("https://storage.googleapis.com/openimages/v5/validation-annotations-bbox.csv", 30_000_000),
    "images": ("https://storage.googleapis.com/openimages/2018_04/validation/validation-images-with-rotation.csv", 20_000_000),
}
IMAGE_BASE = "https://open-images-dataset.s3.amazonaws.com/validation"
CAT_MID = "/m/01yrx"
ALLOWED_LICENSES = {
    "https://creativecommons.org/licenses/by/2.0/",
    "http://creativecommons.org/licenses/by/2.0/",
    "https://creativecommons.org/licenses/by/4.0/",
    "http://creativecommons.org/licenses/by/4.0/",
}


def _fetch(url: str, limit: int, timeout: float) -> bytes:
    def expired(_signum: int, _frame: object) -> None:
        raise TimeoutError(f"HTTP wall-time limit exceeded: {url}")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        with urlopen(url, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise ValueError(f"Response exceeds byte limit: {url}")
            chunks = []
            total = 0
            while chunk := response.read(min(65536, limit + 1 - total)):
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"Response exceeds byte limit: {url}")
                chunks.append(chunk)
            return b"".join(chunks)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _write_new(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)


def _csv_rows(payload: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))


def _selected_boxes(
    rows: list[dict[str, str]],
) -> tuple[dict[str, list[list[float]]], dict[str, list[dict[str, str]]], Counter]:
    candidates: dict[str, list[list[float]]] = defaultdict(list)
    flags: dict[str, list[dict[str, str]]] = defaultdict(list)
    rejected: set[str] = set()
    reasons: Counter = Counter()
    for row in rows:
        if row.get("LabelName") != CAT_MID:
            continue
        image_id = row["ImageID"].lower()
        if len(image_id) != 16 or any(ch not in "0123456789abcdef" for ch in image_id):
            raise ValueError("Invalid Open Images ImageID")
        if any(row.get(flag) != "0" for flag in ("IsGroupOf", "IsDepiction", "IsInside")):
            rejected.add(image_id)
            reasons["special_cat_box"] += 1
            continue
        try:
            x1, x2, y1, y2 = (float(row[key]) for key in ("XMin", "XMax", "YMin", "YMax"))
        except (KeyError, ValueError) as exc:
            raise ValueError("Invalid Cat box coordinates") from exc
        if not all(map(math.isfinite, (x1, x2, y1, y2))) or not (
            0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1
        ):
            raise ValueError("Invalid normalized Cat box")
        candidates[image_id].append([x1, y1, x2, y2])
        flags[image_id].append({
            "IsOccluded": row.get("IsOccluded", ""),
            "IsTruncated": row.get("IsTruncated", ""),
        })
    for image_id in rejected:
        candidates.pop(image_id, None)
        flags.pop(image_id, None)
    reasons["excluded_images_with_special_box"] = len(rejected)
    return dict(candidates), dict(flags), reasons


def _image_metadata(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row["ImageID"].lower(): row for row in rows if row.get("Subset") == "validation"}


def _eligible(metadata: dict[str, str] | None) -> str | None:
    if metadata is None:
        return "missing_metadata"
    if metadata.get("License") not in ALLOWED_LICENSES:
        return "license_unconfirmed"
    if not metadata.get("Author") or not metadata.get("OriginalLandingURL"):
        return "attribution_missing"
    rotation = metadata.get("Rotation", "")
    try:
        is_unrotated = bool(rotation) and math.isfinite(float(rotation)) and float(rotation) == 0
    except ValueError:
        is_unrotated = False
    if not is_unrotated:
        return "rotation_unresolved"
    return None


def collect(
    output_dir: Path,
    *,
    max_images: int = 300,
    max_image_bytes: int = 2_000_000,
    max_total_bytes: int = 200_000_000,
    timeout: int = 20,
    retries: int = 0,
    deadline_seconds: int = 1800,
) -> dict[str, object]:
    if not (1 <= max_images <= 300 and 1 <= max_image_bytes <= 2_000_000):
        raise ValueError("Invalid image count or per-image byte limit")
    if not max_image_bytes <= max_total_bytes <= 200_000_000 or timeout < 1 or not 0 <= retries <= 2:
        raise ValueError("Invalid total byte limit, timeout, or retry count")
    if deadline_seconds < 1:
        raise ValueError("Deadline must be positive")
    started = time.monotonic()
    def bounded_fetch(url: str, limit: int) -> bytes:
        remaining = deadline_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Collection deadline exceeded")
        return _fetch(url, limit, min(timeout, remaining))

    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    images_dir = output_dir / "images"
    images_dir.mkdir(mode=0o700)
    source_dir = output_dir / "source"
    source_dir.mkdir(mode=0o700)
    source_rows = {}
    source_hashes = {}
    for name, (url, limit) in SOURCES.items():
        payload = bounded_fetch(url, limit)
        _write_new(source_dir / f"{name}.csv", payload)
        source_rows[name] = _csv_rows(payload)
        source_hashes[name] = {"url": url, "sha256": hashlib.sha256(payload).hexdigest()}
    class_rows = list(csv.reader(io.StringIO((source_dir / "classes.csv").read_text())))
    if [row for row in class_rows if len(row) == 2 and row[1] == "Cat"] != [[CAT_MID, "Cat"]]:
        raise ValueError("Official Cat class mapping changed")
    boxes, box_flags, reasons = _selected_boxes(source_rows["boxes"])
    metadata = _image_metadata(source_rows["images"])
    samples = []
    inventory = []
    used_bytes = 0
    for image_id in sorted(boxes):
        if len(samples) >= max_images:
            break
        if time.monotonic() - started >= deadline_seconds:
            reasons["deadline"] += 1
            break
        info = metadata.get(image_id)
        reason = _eligible(info)
        if reason:
            reasons[reason] += 1
            continue
        remaining = max_total_bytes - used_bytes
        if remaining < 1:
            reasons["total_byte_limit"] += 1
            break
        url = f"{IMAGE_BASE}/{image_id}.jpg"
        payload = None
        for attempt in range(retries + 1):
            try:
                payload = bounded_fetch(url, min(max_image_bytes, remaining))
                with Image.open(io.BytesIO(payload)) as image:
                    if image.format != "JPEG":
                        raise ValueError("Official image is not JPEG")
                    image.verify()
                break
            except (OSError, URLError, ValueError):
                payload = None
                if attempt == retries:
                    reasons["image_download_or_decode_failed"] += 1
        if payload is None:
            continue
        relative = f"images/{image_id}.jpg"
        _write_new(output_dir / relative, payload)
        digest = hashlib.sha256(payload).hexdigest()
        used_bytes += len(payload)
        samples.append({"path": relative, "sha256": digest, "boxes": boxes[image_id]})
        inventory.append({
            "image_id": image_id, "source_url": url, "sha256": digest,
            "bytes": len(payload), "boxes": boxes[image_id],
            "box_flags": box_flags[image_id],
            "license": info["License"], "license_evidence": "Open Images metadata only",
            "author": info["Author"], "title": info.get("Title", ""),
            "landing_url": info["OriginalLandingURL"],
        })
    status = "complete" if len(samples) == max_images else "partial"
    result = {
        "schema_version": 1, "dataset": "Open Images V7 validation Cat public-development",
        "status": status, "requested": max_images, "collected": len(samples),
        "image_bytes": used_bytes, "source_files": source_hashes,
        "skipped": dict(sorted(reasons.items())), "images": inventory,
        "note": "Public development data; not household-camera held-out ground truth. Image licenses require independent verification before reuse or redistribution.",
    }
    if samples:
        _write_new(output_dir / "manifest.json", (json.dumps({"version": 1, "samples": samples}, indent=2) + "\n").encode())
    _write_new(output_dir / "inventory.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-images", type=int, default=300)
    parser.add_argument("--max-image-bytes", type=int, default=2_000_000)
    parser.add_argument("--max-total-bytes", type=int, default=200_000_000)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    args = parser.parse_args()
    result = collect(**vars(args))
    print(f"{result['status']}: {result['collected']}/{result['requested']} images")


if __name__ == "__main__":
    main()
