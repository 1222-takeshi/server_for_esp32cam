from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import apply_cat_annotation_overlay as overlay


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def fixture(tmp_path: Path):
    source = tmp_path / "source"
    rows = []
    hashes = {}
    groups = []
    specs = [("train", "positive", "existing"), ("train", "positive", "new"),
             ("train", "negative", "new"), ("val", "positive", "existing"),
             ("val", "positive", "new"), ("test", "positive", "existing")]
    for index, (split, kind, cohort) in enumerate(specs):
        image_id = f"{index + 1:016x}"
        sha = _write(source / "images" / split / f"{image_id}.jpg", f"JPEG{index}".encode())
        label = b"15 0.5 0.5 0.4 0.4\n" if kind == "positive" else b""
        relative = f"labels/{split}/{image_id}.txt"
        hashes[relative] = _write(source / relative, label)
        rows.append({"image_id": image_id, "sha256": sha, "split": split, "kind": kind, "cohort": cohort})
        if kind == "positive":
            groups.append({"group_id": f"group-{index}", "image_ids": [image_id], "split": split})
    manifest = {"version": 1, "seed": 42, "cat_class_id": 15, "images": rows,
                "label_hashes": hashes, "groups": groups, "counts": {"train": 3, "val": 2, "test": 1}}
    names = {str(index): f"class-{index}" for index in range(80)}
    names["15"] = "cat"
    config = {"path": str(source.resolve()), "train": "images/train", "val": "images/val",
              "test": "images/test", "names": names}
    manifest_sha = _write(source / "split-manifest.json", json.dumps(manifest).encode())
    config_sha = _write(source / "dataset.yaml", json.dumps(config).encode())
    entries = [{"image_id": row["image_id"], "image_sha256": row["sha256"],
                "label_sha256": hashes[f"labels/{row['split']}/{row['image_id']}.txt"],
                "split": row["split"], "kind": row["kind"], "cohort": row["cohort"],
                "status": "keep", "reviewers": ["auditor-a"]} for row in rows]
    ledger = {"version": 1, "source_split_manifest_sha256": manifest_sha,
              "source_dataset_yaml_sha256": config_sha, "review_id": "audit-001",
              "auditors": ["auditor-a", "auditor-b"], "entries": entries}
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger))
    return source, path, ledger, rows


def _change(entry: dict, status: str, *, boxes: list | None = None):
    entry["status"] = status
    entry["reviewers"] = ["auditor-a", "auditor-b"]
    entry["reason"] = "visual review"
    if boxes is not None:
        entry["boxes"] = boxes


def test_applies_full_review_without_resplitting_or_changing_source(tmp_path):
    source, ledger_path, ledger, rows = fixture(tmp_path)
    before_manifest = overlay.digest(source / "split-manifest.json")
    _change(ledger["entries"][0], "quarantine")
    boxes = [[0.1, 0.2, 0.1000000001, 0.8], [0.6, 0.1, 0.9, 0.4]]
    _change(ledger["entries"][2], "replace_boxes", boxes=boxes)
    _change(ledger["entries"][3], "replace_boxes", boxes=[])
    ledger_path.write_text(json.dumps(ledger))
    output = tmp_path / "derived"
    result = overlay.apply(source, ledger_path, output)
    assert result["counts"] == {"train": 2, "val": 2, "test": 1}
    assert result["positive_counts"] == {"train": 2, "val": 1, "test": 1}
    assert result["review_counts"] == {"quarantine": 1, "keep": 3, "replace_boxes": 2}
    assert result["two_reviewer_counts"] == {"quarantine": 1, "replace_boxes": 2}
    assert not (output / "images/train" / f"{rows[0]['image_id']}.jpg").exists()
    replacement = (output / "labels/train" / f"{rows[2]['image_id']}.txt").read_text().splitlines()
    assert len(replacement) == 2 and all(line.startswith("15 ") for line in replacement)
    assert float(replacement[0].split()[3]) > 0
    assert (output / "labels/val" / f"{rows[3]['image_id']}.txt").read_bytes() == b""
    assert next(row for row in result["images"] if row["image_id"] == rows[2]["image_id"])["kind"] == "positive"
    assert next(row for row in result["images"] if row["image_id"] == rows[3]["image_id"])["kind"] == "negative"
    assert all(row["split"] == original["split"] and row["cohort"] == original["cohort"]
               for row in result["images"] for original in rows if row["image_id"] == original["image_id"])
    assert next(group for group in result["groups"] if group.get("origin") == "reclassified")["image_ids"] == [rows[2]["image_id"]]
    assert overlay.digest(source / "split-manifest.json") == before_manifest
    delta = json.loads((output / "review-delta.json").read_text())
    assert len(delta["entries"]) == 6
    assert next(row for row in delta["entries"] if row["image_id"] == rows[2]["image_id"])["source_kind"] == "negative"
    assert overlay.digest(output / "review-delta.json") == result["review_delta_sha256"]
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "dataset.yaml").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="new private"):
        overlay.apply(source, ledger_path, output)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "stale_image", "stale_label",
                                      "wrong_split", "one_reviewer_change", "same_reviewer_twice",
                                      "missing_reason", "invalid_box", "nan_box", "stale_manifest"])
