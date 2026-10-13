from __future__ import annotations

import io
import hashlib
import json
import time

import pytest
from PIL import Image

from scripts import collect_public_cat_dataset as collector
from scripts.evaluate_pet_detection import load_manifest

ID_A = "000000000000000a"
ID_B = "000000000000000b"
ID_C = "000000000000000c"


def _jpeg() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "black").save(stream, format="JPEG")
    return stream.getvalue()


def _sources() -> dict[str, bytes]:
    classes = b"/m/01yrx,Cat\n/m/0bt9lr,Dog\n"
    box_header = (
        "ImageID,LabelName,XMin,XMax,YMin,YMax,IsGroupOf,IsDepiction,IsInside,IsOccluded,IsTruncated\n"
    )
    boxes = (box_header + "".join([
        f"{ID_A},/m/01yrx,0.1,0.3,0.2,0.4,0,0,0,1,0\n",
        f"{ID_A},/m/01yrx,0.5,0.8,0.5,0.9,0,0,0,0,1\n",
        f"{ID_B},/m/01yrx,0.1,0.3,0.2,0.4,1,0,0,0,0\n",
        f"{ID_B},/m/01yrx,0.4,0.7,0.4,0.7,0,0,0,0,0\n",
        f"{ID_C},/m/01yrx,0.1,0.3,0.2,0.4,0,0,0,0,0\n",
    ])).encode()
    metadata = (
        "ImageID,Subset,License,Author,OriginalLandingURL,Title,Rotation\n"
        f"{ID_A},validation,https://creativecommons.org/licenses/by/2.0/,A,https://example.org/a,A,0.0\n"
        f"{ID_B},validation,https://creativecommons.org/licenses/by/2.0/,B,https://example.org/b,B,0\n"
        f"{ID_C},validation,,C,https://example.org/c,C,0\n"
    ).encode()
    return {"classes": classes, "boxes": boxes, "images": metadata}


def _fake_fetch(monkeypatch, sources, image=None):
    image = _jpeg() if image is None else image
    urls = {url: sources[name] for name, (url, _limit) in collector.SOURCES.items()}
    urls[f"{collector.IMAGE_BASE}/{ID_A}.jpg"] = image
    calls = []

    def fetch(url, limit, timeout):
        calls.append(url)
        payload = urls[url]
        if len(payload) > limit:
            raise ValueError("too large")
        return payload

    monkeypatch.setattr(collector, "_fetch", fetch)
    return calls


