"""Apply a complete, reviewed Cat annotation ledger to a private YOLO dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
from collections import Counter
from pathlib import Path

SPLITS = ("train", "val", "test")
KINDS = ("positive", "negative")
COHORTS = ("existing", "new")
CAT_CLASS_ID = 15
ENTRY_KEYS = {"image_id", "image_sha256", "label_sha256", "split", "kind", "cohort", "status"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _json_file(path: Path, limit: int) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Input must be a bounded regular JSON file")
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate JSON key")
        return result

    value = json.loads(path.read_bytes(), object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise ValueError("Input JSON must be an object")
    return value


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value)


def _private_target(output: Path, source: Path) -> Path:
    target = output.resolve()
    if (output.exists() or output.is_symlink() or not target.parent.is_dir() or target.is_relative_to(source)
            or any((parent / ".git").exists() for parent in (target, *target.parents))):
        raise ValueError("Output must be a new private directory outside source and Git")
    return target


def _source_file(root: Path, relative: str, sha256: str) -> Path:
    path = root / relative
    if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root)
            or digest(path) != sha256):
        raise ValueError(f"Source file missing, unsafe, or SHA-mismatched: {relative}")
    return path


def _box(box: object) -> tuple[float, float, float, float]:
    if not isinstance(box, list) or len(box) != 4 or any(type(v) not in (int, float) for v in box):
        raise ValueError("Cat box needs four numeric xyxy values")
    x1, y1, x2, y2 = map(float, box)
    if not all(map(math.isfinite, (x1, y1, x2, y2))) or not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError("Cat box must be a finite normalized rectangle")
    return x1, y1, x2, y2


def _yolo(boxes: list) -> bytes:
    lines = []
    for x1, y1, x2, y2 in map(_box, boxes):
        lines.append(f"{CAT_CLASS_ID} {(x1 + x2) / 2:.17g} {(y1 + y2) / 2:.17g} "
                     f"{x2 - x1:.17g} {y2 - y1:.17g}\n")
    return "".join(lines).encode()


def _validate_source(source: Path) -> tuple[dict, dict, dict[str, dict]]:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Source must be an existing dataset directory")
    manifest = _json_file(source / "split-manifest.json", 8_000_000)
    config = _json_file(source / "dataset.yaml", 32_000)
    if (manifest.get("version") != 1 or manifest.get("cat_class_id") != CAT_CLASS_ID
            or not isinstance(manifest.get("images"), list) or not manifest["images"]
            or not isinstance(manifest.get("label_hashes"), dict)
            or not isinstance(manifest.get("groups"), list)
            or config.get("path") != str(source.resolve())
            or any(config.get(name) != f"images/{name}" for name in SPLITS)
            or not isinstance(config.get("names"), dict)
            or set(config["names"]) != {str(index) for index in range(80)}
            or config["names"].get(str(CAT_CLASS_ID)) != "cat"):
        raise ValueError("Unsupported source dataset schema or class mapping")
    rows = {}
    counts = Counter()
    labels = set()
    for row in manifest["images"]:
        if (not isinstance(row, dict) or not _hex(row.get("image_id"), 16)
                or not _hex(row.get("sha256"), 64) or row.get("split") not in SPLITS
                or row.get("kind") not in KINDS or row.get("cohort") not in COHORTS
                or row["image_id"] in rows):
            raise ValueError("Invalid or duplicate source image record")
        image_id, split = row["image_id"], row["split"]
        image_relative = f"images/{split}/{image_id}.jpg"
        label_relative = f"labels/{split}/{image_id}.txt"
        label_hash = manifest["label_hashes"].get(label_relative)
        if not _hex(label_hash, 64):
            raise ValueError("Missing source label SHA")
        _source_file(source, image_relative, row["sha256"])
        label = _source_file(source, label_relative, label_hash)
        content = label.read_text(encoding="utf-8")
        if row["kind"] == "negative" and content:
            raise ValueError("Negative source label is not empty")
        if row["kind"] == "positive" and not content:
            raise ValueError("Positive source label is empty")
        for line in content.splitlines():
            fields = line.split()
            if len(fields) != 5 or fields[0] != str(CAT_CLASS_ID):
                raise ValueError("Unexpected source label class or shape")
            try:
                center_x, center_y, width, height = map(float, fields[1:])
            except ValueError as exc:
                raise ValueError("Invalid source YOLO label") from exc
            _box([center_x - width / 2, center_y - height / 2,
                  center_x + width / 2, center_y + height / 2])
        rows[image_id] = row
        labels.add(label_relative)
        counts[split] += 1
    if set(manifest["label_hashes"]) != labels or dict(counts) != manifest.get("counts"):
        raise ValueError("Source label set or split counts mismatch")
    grouped = set()
    group_ids = set()
    for group in manifest["groups"]:
        if (not isinstance(group, dict) or not isinstance(group.get("group_id"), str)
                or group["group_id"] in group_ids or group.get("split") not in SPLITS
                or not isinstance(group.get("image_ids"), list) or not group["image_ids"]):
            raise ValueError("Invalid source group")
        group_ids.add(group["group_id"])
        for image_id in group["image_ids"]:
            if (image_id in grouped or image_id not in rows
                    or rows[image_id]["kind"] != "positive" or rows[image_id]["split"] != group["split"]):
                raise ValueError("Source groups disagree with positive images")
            grouped.add(image_id)
    if grouped != {image_id for image_id, row in rows.items() if row["kind"] == "positive"}:
        raise ValueError("Source groups do not cover positive images")
    return manifest, config, rows


def _validate_ledger(ledger: dict, source_manifest_sha: str,
                     source_config_sha: str, rows: dict[str, dict], label_hashes: dict) -> dict[str, dict]:
    required = {"version", "source_split_manifest_sha256", "source_dataset_yaml_sha256",
                "review_id", "entries"}
    if (set(ledger) not in (required, required | {"auditors"}) or ledger["version"] != 1
            or ledger["source_split_manifest_sha256"] != source_manifest_sha
            or ledger["source_dataset_yaml_sha256"] != source_config_sha
            or not isinstance(ledger["review_id"], str) or not ledger["review_id"].strip()
            or not isinstance(ledger["entries"], list) or len(ledger["entries"]) != len(rows)):
        raise ValueError("Incomplete or stale review ledger")
    if "auditors" in ledger and (not isinstance(ledger["auditors"], list)
                               or any(not isinstance(name, str) or not name.strip()
                                      for name in ledger["auditors"])):
        raise ValueError("Invalid auditor list")
    indexed = {}
    for entry in ledger["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("image_id"), str):
            raise ValueError("Invalid ledger entry")
        image_id = entry["image_id"]
        if image_id in indexed or image_id not in rows:
            raise ValueError("Duplicate or unknown ledger image ID")
        row = rows[image_id]
        status = entry.get("status")
        expected = ENTRY_KEYS | {"reviewers"} | ({"reason"} if status == "quarantine" else
                                                 {"reason", "boxes"} if status == "replace_boxes" else set())
        label_relative = f"labels/{row['split']}/{image_id}.txt"
        if (status not in ("keep", "quarantine", "replace_boxes") or set(entry) != expected
                or any(entry.get(key) != row[key] for key in ("split", "kind", "cohort"))
                or entry.get("image_sha256") != row["sha256"]
                or entry.get("label_sha256") != label_hashes[label_relative]):
            raise ValueError("Ledger entry does not match source evidence")
        reviewers = entry["reviewers"]
        if (not isinstance(reviewers, list) or len(reviewers) < (1 if status == "keep" else 2)
                or any(not isinstance(name, str) or not name.strip() for name in reviewers)
                or len({name.strip().casefold() for name in reviewers}) != len(reviewers)):
            raise ValueError("Changed annotation needs two distinct reviewers; keep needs one")
        if status != "keep" and (not isinstance(entry["reason"], str) or not entry["reason"].strip()):
            raise ValueError("Changed annotation requires a reason")
        if status == "replace_boxes":
            if not isinstance(entry["boxes"], list):
                raise ValueError("Replacement boxes must be a list")
            _yolo(entry["boxes"])
        indexed[image_id] = entry
    if set(indexed) != set(rows):
        raise ValueError("Ledger must review every source image")
    return indexed


def apply(source: Path, ledger_path: Path, output: Path) -> dict:
    if source.is_symlink():
        raise ValueError("Source dataset cannot be a symlink")
    source = source.resolve()
    target = _private_target(output, source)
    if ledger_path.resolve().is_relative_to(target):
        raise ValueError("Ledger cannot be inside output")
    source_manifest_path, source_config_path = source / "split-manifest.json", source / "dataset.yaml"
    manifest, config, rows = _validate_source(source)
    ledger = _json_file(ledger_path, 8_000_000)
    manifest_sha, config_sha, ledger_sha = map(digest, (source_manifest_path, source_config_path, ledger_path))
    entries = _validate_ledger(ledger, manifest_sha, config_sha, rows, manifest["label_hashes"])
    old_umask = os.umask(0o077)
    try:
        target.mkdir(mode=0o700)
        for kind in ("images", "labels"):
            for split in SPLITS:
                (target / kind / split).mkdir(parents=True, mode=0o700)
        review_counts = Counter()
        two_reviewer_counts = Counter()
        counts = Counter()
        kinds = {kind: Counter() for kind in KINDS}
        cohorts = {cohort: Counter() for cohort in COHORTS}
        output_rows = []
        label_hashes = {}
        delta = []
        for image_id, row in rows.items():
            entry = entries[image_id]
            split, status = row["split"], entry["status"]
            review_counts[status] += 1
            if len(entry["reviewers"]) >= 2:
                two_reviewer_counts[status] += 1
            image_relative = f"images/{split}/{image_id}.jpg"
            label_relative = f"labels/{split}/{image_id}.txt"
            before_label_sha = manifest["label_hashes"][label_relative]
            derived_kind = None
            after_label_sha = None
            if status != "quarantine":
                image_target = target / image_relative
                with (source / image_relative).open("rb") as src, image_target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                image_target.chmod(0o600)
                if status == "keep":
                    with (source / label_relative).open("rb") as src, (target / label_relative).open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                    derived_kind = row["kind"]
                else:
                    with (target / label_relative).open("xb") as dst:
                        dst.write(_yolo(entry["boxes"]))
                    derived_kind = "positive" if entry["boxes"] else "negative"
                (target / label_relative).chmod(0o600)
                after_label_sha = digest(target / label_relative)
                if digest(source / image_relative) != row["sha256"] or digest(image_target) != row["sha256"]:
                    raise ValueError("Source or copied image changed")
                if digest(source / label_relative) != before_label_sha:
                    raise ValueError("Source label changed")
                output_rows.append({**row, "kind": derived_kind})
                label_hashes[label_relative] = after_label_sha
                counts[split] += 1
                kinds[derived_kind][split] += 1
                cohorts[row["cohort"]][split] += 1
            delta.append({"image_id": image_id, "split": split, "cohort": row["cohort"],
                          "status": status, "source_kind": row["kind"], "derived_kind": derived_kind,
                          "reviewers": entry["reviewers"],
                          "image_sha256": row["sha256"], "source_label_sha256": before_label_sha,
                          "derived_label_sha256": after_label_sha,
                          **({"reason": entry["reason"]} if status != "keep" else {}),
                          **({"boxes": entry["boxes"]} if status == "replace_boxes" else {})})
        if any(counts[split] == 0 or kinds["positive"][split] == 0 for split in SPLITS):
            raise ValueError("Each derived split needs positive images")
        if (digest(source_manifest_path) != manifest_sha or digest(source_config_path) != config_sha
                or digest(ledger_path) != ledger_sha):
            raise ValueError("Source manifest, config, or review ledger changed")
        _validate_source(source)
        positive_ids = {row["image_id"] for row in output_rows if row["kind"] == "positive"}
        groups = []
        grouped = set()
        for group in manifest["groups"]:
            ids = [image_id for image_id in group["image_ids"] if image_id in positive_ids]
            if ids:
                groups.append({**group, "image_ids": ids})
                grouped.update(ids)
        for image_id in sorted(positive_ids - grouped):
            group_id = hashlib.sha256(f"reclassified:{image_id}".encode()).hexdigest()[:16]
            if any(group["group_id"] == group_id for group in groups):
                raise ValueError("Reclassified group ID collides with source group")
            groups.append({"group_id": group_id, "image_ids": [image_id],
                           "split": rows[image_id]["split"], "origin": "reclassified"})
        delta_payload = json.dumps({"version": 1, "review_id": ledger["review_id"],
                                    "entries": delta}, indent=2, sort_keys=True).encode() + b"\n"
        delta_sha = hashlib.sha256(delta_payload).hexdigest()
        summary = {"version": 1, "seed": manifest.get("seed"), "cat_class_id": CAT_CLASS_ID,
                   "source_split_manifest_sha256": manifest_sha, "source_dataset_yaml_sha256": config_sha,
                   "source_groups_sha256": hashlib.sha256(json.dumps(manifest["groups"], sort_keys=True).encode()).hexdigest(),
                   "review_ledger_sha256": ledger_sha, "review_delta_sha256": delta_sha,
                   "review_id": ledger["review_id"], "auditors": ledger.get("auditors", []),
                   "review_counts": dict(review_counts), "two_reviewer_counts": dict(two_reviewer_counts),
                   "counts": dict(counts),
                   "positive_counts": dict(kinds["positive"]), "negative_counts": dict(kinds["negative"]),
                   "cohort_counts": {name: dict(values) for name, values in cohorts.items()},
                   "groups": groups, "images": output_rows, "label_hashes": label_hashes}
        config = {**config, "path": str(target)}
        for name, payload in (("review-delta.json", delta_payload),
                              ("split-manifest.json", json.dumps(summary, indent=2, sort_keys=True).encode() + b"\n"),
                              ("dataset.yaml", json.dumps(config, indent=2).encode() + b"\n")):
            with (target / name).open("xb") as stream:
                stream.write(payload)
            (target / name).chmod(0o600)
        return summary
    finally:
        os.umask(old_umask)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dataset", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = apply(args.source_dataset, args.ledger, args.output)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Overlay failed: {type(exc).__name__}\n")
    print(json.dumps({"counts": result["counts"], "review_counts": result["review_counts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
