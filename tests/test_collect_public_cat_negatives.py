from __future__ import annotations

import hashlib
import io
import json

import pytest
from PIL import Image

from scripts import collect_public_cat_negatives as collector
from scripts.evaluate_pet_detection import load_manifest

P, V, T, C, A = (f"{value:016x}" for value in range(1, 6))


def _jpeg(color: str) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(stream, format="JPEG")
    return stream.getvalue()


def _metadata(image_id: str, split: str, author: str, rotation: str = "0.0") -> str:
    return (
        f"{image_id},{split},https://example.org/raw/{image_id},https://example.org/{image_id},"
        f"https://creativecommons.org/licenses/by/2.0/,https://example.org/author/{author},"
        f"{author},Title,1,md5,thumb,{rotation}\n"
    )


HEADER = "ImageID,Subset,OriginalURL,OriginalLandingURL,License,AuthorProfileURL,Author,Title,OriginalSize,OriginalMD5,Thumbnail300KURL,Rotation\n"
BOX_HEADER = "ImageID,LabelName\n"
LABEL_HEADER = "ImageID,Source,LabelName,Confidence\n"


def _fixture(tmp_path, monkeypatch, *, extra_labels="", image_override=None,
             test_rotation="0.0", test_license="https://creativecommons.org/licenses/by/2.0/"):
    positive = tmp_path / "positive"
    (positive / "source").mkdir(parents=True)
    boxes = (BOX_HEADER + f"{P},/m/01yrx\n{C},/m/01yrx\n").encode()
    images = (HEADER + _metadata(P, "validation", "shared") + _metadata(V, "validation", "v") +
              _metadata(C, "validation", "c") + _metadata(A, "validation", "shared")).encode()
    for name, payload in (("boxes", boxes), ("images", images)):
        (positive / "source" / f"{name}.csv").write_bytes(payload)
    (positive / "inventory.json").write_text(json.dumps({
        "schema_version": 1,
        "source_files": {name: {"sha256": hashlib.sha256(data).hexdigest()}
                         for name, data in (("boxes", boxes), ("images", images))},
        "images": [{"image_id": P, "sha256": hashlib.sha256(_jpeg("red")).hexdigest(),
                    "source_url": f"{collector.IMAGE_BASE}/validation/{P}.jpg",
                    "landing_url": f"https://example.org/{P}"}],
    }))
    source = {
        "validation_labels": (LABEL_HEADER + f"{V},verification,/m/01yrx,0.0\n"
                              f"{C},verification,/m/01yrx,0.0\n"
                              f"{A},verification,/m/01yrx,0.0\n" + extra_labels).encode(),
        "test_labels": (LABEL_HEADER + f"{T},crowdsource-verification,/m/01yrx,0\n").encode(),
        "test_images": (HEADER + _metadata(T, "test", "t", test_rotation).replace(
            "https://creativecommons.org/licenses/by/2.0/", test_license)).encode(),
        "test_boxes": BOX_HEADER.encode(),
    }
    urls = {url: source[name] for name, (url, _) in collector.SOURCES.items()}
    urls.update({
        f"{collector.IMAGE_BASE}/validation/{V}.jpg": _jpeg("green"),
        f"{collector.IMAGE_BASE}/test/{T}.jpg": _jpeg("blue"),
    })
    if image_override is not None:
        urls[f"{collector.IMAGE_BASE}/test/{T}.jpg"] = image_override
    calls = []

    def fetch(url, limit, _timeout):
        calls.append(url)
        payload = urls[url]
        if len(payload) > limit:
            raise ValueError("byte limit")
        return payload

    monkeypatch.setattr(collector, "_fetch", fetch)
    return positive, calls, source


def test_collects_verified_negatives_with_provenance_and_empty_boxes(tmp_path, monkeypatch):
    positive, calls, source = _fixture(tmp_path, monkeypatch)
    out = tmp_path / "negative"
    result = collector.collect(positive, out, max_images=2)
    manifest = json.loads((out / "manifest.json").read_text())
    assert result["status"] == "complete" and result["collected"] == 2
    assert len(load_manifest(out / "manifest.json")) == 2
    assert all(sample["boxes"] == [] for sample in manifest["samples"])
    assert [item["source_split"] for item in result["images"]] == ["validation", "test"]
    assert result["images"][1]["cat_label"]["Source"] == ["crowdsource-verification"]
    assert result["selection_seed"] == 42
    assert result["source_files"]["test_labels"]["sha256"] == hashlib.sha256(source["test_labels"]).hexdigest()
    assert f"{collector.IMAGE_BASE}/validation/{C}.jpg" not in calls
    assert f"{collector.IMAGE_BASE}/validation/{A}.jpg" not in calls
    assert (out / "inventory.json").stat().st_mode & 0o777 == 0o600


