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


@pytest.mark.parametrize(("requested", "expected"), [
    (1, {"train": 1, "val": 0, "test": 0}),
    (2, {"train": 1, "val": 1, "test": 0}),
    (49, {"train": 33, "val": 8, "test": 8}),
    (150, {"train": 100, "val": 25, "test": 25}),
])
def test_role_quotas_preserve_default_and_small_batch(requested, expected):
    assert collector._role_quotas(requested) == expected


def _exclude_run(tmp_path, name, **entry):
    run = tmp_path / name
    run.mkdir()
    (run / "inventory.json").write_text(json.dumps({
        "schema_version": 1,
        "images": [{
            "image_id": C, "sha256": "a" * 64,
            "source_url": f"{collector.IMAGE_BASE}/validation/{C}.jpg",
            "author": "other", "author_profile_url": "https://example.org/author/other",
            "original_url": "https://example.org/raw/other", "landing_url": "https://example.org/other",
            **entry,
        }],
    }))
    return run


@pytest.mark.parametrize(("field", "value", "blocked"), [
    ("image_id", V, V),
    ("sha256", hashlib.sha256(_jpeg("green")).hexdigest(), V),
    ("author", "v", V),
    ("author_profile_url", "https://example.org/author/v", V),
    ("landing_url", f"https://example.org/{T}", T),
    ("original_url", f"https://example.org/raw/{T}", T),
    ("source_url", f"{collector.IMAGE_BASE}/test/{T}.jpg", T),
])
def test_exclude_run_blocks_each_identity_key(tmp_path, monkeypatch, field, value, blocked):
    positive, calls, _ = _fixture(tmp_path, monkeypatch)
    run = _exclude_run(tmp_path, "old", **{field: value})
    result = collector.collect(positive, tmp_path / "new", exclude_runs=(run,), max_images=2)
    assert result["collected"] == 1 and result["status"] == "partial"
    if field != "sha256":
        assert f"{collector.IMAGE_BASE}/{'test' if blocked == T else 'validation'}/{blocked}.jpg" not in calls
    assert result["excluded_runs"][0]["inventory_sha256"] == hashlib.sha256(
        (run / "inventory.json").read_bytes()
    ).hexdigest()


def test_multiple_exclude_runs_union_and_unmodified_source(tmp_path, monkeypatch):
    positive, calls, _ = _fixture(tmp_path, monkeypatch)
    first = _exclude_run(tmp_path, "first", image_id=V)
    second = _exclude_run(tmp_path, "second", image_id=T)
    originals = [(run / "inventory.json").read_bytes() for run in (first, second)]
    result = collector.collect(positive, tmp_path / "new", exclude_runs=(first, second), max_images=2)
    assert result["collected"] == 0 and result["status"] == "partial"
    assert len(result["excluded_runs"]) == 2
    assert all((run / "inventory.json").read_bytes() == payload
               for run, payload in zip((first, second), originals, strict=True))
    assert f"{collector.IMAGE_BASE}/validation/{V}.jpg" not in calls
    assert f"{collector.IMAGE_BASE}/test/{T}.jpg" not in calls


def test_invalid_or_changed_exclusion_run_rejected(tmp_path, monkeypatch):
    positive, _, _ = _fixture(tmp_path, monkeypatch)
    run = _exclude_run(tmp_path, "old")
    data = json.loads((run / "inventory.json").read_text())
    del data["images"][0]["author_profile_url"]
    del data["images"][0]["author"]
    (run / "inventory.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="attribution"):
        collector.collect(positive, tmp_path / "new", exclude_runs=(run,))
    assert not (tmp_path / "new").exists()
    data["images"][0]["author"] = "other"
    data["images"][0]["author_profile_url"] = "https://example.org/author/other"
    (run / "inventory.json").write_text(json.dumps(data))
    original_fetch = collector._fetch

    def mutate_on_download(url, limit, timeout):
        if url == collector.SOURCES["test_labels"][0]:
            (run / "inventory.json").write_text("changed")
        return original_fetch(url, limit, timeout)

    monkeypatch.setattr(collector, "_fetch", mutate_on_download)
    with pytest.raises(ValueError, match="Exclusion run changed"):
        collector.collect(positive, tmp_path / "changed", exclude_runs=(run,), max_images=1)


def test_older_positive_exclusion_reads_verified_metadata(tmp_path, monkeypatch):
    positive, _, _ = _fixture(tmp_path, monkeypatch)
    keys, watched = collector._exclude_evidence(positive)
    assert "AuthorProfileURL:https://example.org/author/shared" in keys["authors"]
    assert f"https://example.org/raw/{P}" in keys["urls"]
    assert positive / "source" / "images.csv" in watched
    (positive / "source" / "images.csv").write_text("changed")
    with pytest.raises(ValueError, match="metadata source hash changed"):
        collector._exclude_evidence(positive)


@pytest.mark.parametrize(("field", "value", "error"), [
    ("sha256", "bad", "SHA-256"),
    ("sha256", "G" * 64, "SHA-256"),
    ("source_url", "file:///tmp/image.jpg", "source_url"),
    ("source_url", 123, "source_url"),
    ("author", 123, "author identity"),
])
def test_invalid_exclusion_identity_rejected_before_output(tmp_path, monkeypatch, field, value, error):
    positive, _, _ = _fixture(tmp_path, monkeypatch)
    run = _exclude_run(tmp_path, "bad", **{field: value})
    with pytest.raises(ValueError, match=error):
        collector.collect(positive, tmp_path / "new", exclude_runs=(run,))
    assert not (tmp_path / "new").exists()


def test_legacy_metadata_must_cover_inventory_ids(tmp_path, monkeypatch):
    positive, _, _ = _fixture(tmp_path, monkeypatch)
    inventory_path = positive / "inventory.json"
    inventory = json.loads(inventory_path.read_text())
    inventory["images"][0]["image_id"] = T
    inventory_path.write_text(json.dumps(inventory))
    with pytest.raises(ValueError, match="missing image IDs"):
        collector._exclude_evidence(positive)


def test_negative_exclusion_author_without_profile_is_valid(tmp_path):
    run = _exclude_run(tmp_path, "old", author_profile_url="")
    keys, _ = collector._exclude_evidence(run)
    assert "Author:other" in keys["authors"]


def test_output_inside_exclusion_run_rejected(tmp_path):
    run = _exclude_run(tmp_path, "old")
    with pytest.raises(ValueError, match="output inside"):
        collector.collect(tmp_path / "positive", run / "new", exclude_runs=(run,))
    assert not (run / "new").exists()


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
