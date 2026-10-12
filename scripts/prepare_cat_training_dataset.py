#!/usr/bin/env python3
"""Validate public Cat ground truth and derive an immutable YOLO split."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
from collections import defaultdict
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
SEED = 42
SPLITS = ("train", "val", "test")
PRETRAINED_CLASS_COUNT = 80


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _private_output(path: Path, source: Path) -> Path:
    target = path.resolve()
    if path.exists() or not target.parent.is_dir():
        raise ValueError("Output must be a new directory with an existing parent")
    if target.is_relative_to(source) or any((parent / ".git").exists() for parent in (target, *target.parents)):
        raise ValueError("Output must be outside source data and Git")
    return target


def _box(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4 or any(type(v) not in (int, float) for v in value):
        raise ValueError("Each Cat box needs four numbers")
    x1, y1, x2, y2 = map(float, value)
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)) or not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError("Cat box must be normalized xyxy")
    return x1, y1, x2, y2


def load_pretrained_names(path: Path, cat_class_id: int) -> tuple[dict[str, str], str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024:
        raise ValueError("Pretrained names must be a regular JSON file of at most 16 KiB")

    def unique_keys(pairs: list[tuple[str, object]]) -> dict:
        names = dict(pairs)
        if len(names) != len(pairs):
            raise ValueError("Duplicate pretrained class index")
        return names

    names = json.loads(path.read_bytes(), object_pairs_hook=unique_keys)
    expected = {str(index) for index in range(PRETRAINED_CLASS_COUNT)}
    if (not isinstance(names, dict) or set(names) != expected
            or any(not isinstance(name, str) or not name.strip() or name != name.strip()
                   for name in names.values())
            or len({name.casefold() for name in names.values()}) != PRETRAINED_CLASS_COUNT):
        raise ValueError("Pretrained names must have 80 distinct indexed class names")
    if type(cat_class_id) is not int or cat_class_id not in range(PRETRAINED_CLASS_COUNT):
        raise ValueError("Cat class ID must be an integer from 0 to 79")
    if names[str(cat_class_id)] != "cat":
        raise ValueError("Cat class ID must match the Cat entry in pretrained names")
    return names, digest(path)


def load_inputs(manifest_path: Path, inventory_path: Path) -> tuple[list[dict], dict]:
    if (manifest_path.is_symlink() or inventory_path.is_symlink()
            or manifest_path.parent.resolve() != inventory_path.parent.resolve()):
        raise ValueError("Manifest and inventory must be regular files in one source directory")
    root = manifest_path.parent.resolve()
    manifest = json.loads(manifest_path.read_bytes())
    inventory = json.loads(inventory_path.read_bytes())
    if not isinstance(manifest, dict) or not isinstance(inventory, dict):
        raise ValueError("Manifest and inventory must be objects")
    if (set(manifest) != {"version", "samples"} or type(manifest["version"]) is not int
            or manifest["version"] != 1
            or not isinstance(manifest["samples"], list) or not manifest["samples"]):
        raise ValueError("Invalid ground-truth manifest")
    if (type(inventory.get("schema_version")) is not int or inventory["schema_version"] != 1
            or not isinstance(inventory.get("images"), list)
            or inventory.get("collected") != len(inventory["images"])):
        raise ValueError("Invalid source inventory")
    indexed = {}
    for image in inventory["images"]:
        if not isinstance(image, dict):
            raise ValueError("Invalid inventory image")
        image_id = image.get("image_id")
        if not isinstance(image_id, str) or not image_id or image_id in indexed:
            raise ValueError("Duplicate or missing inventory image ID")
        indexed[image_id] = image
    rows = []
    seen_paths: set[str] = set()
    seen_hashes: set[str] = set()
    for sample in manifest["samples"]:
        if not isinstance(sample, dict) or set(sample) != {"path", "sha256", "boxes"}:
            raise ValueError("Incomplete Cat ground truth")
        relative = sample["path"]
        if not isinstance(relative, str) or Path(relative).is_absolute() or Path(relative).parts[:1] != ("images",):
            raise ValueError("Unsafe image path")
        image_id = Path(relative).stem
        if (Path(relative).name != f"{image_id}.jpg" or len(Path(relative).parts) != 2
                or relative in seen_paths or image_id not in indexed):
            raise ValueError("Duplicate, missing, or unsafe image path")
        source = root / relative
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root):
            raise ValueError("Image is missing or unsafe")
        meta = indexed[image_id]
        if (not isinstance(sample["sha256"], str) or len(sample["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in sample["sha256"])
                or sample["sha256"] != meta.get("sha256") or sample["sha256"] in seen_hashes
                or digest(source) != sample["sha256"]):
            raise ValueError("Image hash is missing, duplicated, or mismatched")
        boxes = sample["boxes"]
        if not isinstance(boxes, list) or not boxes or boxes != meta.get("boxes"):
            raise ValueError("Missing or mismatched positive Cat boxes")
        if (not isinstance(meta.get("author"), str) or not meta["author"].strip()
                or not isinstance(meta.get("source_url"), str) or not meta["source_url"]
                or not isinstance(meta.get("landing_url"), str) or not meta["landing_url"]
                or not isinstance(meta.get("license"), str) or not meta["license"]
                or not isinstance(meta.get("box_flags"), list)
                or len(meta["box_flags"]) != len(boxes)):
            raise ValueError("Incomplete source provenance")
        parsed_boxes = [_box(box) for box in boxes]
        with Image.open(source) as photo:
            if photo.format != "JPEG" or photo.getexif().get(274, 1) != 1:
                raise ValueError("Image must be JPEG with resolved EXIF orientation")
            photo.verify()
        rows.append({"id": image_id, "source": source, "sha256": sample["sha256"],
                     "author": meta["author"].strip(), "source_url": meta["source_url"],
                     "boxes": parsed_boxes})
        seen_paths.add(relative)
        seen_hashes.add(sample["sha256"])
    if len(rows) != len(indexed):
        raise ValueError("Manifest and inventory image sets differ")
    return sorted(rows, key=lambda row: row["id"]), {
        "manifest_sha256": digest(manifest_path), "inventory_sha256": digest(inventory_path),
        "inventory_status": inventory.get("status"), "source_image_count": len(rows),
    }


def assign_splits(rows: list[dict], seed: int = SEED) -> tuple[dict[str, str], list[dict]]:
    parent = list(range(len(rows)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        parent[root(second)] = root(first)

    seen: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        for kind, value in (("author", row["author"]), ("hash", row["sha256"]),
                            ("url", row["source_url"])):
            key = (kind, value)
            if key in seen:
                union(index, seen[key])
            else:
                seen[key] = index
    grouped: dict[int, list[dict]] = defaultdict(list)
    for index, row in enumerate(rows):
        grouped[root(index)].append(row)
    groups = list(grouped.values())
    if len(groups) < 3:
        raise ValueError("At least three independent source groups are required")
    groups.sort(key=lambda group: hashlib.sha256(
        f"{seed}:".encode() + ",".join(sorted(row["id"] for row in group)).encode()).hexdigest())
    total = len(rows)
    targets = {"val": max(1, round(total * .1)), "test": max(1, round(total * .1))}
    targets["train"] = total - targets["val"] - targets["test"]
    counts = dict.fromkeys(SPLITS, 0)
    assignments: dict[str, str] = {}
    group_records = []
    for index, group in enumerate(groups):
        empty = [name for name in SPLITS if counts[name] == 0]
        if len(groups) - index == len(empty):
            split = empty[0]
        else:
            split = max(SPLITS, key=lambda name: (targets[name] - counts[name], -SPLITS.index(name)))
        ids = sorted(row["id"] for row in group)
        group_id = hashlib.sha256(",".join(ids).encode()).hexdigest()[:16]
        group_records.append({"group_id": group_id, "split": split, "image_ids": ids})
        for image_id in ids:
            assignments[image_id] = split
        counts[split] += len(group)
    if any(counts[name] == 0 for name in SPLITS):
        raise ValueError("Every split needs at least one independent group")
    return assignments, group_records


def prepare(manifest: Path, inventory: Path, output: Path,
            pretrained_names_json: Path | None = None, cat_class_id: int | None = None) -> dict:
    if (pretrained_names_json is None) != (cat_class_id is None):
        raise ValueError("Pretrained names and Cat class ID must be supplied together")
    names = {"0": "cat"}
    names_sha256 = None
    if pretrained_names_json is not None:
        names, names_sha256 = load_pretrained_names(pretrained_names_json, cat_class_id)
    label_class_id = 0 if cat_class_id is None else cat_class_id
    rows, source = load_inputs(manifest, inventory)
    target = _private_output(output, manifest.parent.resolve())
    assignments, groups = assign_splits(rows)
    old_umask = os.umask(0o077)
    try:
        target.mkdir(mode=0o700)
        for kind in ("images", "labels"):
            for split in SPLITS:
                (target / kind / split).mkdir(mode=0o700, parents=True)
        counts = dict.fromkeys(SPLITS, 0)
        box_count = 0
        for row in rows:
            split = assignments[row["id"]]
            image_target = target / "images" / split / f"{row['id']}.jpg"
            with row["source"].open("rb") as src, image_target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            image_target.chmod(0o600)
            if digest(row["source"]) != row["sha256"] or digest(image_target) != row["sha256"]:
                raise ValueError("Source or copied image changed during preparation")
            label = target / "labels" / split / f"{row['id']}.txt"
            with label.open("x", encoding="utf-8") as stream:
                for x1, y1, x2, y2 in row["boxes"]:
                    stream.write(f"{label_class_id} {(x1 + x2) / 2:.17g} {(y1 + y2) / 2:.17g} {x2 - x1:.17g} {y2 - y1:.17g}\n")
            label.chmod(0o600)
            counts[split] += 1
            box_count += len(row["boxes"])
        if source["manifest_sha256"] != digest(manifest) or source["inventory_sha256"] != digest(inventory):
            raise ValueError("Source manifest or inventory changed during preparation")
        if pretrained_names_json is not None and names_sha256 != digest(pretrained_names_json):
            raise ValueError("Pretrained names changed during preparation")
        summary = {"version": 1, "seed": SEED, "source": source, "counts": counts,
                   "cat_boxes": box_count, "groups": groups,
                   "images": [{"image_id": row["id"], "sha256": row["sha256"],
                               "split": assignments[row["id"]]} for row in rows]}
        if pretrained_names_json is not None:
            summary["dataset_mode"] = "pretrained_classes"
            summary["cat_class_id"] = label_class_id
            summary["pretrained_names_sha256"] = names_sha256
        files = {"split-manifest.json": json.dumps(summary, indent=2, sort_keys=True) + "\n",
                 "dataset.yaml": json.dumps({"path": str(target), "train": "images/train",
                                               "val": "images/val", "test": "images/test",
                                               "names": names}, indent=2) + "\n"}
        for name, content in files.items():
            path = target / name
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
            path.chmod(0o600)
        return summary
    finally:
        os.umask(old_umask)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pretrained-names-json", type=Path)
    parser.add_argument("--cat-class-id", type=int)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.manifest, args.inventory, args.output,
                         args.pretrained_names_json, args.cat_class_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Preparation failed: {type(exc).__name__}\n")
    print(json.dumps({"output": str(args.output), "counts": result["counts"],
                      "cat_boxes": result["cat_boxes"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