def test_collects_multi_cat_positive_with_provenance_and_no_negative(tmp_path, monkeypatch):
    calls = _fake_fetch(monkeypatch, _sources())
    out = tmp_path / "public"
    result = collector.collect(out, max_images=2)
    manifest = json.loads((out / "manifest.json").read_text())
    assert result["status"] == "partial" and result["collected"] == 1
    assert result["skipped"]["excluded_images_with_special_box"] == 1
    assert result["skipped"]["license_unconfirmed"] == 1
    assert len(manifest["samples"]) == 1
    assert len(manifest["samples"][0]["boxes"]) == 2
    assert result["images"][0]["license_evidence"] == "Open Images metadata only"
    assert result["images"][0]["box_flags"] == [
        {"IsOccluded": "1", "IsTruncated": "0"},
        {"IsOccluded": "0", "IsTruncated": "1"},
    ]
    assert f"{collector.IMAGE_BASE}/{ID_B}.jpg" not in calls
    assert f"{collector.IMAGE_BASE}/{ID_C}.jpg" not in calls
    assert (out / "images" / f"{ID_A}.jpg").is_file()
    assert (out / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert len(load_manifest(out / "manifest.json")) == 1


def test_existing_output_directory_cannot_be_overwritten(tmp_path, monkeypatch):
    _fake_fetch(monkeypatch, _sources())
    out = tmp_path / "public"
    collector.collect(out, max_images=1)
    before = (out / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        collector.collect(out, max_images=1)
    assert (out / "manifest.json").read_bytes() == before


def test_invalid_jpeg_is_skipped_not_written(tmp_path, monkeypatch):
    _fake_fetch(monkeypatch, _sources(), image=b"not-jpeg")
    out = tmp_path / "public"
    result = collector.collect(out, max_images=1)
    assert result["status"] == "partial"
    assert result["skipped"]["image_download_or_decode_failed"] == 1
    assert not list((out / "images").iterdir())
    assert not (out / "manifest.json").exists()
    assert (out / "inventory.json").is_file()


@pytest.mark.parametrize("rotation", ["", "90.0", "nan", "unknown"])
def test_unresolved_rotation_is_excluded(rotation):
    metadata = collector._csv_rows(_sources()["images"])[0]
    metadata["Rotation"] = rotation
    assert collector._eligible(metadata) == "rotation_unresolved"


def test_special_box_excludes_whole_image():
    boxes, _, reasons = collector._selected_boxes(collector._csv_rows(_sources()["boxes"]))
    assert ID_B not in boxes
    assert reasons["excluded_images_with_special_box"] == 1


@pytest.mark.parametrize("field,value", [("XMax", "0.1"), ("XMin", "nan")])
def test_invalid_boxes_rejected(field, value):
    rows = collector._csv_rows(_sources()["boxes"])
    rows[0][field] = value
    with pytest.raises(ValueError, match="Cat box"):
        collector._selected_boxes(rows)


def test_class_mapping_change_rejected(tmp_path, monkeypatch):
    sources = _sources()
    sources["classes"] = b"/m/wrong,Cat\n"
    _fake_fetch(monkeypatch, sources)
    with pytest.raises(ValueError, match="Cat class mapping"):
        collector.collect(tmp_path / "public", max_images=1)
    assert not (tmp_path / "public" / "manifest.json").exists()


def test_limits_rejected_before_creating_output(tmp_path):
    out = tmp_path / "public"
    with pytest.raises(ValueError, match="Invalid image count"):
        collector.collect(out, max_images=1001)
    assert not out.exists()
    with pytest.raises(ValueError, match="per-image byte limit"):
        collector.collect(out, max_image_bytes=2_000_001)
    with pytest.raises(ValueError, match="total byte limit"):
        collector.collect(out, max_total_bytes=1_000_000_001)
    assert not out.exists()


def test_fetch_has_wall_clock_limit_even_when_response_trickles(monkeypatch):
    class SlowResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            time.sleep(0.2)
            return b"x"

    monkeypatch.setattr(collector, "urlopen", lambda *_args, **_kwargs: SlowResponse())
    with pytest.raises(TimeoutError, match="wall-time"):
        collector._fetch("https://example.org/slow", 100, 0.02)


def test_fetch_rejects_oversize_without_content_length(monkeypatch):
    class OversizeResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, size):
            return b"x" * size

    monkeypatch.setattr(collector, "urlopen", lambda *_args, **_kwargs: OversizeResponse())
    with pytest.raises(ValueError, match="byte limit"):
        collector._fetch("https://example.org/oversize", 4, 1)


def _prior_run(tmp_path, image_id=ID_A, body=None, author="Prior"):
    body = _jpeg() if body is None else body
    run = tmp_path / "prior"
    (run / "images").mkdir(parents=True)
    (run / "images" / f"{image_id}.jpg").write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    (run / "manifest.json").write_text(json.dumps({"version": 1, "samples": [
        {"path": f"images/{image_id}.jpg", "sha256": digest, "boxes": []}]}))
    (run / "inventory.json").write_text(json.dumps({"schema_version": 1, "collected": 1, "images": [
        {"image_id": image_id, "sha256": digest, "author": author,
         "author_profile_url": f"https://example.org/author/{author}",
         "landing_url": f"https://example.org/{image_id}",
         "original_url": f"https://example.org/raw/{image_id}",
         "source_url": f"{collector.IMAGE_BASE}/{image_id}.jpg"}]}))
    return run


def _test_sources(monkeypatch, *, cat_author="New", image_body=None):
    image_body = _jpeg() if image_body is None else image_body
    sources = {
        "classes": _sources()["classes"],
        "boxes": ("ImageID,LabelName,XMin,XMax,YMin,YMax,IsGroupOf,IsDepiction,IsInside,IsOccluded,IsTruncated\n"
                  f"{ID_A},/m/01yrx,0.1,0.3,0.2,0.4,0,0,0,0,0\n"
                  f"{ID_C},/m/01yrx,0.1,0.3,0.2,0.4,0,0,0,0,0\n").encode(),
        "images": ("ImageID,Subset,License,Author,AuthorProfileURL,OriginalLandingURL,OriginalURL,Title,Rotation\n"
                   f"{ID_A},test,https://creativecommons.org/licenses/by/2.0/,Prior,https://example.org/author/Prior,https://example.org/{ID_A},https://example.org/raw/{ID_A},A,0\n"
                   f"{ID_C},test,https://creativecommons.org/licenses/by/2.0/,{cat_author},https://example.org/author/{cat_author},https://example.org/{ID_C},https://example.org/raw/{ID_C},C,0\n").encode(),
    }
    urls = {url: sources[name] for name, (url, _limit) in collector.TEST_SOURCES.items()}
    urls[f"{collector.IMAGE_BASE.rsplit('/', 1)[0]}/test/{ID_A}.jpg"] = image_body
    urls[f"{collector.IMAGE_BASE.rsplit('/', 1)[0]}/test/{ID_C}.jpg"] = image_body
    calls = []

    def fetch(url, limit, _timeout):
        calls.append(url)
        payload = urls[url]
        if len(payload) > limit:
            raise ValueError("too large")
        return payload

    monkeypatch.setattr(collector, "_fetch", fetch)
    return calls


def test_test_split_excludes_prior_id_and_preserves_provenance(tmp_path, monkeypatch):
    prior = _prior_run(tmp_path)
    # Use a distinct valid JPEG so the post-download SHA check does not reject it.
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(stream, format="JPEG")
    calls = _test_sources(monkeypatch, image_body=stream.getvalue())
    out = tmp_path / "expanded"
    result = collector.collect(out, source_split="test", exclude_runs=(prior,), max_images=2)
    assert result["status"] == "partial" and result["collected"] == 1
    assert result["source_split"] == "test"
    assert result["skipped"]["excluded_prior_image_id"] == 1
    assert result["excluded_runs"][0]["inventory_sha256"] == collector._file_sha256(prior / "inventory.json")
    assert result["source_files"]["boxes"]["url"] == collector.TEST_SOURCES["boxes"][0]
    assert result["images"][0]["image_id"] == ID_C
    assert result["images"][0]["author_profile_url"] == "https://example.org/author/New"
    assert result["images"][0]["original_url"] == f"https://example.org/raw/{ID_C}"
    assert f"{collector.IMAGE_BASE.rsplit('/', 1)[0]}/test/{ID_A}.jpg" not in calls
    assert (out / "images" / f"{ID_C}.jpg").is_file()
    assert collector._file_sha256(prior / "images" / f"{ID_A}.jpg") == json.loads((prior / "inventory.json").read_text())["images"][0]["sha256"]


@pytest.mark.parametrize("overlap", ["author", "sha256"])
def test_prior_provenance_or_image_hash_overlap_is_not_saved(tmp_path, monkeypatch, overlap):
    prior = _prior_run(tmp_path, author="New" if overlap == "author" else "Prior")
    _test_sources(monkeypatch)
    out = tmp_path / "expanded"
    result = collector.collect(out, source_split="test", exclude_runs=(prior,), max_images=1)
    assert result["collected"] == 0
    assert not list((out / "images").iterdir())
    assert result["skipped"]["excluded_prior_provenance" if overlap == "author"
                             else "excluded_prior_image_sha256"] == 1


def test_tampered_exclusion_run_rejected_before_output(tmp_path, monkeypatch):
    prior = _prior_run(tmp_path)
    (prior / "images" / f"{ID_A}.jpg").write_bytes(b"changed")
    _test_sources(monkeypatch)
    out = tmp_path / "expanded"
    with pytest.raises(ValueError, match="hash mismatch"):
        collector.collect(out, source_split="test", exclude_runs=(prior,), max_images=1)
    assert not out.exists()


def test_legacy_exclusion_uses_verified_source_profile_and_original_url(tmp_path):
    prior = _prior_run(tmp_path)
    inventory_path = prior / "inventory.json"
    inventory = json.loads(inventory_path.read_text())
    del inventory["images"][0]["author_profile_url"]
    del inventory["images"][0]["original_url"]
    (prior / "source").mkdir()
    data = ("ImageID,Author,AuthorProfileURL,OriginalLandingURL,OriginalURL\n"
            f"{ID_A},Prior,https://example.org/author/Prior,https://example.org/{ID_A},https://example.org/raw/{ID_A}\n").encode()
    (prior / "source" / "images.csv").write_bytes(data)
    inventory["source_files"] = {"images": {"sha256": hashlib.sha256(data).hexdigest()}}
    inventory_path.write_text(json.dumps(inventory))
    excluded, records = collector._excluded_runs((prior,))
    assert "https://example.org/author/prior" in excluded["author_profile_url"]
    assert f"https://example.org/raw/{ID_A}" in excluded["original_url"]
    assert records[0]["image_count"] == 1
    (prior / "source" / "images.csv").write_bytes(data + b"changed")
    with pytest.raises(ValueError, match="metadata is missing or changed"):
        collector._excluded_runs((prior,))


def test_exif_rotated_jpeg_is_skipped(tmp_path, monkeypatch):
    stream = io.BytesIO()
    image = Image.new("RGB", (8, 8), "blue")
    exif = image.getexif()
    exif[274] = 6
    image.save(stream, format="JPEG", exif=exif)
    _test_sources(monkeypatch, image_body=stream.getvalue())
    result = collector.collect(tmp_path / "expanded", source_split="test", max_images=2)
    assert result["collected"] == 0
    assert result["skipped"]["image_download_or_decode_failed"] == 2


def test_oversize_or_duplicate_exclusions_fail_before_output(tmp_path):
    prior = _prior_run(tmp_path)
    out = tmp_path / "expanded"
    with pytest.raises(ValueError, match="distinct"):
        collector.collect(out, exclude_runs=(prior, prior))
    assert not out.exists()
    with pytest.raises(ValueError, match="deadline"):
        collector.collect(out, deadline_seconds=0)
    assert not out.exists()
    with pytest.raises(ValueError, match="source split"):
        collector.collect(out, source_split="unknown")
    with pytest.raises(ValueError, match="outside exclusion runs"):
        collector.collect(prior / "new", exclude_runs=(prior,))
    git = tmp_path / "repo"
    git.mkdir()
    (git / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(ValueError, match="Git"):
        collector.collect(git / "output")


def test_duplicate_jpeg_in_new_run_is_counted_once(tmp_path, monkeypatch):
    _test_sources(monkeypatch)
    out = tmp_path / "new"
    result = collector.collect(out, source_split="test", max_images=2)
    assert result["collected"] == 1
    assert result["skipped"]["duplicate_image_sha256"] == 1