def test_roles_are_fixed_before_model_results(tmp_path, monkeypatch):
    positive, _, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(collector, "ROLES", {"train": 1, "val": 1, "test": 0})
    result = collector.collect(positive, tmp_path / "negative", max_images=2)
    assert [entry["role"] for entry in result["images"]] == ["train", "val"]


def test_seeded_order_is_reproducible_and_mixes_source_splits():
    keys = {(split, f"{number:016x}"): None
            for split in ("validation", "test") for number in range(1, 9)}
    first = collector._selection_order(keys)
    assert first == collector._selection_order(dict(reversed(list(keys.items()))))
    assert {split for split, _ in first[:5]} == {"validation", "test"}


@pytest.mark.parametrize("invalid", [
    {"test_rotation": ""},
    {"test_rotation": "90.0"},
    {"test_license": ""},
])
def test_rotation_and_license_rejected(tmp_path, monkeypatch, invalid):
    positive, calls, _ = _fixture(tmp_path, monkeypatch, **invalid)
    result = collector.collect(positive, tmp_path / "negative", max_images=2)
    assert result["collected"] == 1
    assert f"{collector.IMAGE_BASE}/test/{T}.jpg" not in calls


@pytest.mark.parametrize("bad", [
    f"{V},machine,/m/01yrx,0.0\n",
    f"{V},verification,/m/01yrx,1.0\n",
    f"{V},verification,/m/01yrx,0.5\n",
])
def test_conflicting_cat_label_excludes_image(tmp_path, monkeypatch, bad):
    positive, calls, _ = _fixture(tmp_path, monkeypatch, extra_labels=bad)
    result = collector.collect(positive, tmp_path / "negative", max_images=2)
    assert result["collected"] == 1 and result["status"] == "partial"
    assert f"{collector.IMAGE_BASE}/validation/{V}.jpg" not in calls


def test_positive_source_hash_and_no_overwrite(tmp_path, monkeypatch):
    positive, _, _ = _fixture(tmp_path, monkeypatch)
    out = tmp_path / "negative"
    collector.collect(positive, out, max_images=1)
    old = (out / "inventory.json").read_bytes()
    with pytest.raises(FileExistsError):
        collector.collect(positive, out, max_images=1)
    assert (out / "inventory.json").read_bytes() == old
    (positive / "source" / "boxes.csv").write_text("changed")
    with pytest.raises(ValueError, match="source hash changed"):
        collector.collect(positive, tmp_path / "new", max_images=1)
    assert not (tmp_path / "new").exists()


def test_existing_positive_id_cannot_be_collected(tmp_path, monkeypatch):
    positive, calls, _ = _fixture(
        tmp_path, monkeypatch, extra_labels=f"{P},verification,/m/01yrx,0.0\n"
    )
    collector.collect(positive, tmp_path / "negative", max_images=2)
    assert f"{collector.IMAGE_BASE}/validation/{P}.jpg" not in calls


def test_invalid_source_image_id_rejected():
    labels = (LABEL_HEADER + "../../x,verification,/m/01yrx,0.0\n").encode()
    with pytest.raises(ValueError, match="Invalid Cat ImageID"):
        collector._negative_labels(labels)


def test_hash_duplicate_and_bad_jpeg_are_not_written(tmp_path, monkeypatch):
    positive, _, _ = _fixture(tmp_path, monkeypatch, image_override=_jpeg("red"))
    out = tmp_path / "negative"
    result = collector.collect(positive, out, max_images=2)
    assert result["collected"] == 1
    assert result["skipped"]["image_hash_overlap"] == 1
    assert not (out / "images" / f"{T}.jpg").exists()
    positive, _, _ = _fixture(tmp_path / "other", monkeypatch, image_override=b"bad")
    result = collector.collect(positive, tmp_path / "other" / "negative", max_images=2)
    assert result["skipped"]["image_download_or_decode_failed"] == 1


def test_limits_and_output_inside_positive_run_rejected(tmp_path):
    for kwargs in ({"max_images": 151}, {"max_image_bytes": 2_000_001},
                   {"max_total_bytes": 200_000_001}, {"deadline_seconds": 0}):
        with pytest.raises(ValueError):
            collector.collect(tmp_path / "positive", tmp_path / "negative", **kwargs)
    with pytest.raises(ValueError, match="output inside"):
        collector.collect(tmp_path / "positive", tmp_path / "positive" / "new")


@pytest.mark.parametrize("git_kind", ["directory", "worktree-file"])
def test_output_inside_git_rejected_before_source_read(tmp_path, git_kind):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = repo / ".git"
    if git_kind == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: elsewhere")
    with pytest.raises(ValueError, match="Git"):
        collector.collect(tmp_path / "positive", repo / "data" / "run")
    assert not (repo / "data").exists()