def test_rejects_invalid_or_incomplete_ledger_before_output(tmp_path, mutation):
    source, path, ledger, rows = fixture(tmp_path)
    entries = ledger["entries"]
    if mutation == "missing":
        entries.pop()
    elif mutation == "duplicate":
        entries[-1] = entries[0].copy()
    elif mutation == "unknown":
        entries[0]["image_id"] = "f" * 16
    elif mutation == "stale_image":
        entries[0]["image_sha256"] = "0" * 64
    elif mutation == "stale_label":
        entries[0]["label_sha256"] = "0" * 64
    elif mutation == "wrong_split":
        entries[0]["split"] = "test"
    elif mutation == "one_reviewer_change":
        _change(entries[0], "quarantine")
        entries[0]["reviewers"] = ["auditor-a"]
    elif mutation == "same_reviewer_twice":
        _change(entries[0], "quarantine")
        entries[0]["reviewers"] = ["auditor-a", "AUDITOR-A"]
    elif mutation == "missing_reason":
        _change(entries[0], "quarantine")
        del entries[0]["reason"]
    elif mutation == "invalid_box":
        _change(entries[0], "replace_boxes", boxes=[[0.8, 0.1, 0.2, 0.5]])
    elif mutation == "nan_box":
        _change(entries[0], "replace_boxes", boxes=[[0.1, 0.2, float("nan"), 0.5]])
    elif mutation == "stale_manifest":
        ledger["source_split_manifest_sha256"] = "0" * 64
    path.write_text(json.dumps(ledger))
    output = tmp_path / "derived"
    with pytest.raises(ValueError):
        overlay.apply(source, path, output)
    assert not output.exists()


def test_rejects_source_hash_mismatch_and_unsafe_output(tmp_path):
    source, path, _, rows = fixture(tmp_path)
    with pytest.raises(ValueError, match="outside source"):
        overlay.apply(source, path, source / "derived")
    git = tmp_path / "repo"
    git.mkdir()
    (git / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(ValueError, match="outside source and Git"):
        overlay.apply(source, path, git / "derived")
    (source / "images/train" / f"{rows[0]['image_id']}.jpg").write_bytes(b"mutated")
    with pytest.raises(ValueError, match="SHA-mismatched"):
        overlay.apply(source, path, tmp_path / "derived")
    assert not (tmp_path / "derived").exists()


def test_rejects_ambiguous_duplicate_json_keys(tmp_path):
    source, path, _, _ = fixture(tmp_path)
    path.write_text(path.read_text()[:-1] + ', "review_id": "changed"}')
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        overlay.apply(source, path, tmp_path / "derived")
    assert not (tmp_path / "derived").exists()


@pytest.mark.parametrize("changed", ["manifest", "ledger"])
def test_parsed_json_and_recorded_hash_use_same_bytes(tmp_path, monkeypatch, changed):
    source, ledger, _, _ = fixture(tmp_path)
    changed_path = source / "split-manifest.json" if changed == "manifest" else ledger
    original = overlay._json_file
    altered = False

    def read_then_replace(path, limit):
        nonlocal altered
        result = original(path, limit)
        if path == changed_path and not altered:
            changed_path.write_bytes(changed_path.read_bytes() + b" ")
            altered = True
        return result

    monkeypatch.setattr(overlay, "_json_file", read_then_replace)
    output = tmp_path / "derived"
    with pytest.raises(ValueError, match="changed"):
        overlay.apply(source, ledger, output)
    assert not (output / "split-manifest.json").exists()
    assert not (output / "review-delta.json").exists()


@pytest.mark.parametrize("changed", ["image", "label", "manifest", "config", "ledger"])
def test_copy_time_input_mutation_leaves_no_final_metadata(tmp_path, monkeypatch, changed):
    source, ledger, _, rows = fixture(tmp_path)
    targets = {"image": source / "images/train" / f"{rows[0]['image_id']}.jpg",
               "label": source / "labels/train" / f"{rows[0]['image_id']}.txt",
               "manifest": source / "split-manifest.json", "config": source / "dataset.yaml",
               "ledger": ledger}
    original = overlay.shutil.copyfileobj
    mutated = False

    def copy_then_mutate(src, dst):
        nonlocal mutated
        original(src, dst)
        if not mutated:
            with targets[changed].open("ab") as stream:
                stream.write(b"changed")
            mutated = True

    monkeypatch.setattr(overlay.shutil, "copyfileobj", copy_then_mutate)
    output = tmp_path / "derived"
    with pytest.raises(ValueError, match="changed|mismatch"):
        overlay.apply(source, ledger, output)
    assert not (output / "split-manifest.json").exists()
    assert not (output / "dataset.yaml").exists()


def test_keep_rejects_corrupted_copied_label(tmp_path, monkeypatch):
    source, ledger, _, _ = fixture(tmp_path)
    original = overlay.shutil.copyfileobj

    def corrupt_label_copy(src, dst):
        original(src, dst)
        if src.name.endswith(".txt"):
            dst.write(b"corrupt")

    monkeypatch.setattr(overlay.shutil, "copyfileobj", corrupt_label_copy)
    output = tmp_path / "derived"
    with pytest.raises(ValueError, match="Derived label SHA mismatch"):
        overlay.apply(source, ledger, output)
    assert not (output / "split-manifest.json").exists()
    assert not (output / "review-delta.json").exists()
