from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from scripts import prepare_cat_training_dataset as cli


def image_bytes(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (12, 10), color)
    stream = io.BytesIO()
    image.save(stream, format="JPEG")
    return stream.getvalue()


def source_fixture(tmp_path: Path, count: int = 8):
    root = tmp_path / "source"
    (root / "images").mkdir(parents=True)
    manifest = {"version": 1, "samples": []}
    inventory = {"schema_version": 1, "collected": count, "status": "partial", "images": []}
    for index in range(count):
        image_id = f"image{index:02d}"
        body = image_bytes((index * 25, 10, 20))
        path = root / "images" / f"{image_id}.jpg"
        path.write_bytes(body)
        sha = hashlib.sha256(body).hexdigest()
        boxes = [[0.1, 0.2, 0.5, 0.8]]
        if index == 0:
            boxes.append([0.6, 0.1, 0.9, 0.4])
        manifest["samples"].append({"path": f"images/{image_id}.jpg", "sha256": sha,
                                    "boxes": boxes})
        inventory["images"].append({"image_id": image_id, "sha256": sha, "boxes": boxes,
                                    "author": f"author{index // 2}",
                                    "source_url": f"https://example.org/{image_id}",
                                    "landing_url": f"https://example.org/page/{image_id}",
                                    "license": "CC BY 2.0", "box_flags": [{} for _ in boxes]})
    manifest_path = root / "manifest.json"
    inventory_path = root / "inventory.json"
    manifest_path.write_text(json.dumps(manifest))
    inventory_path.write_text(json.dumps(inventory))
    return manifest_path, inventory_path


def test_prepares_immutable_yolo_splits_with_multi_cat_boxes(tmp_path):
    manifest, inventory = source_fixture(tmp_path)
    original_hash = cli.digest(manifest)
    output = tmp_path / "derived"
    result = cli.prepare(manifest, inventory, output)
    assert sum(result["counts"].values()) == 8
    assert all(result["counts"][split] for split in cli.SPLITS)
    assert result["cat_boxes"] == 9
    assert result["source"]["manifest_sha256"] == original_hash
    assert cli.digest(manifest) == original_hash
    assert json.loads((output / "dataset.yaml").read_text())["names"] == {"0": "cat"}
    assert (output / "split-manifest.json").stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700
    assert len(list(output.glob("images/*/*.jpg"))) == 8
    for row in result["images"]:
        copied = output / "images" / row["split"] / f"{row['image_id']}.jpg"
        assert cli.digest(copied) == row["sha256"]
        assert copied.stat().st_mode & 0o777 == 0o600
    first = next(row for row in result["images"] if row["image_id"] == "image00")
    labels = (output / "labels" / first["split"] / "image00.txt").read_text().splitlines()
    assert labels == ["0 0.300000000 0.500000000 0.400000000 0.600000000",
                      "0 0.750000000 0.250000000 0.300000000 0.300000000"]
    with pytest.raises(ValueError, match="new directory"):
        cli.prepare(manifest, inventory, output)


def test_author_and_source_url_are_whole_groups_and_deterministic(tmp_path):
    manifest, inventory = source_fixture(tmp_path)
    payload = json.loads(inventory.read_text())
    payload["images"][4]["source_url"] = payload["images"][0]["source_url"]
    inventory.write_text(json.dumps(payload))
    rows, _ = cli.load_inputs(manifest, inventory)
    assigned, groups = cli.assign_splits(rows)
    assert assigned["image00"] == assigned["image01"] == assigned["image04"] == assigned["image05"]
    assert assigned == cli.assign_splits(list(reversed(rows)))[0]
    assert len(groups) >= 3


@pytest.mark.parametrize("mutation", ["missing_inventory", "wrong_sha", "duplicate_path",
                                      "duplicate_hash", "negative", "wrong_box", "missing_author",
                                      "unsafe_path"])
def test_rejects_incomplete_or_mismatched_sources_before_writing(tmp_path, mutation):
    manifest, inventory = source_fixture(tmp_path)
    m = json.loads(manifest.read_text())
    i = json.loads(inventory.read_text())
    if mutation == "missing_inventory":
        inventory.unlink()
    elif mutation == "wrong_sha":
        m["samples"][0]["sha256"] = "0" * 64
    elif mutation == "duplicate_path":
        m["samples"][1] = m["samples"][0]
    elif mutation == "duplicate_hash":
        m["samples"][1]["sha256"] = m["samples"][0]["sha256"]
    elif mutation == "negative":
        m["samples"][0]["boxes"] = []
        i["images"][0]["boxes"] = []
        i["images"][0]["box_flags"] = []
    elif mutation == "wrong_box":
        i["images"][0]["boxes"][0][0] = 0.0
    elif mutation == "missing_author":
        i["images"][0]["author"] = ""
    elif mutation == "unsafe_path":
        m["samples"][0]["path"] = "../other.jpg"
    if mutation != "missing_inventory":
        inventory.write_text(json.dumps(i))
    manifest.write_text(json.dumps(m))
    output = tmp_path / "derived"
    with pytest.raises((ValueError, OSError)):
        cli.prepare(manifest, inventory, output)
    assert not output.exists()


def test_rejects_output_under_source_or_git(tmp_path):
    manifest, inventory = source_fixture(tmp_path)
    with pytest.raises(ValueError):
        cli.prepare(manifest, inventory, manifest.parent / "derived")
    git_root = tmp_path / "repo"
    git_root.mkdir()
    (git_root / ".git").mkdir()
    with pytest.raises(ValueError):
        cli.prepare(manifest, inventory, git_root / "derived")


def test_too_few_independent_groups_rejected(tmp_path):
    manifest, inventory = source_fixture(tmp_path, count=3)
    data = json.loads(inventory.read_text())
    for item in data["images"]:
        item["author"] = "one-author"
    inventory.write_text(json.dumps(data))
    rows, _ = cli.load_inputs(manifest, inventory)
    with pytest.raises(ValueError, match="three independent"):
        cli.assign_splits(rows)
