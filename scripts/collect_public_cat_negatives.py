"""Collect human-verified Open Images Cat-negative development images."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import time
from collections import Counter
from pathlib import Path
from urllib.error import URLError

from PIL import Image

from scripts.collect_public_cat_dataset import CAT_MID, _eligible, _fetch, _write_new

SOURCES = {
    "validation_labels": (
        "https://storage.googleapis.com/openimages/v7/oidv7-val-annotations-human-imagelabels.csv",
        30_000_000,
    ),
    "test_labels": (
        "https://storage.googleapis.com/openimages/v7/oidv7-test-annotations-human-imagelabels.csv",
        100_000_000,
    ),
    "test_images": (
        "https://storage.googleapis.com/openimages/2018_04/test/test-images-with-rotation.csv",
        50_000_000,
    ),
    "test_boxes": (
        "https://storage.googleapis.com/openimages/v5/test-annotations-bbox.csv",
        80_000_000,
    ),
}
IMAGE_BASE = "https://open-images-dataset.s3.amazonaws.com"
ROLES = {"train": 100, "val": 25, "test": 25}
HUMAN_SOURCES = {"verification", "crowdsource-verification"}
SELECTION_SEED = 42


def _rows(payload: bytes):
    return csv.DictReader(io.StringIO(payload.decode("utf-8")))


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _cat_id(row: dict[str, str]) -> str:
    image_id = row["ImageID"].lower()
    if len(image_id) != 16 or any(char not in "0123456789abcdef" for char in image_id):
        raise ValueError("Invalid Cat ImageID")
    return image_id


def _ids_with_cat_boxes(payload: bytes) -> set[str]:
    return {_cat_id(row) for row in _rows(payload) if row["LabelName"] == CAT_MID}


def _negative_labels(payload: bytes) -> tuple[dict[str, list[str]], set[str]]:
    negative: dict[str, list[str]] = {}
    conflicts: set[str] = set()
    for row in _rows(payload):
        if row["LabelName"] != CAT_MID:
            continue
        image_id = _cat_id(row)
        if row["Source"] in HUMAN_SOURCES and row["Confidence"] in {"0", "0.0"}:
            negative.setdefault(image_id, []).append(row["Source"])
        else:
            conflicts.add(image_id)
    return negative, conflicts


def _metadata(payload: bytes, split: str, ids: set[str]) -> dict[str, dict[str, str]]:
    selected = {}
    for row in _rows(payload):
        image_id = row["ImageID"].lower()
        if image_id in ids and row.get("Subset") == split:
            if image_id in selected:
                raise ValueError(f"Duplicate {split} metadata: {image_id}")
            selected[image_id] = row
    return selected


def _author_keys(row: dict[str, str]) -> set[str]:
    return {
        f"{field}:{value.strip().casefold()}"
        for field in ("Author", "AuthorProfileURL")
        if (value := row.get(field)) and value.strip()
    }


def _selection_order(candidates: dict[tuple[str, str], object]) -> list[tuple[str, str]]:
    return sorted(
        candidates,
        key=lambda pair: hashlib.sha256(
            f"{SELECTION_SEED}:{pair[0]}:{pair[1]}".encode()
        ).digest(),
    )


def _positive_evidence(run: Path) -> tuple[dict, str, dict[str, bytes], set[str], set[str], set[str], set[str]]:
    inventory_bytes = (run / "inventory.json").read_bytes()
    inventory = json.loads(inventory_bytes)
    if inventory.get("schema_version") != 1 or not isinstance(inventory.get("images"), list):
        raise ValueError("Unsupported positive inventory")
    sources = {}
    for name in ("boxes", "images"):
        payload = (run / "source" / f"{name}.csv").read_bytes()
        if _digest(payload) != inventory["source_files"][name]["sha256"]:
            raise ValueError(f"Positive source hash changed: {name}")
        sources[name] = payload
    positive_ids = {entry["image_id"] for entry in inventory["images"]}
    positive_hashes = {entry["sha256"] for entry in inventory["images"]}
    positive_urls = {
        entry[key] for entry in inventory["images"]
        for key in ("source_url", "landing_url") if entry.get(key)
    }
    authors = set()
    for row in _rows(sources["images"]):
        if row["ImageID"].lower() in positive_ids:
            authors.update(_author_keys(row))
            positive_urls.update(row[key] for key in ("OriginalURL", "OriginalLandingURL") if row.get(key))
    return inventory, _digest(inventory_bytes), sources, positive_ids, positive_hashes, positive_urls, authors


def collect(
    positive_run: Path,
    output_dir: Path,
    *,
    max_images: int = 150,
    max_image_bytes: int = 2_000_000,
    max_total_bytes: int = 200_000_000,
    timeout: int = 30,
    retries: int = 0,
    deadline_seconds: int = 1800,
) -> dict[str, object]:
    if not 1 <= max_images <= 150 or not 1 <= max_image_bytes <= 2_000_000:
        raise ValueError("Invalid image count or per-image byte limit")
    if not max_image_bytes <= max_total_bytes <= 200_000_000 or timeout < 1 or not 0 <= retries <= 2:
        raise ValueError("Invalid byte, timeout, or retry limit")
    target = output_dir.resolve()
    if (deadline_seconds < 1 or target.is_relative_to(positive_run.resolve())
            or any((parent / ".git").exists() for parent in (target, *target.parents))):
        raise ValueError("Invalid deadline or output inside positive run or Git")
    started = time.monotonic()

    def bounded_fetch(url: str, limit: int) -> bytes:
        remaining = deadline_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Collection deadline exceeded")
        return _fetch(url, limit, min(timeout, remaining))

    positive_inventory, initial_inventory_hash, positive_sources, positive_ids, positive_hashes, positive_urls, positive_authors = (
        _positive_evidence(positive_run)
    )
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output_dir / "images").mkdir(mode=0o700)
    (output_dir / "source").mkdir(mode=0o700)
    source_data = {}
    source_hashes = {}
    for name, (url, limit) in SOURCES.items():
        payload = bounded_fetch(url, limit)
        _write_new(output_dir / "source" / f"{name}.csv", payload)
        source_data[name] = payload
        source_hashes[name] = {"url": url, "sha256": _digest(payload)}
    for name in ("boxes", "images"):
        source_hashes[f"positive_{name}"] = positive_inventory["source_files"][name]
    labels = {}
    candidates = {}
    for split in ("validation", "test"):
        negative, conflicts = _negative_labels(source_data[f"{split}_labels"])
        boxes = _ids_with_cat_boxes(
            positive_sources["boxes"] if split == "validation" else source_data["test_boxes"]
        )
        for image_id in sorted(negative.keys() - conflicts - boxes - positive_ids):
            candidates[(split, image_id)] = negative[image_id]
        labels[split] = _metadata(
            positive_sources["images"] if split == "validation" else source_data["test_images"],
            split,
            set(negative),
        )
    samples = []
    images = []
    used_bytes = 0
    used_authors = set()
    used_urls = set()
    used_hashes = set()
    counts: Counter = Counter()
    skipped: Counter = Counter()
    for split, image_id in _selection_order(candidates):
        if len(samples) >= max_images:
            break
        if time.monotonic() - started >= deadline_seconds:
            skipped["deadline"] += 1
            break
        info = labels[split].get(image_id)
        reason = _eligible(info)
        if reason:
            skipped[reason] += 1
            continue
        author_keys = _author_keys(info)
        if not info.get("OriginalURL"):
            skipped["attribution_missing"] += 1
            continue
        urls = {info["OriginalURL"], info["OriginalLandingURL"]}
        if not author_keys or author_keys & (positive_authors | used_authors):
            skipped["author_overlap"] += 1
            continue
        if urls & (positive_urls | used_urls):
            skipped["url_overlap"] += 1
            continue
        remaining = max_total_bytes - used_bytes
        if remaining < 1:
            skipped["total_byte_limit"] += 1
            break
        url = f"{IMAGE_BASE}/{split}/{image_id}.jpg"
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
                    skipped["image_download_or_decode_failed"] += 1
        if payload is None:
            continue
        digest = _digest(payload)
        if digest in positive_hashes | used_hashes:
            skipped["image_hash_overlap"] += 1
            continue
        role = next((name for name, quota in ROLES.items() if counts[name] < quota), None)
        if role is None:
            break
        relative = f"images/{image_id}.jpg"
        _write_new(output_dir / relative, payload)
        samples.append({"path": relative, "sha256": digest, "boxes": []})
        images.append({
            "image_id": image_id, "source_split": split, "role": role,
            "source_url": url, "sha256": digest, "bytes": len(payload),
            "cat_label": {"LabelName": CAT_MID, "Confidence": 0, "Source": candidates[(split, image_id)]},
            "license": info["License"], "license_evidence": "Open Images metadata only",
            "author": info["Author"], "author_profile_url": info.get("AuthorProfileURL", ""),
            "title": info.get("Title", ""), "landing_url": info["OriginalLandingURL"],
            "original_url": info["OriginalURL"],
        })
        counts[role] += 1
        used_bytes += len(payload)
        used_authors.update(author_keys)
        used_urls.update(urls)
        used_hashes.add(digest)
    for name in ("boxes", "images"):
        if _digest((positive_run / "source" / f"{name}.csv").read_bytes()) != source_hashes[f"positive_{name}"]["sha256"]:
            raise ValueError(f"Positive source changed during collection: {name}")
    positive_inventory_hash = _digest((positive_run / "inventory.json").read_bytes())
    if positive_inventory_hash != initial_inventory_hash:
        raise ValueError("Positive inventory changed during collection")
    result = {
        "schema_version": 1, "dataset": "Open Images V7 human-verified Cat-negative public-development",
        "status": "complete" if len(samples) == max_images else "partial",
        "requested": max_images, "collected": len(samples), "role_counts": dict(counts),
        "selection_seed": SELECTION_SEED,
        "image_bytes": used_bytes, "source_files": source_hashes,
        "positive_inventory_sha256": positive_inventory_hash,
        "skipped": dict(sorted(skipped.items())), "images": images,
        "note": "Public development negatives, not household-camera held-out data; rights require original verification.",
    }
    if samples:
        _write_new(output_dir / "manifest.json", (json.dumps({"version": 1, "samples": samples}, indent=2) + "\n").encode())
    _write_new(output_dir / "inventory.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positive-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-images", type=int, default=150)
    parser.add_argument("--max-image-bytes", type=int, default=2_000_000)
    parser.add_argument("--max-total-bytes", type=int, default=200_000_000)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    args = parser.parse_args()
    result = collect(**vars(args))
    print(f"{result['status']}: {result['collected']}/{result['requested']} images")


if __name__ == "__main__":
    main()
