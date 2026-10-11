from __future__ import annotations

import io
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
        collector.collect(out, max_images=301)
    assert not out.exists()
    with pytest.raises(ValueError, match="per-image byte limit"):
        collector.collect(out, max_image_bytes=2_000_001)
    with pytest.raises(ValueError, match="total byte limit"):
        collector.collect(out, max_total_bytes=200_000_001)
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
