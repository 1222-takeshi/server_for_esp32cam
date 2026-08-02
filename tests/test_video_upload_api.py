from __future__ import annotations

from io import BytesIO
from uuid import uuid4

from flask import request

from script.app import create_app


def _minimal_mp4() -> bytes:
    # ISO BMFF: size(24) + ftyp box + minimal mdat box. Storage only needs the
    # ftyp signature; this keeps route tests small and deterministic.
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00\x00\x00\x08mdat"


DEVICE_KEYS = {"cam-a": "key-a", "cam-b": "key-b"}


def _video_app(tmp_path, **overrides):
    config = {
        "TESTING": True,
        "UPLOAD_DIR": tmp_path / f"uploads-{uuid4().hex}",
        "DB_PATH": tmp_path / f"esp32cam-{uuid4().hex}.sqlite3",
        "INFERENCE_MODE": "sync",
        "RATELIMIT_ENABLED": False,
        "DEVICE_API_KEYS": DEVICE_KEYS.copy(),
        "MAX_CONTENT_LENGTH": 10 * 1024 * 1024,
        "MAX_VIDEO_CONTENT_LENGTH": 1024,
    }
    config.update(overrides)
    return create_app(config)


def _headers(camera_id: str = "cam-a", key: str = "key-a") -> dict[str, str]:
    return {"Content-Type": "video/mp4", "X-API-Key": key, "X-Captured-At": "2026-03-11T12:34:56Z"}


def test_video_upload_raw_mp4_returns_201_and_records_metadata(tmp_path):
    app = _video_app(tmp_path)
    client = app.test_client()
    payload = _minimal_mp4()

    response = client.post("/api/cameras/cam-a/videos", data=payload, headers=_headers())

    assert response.status_code == 201
    body = response.get_json()
    assert body["message"] == "Video uploaded successfully"
    video = body["video"]
    assert isinstance(video["id"], int)
    assert video["filename"].endswith(".mp4")
    assert video["relative_path"].startswith("cam-a/videos/")
    assert video["size"] == len(payload)
    assert video["camera_id"] == "cam-a"
    assert video["captured_at"] == "2026-03-11T12:34:56+00:00"
    assert video["duration_ms"] is None
    assert video["content_type"] == "video/mp4"
    assert len(video["sha256"]) == 64
    assert video["processing_status"] == "skipped"

    stored = app.extensions["metadata_store"].get_video(video["id"], camera_id="cam-a")
    assert stored is not None
    assert stored.relative_path == video["relative_path"]


def test_video_upload_can_exceed_legacy_snapshot_limit_when_within_video_limit(tmp_path):
    app = _video_app(tmp_path, MAX_CONTENT_LENGTH=64, MAX_VIDEO_CONTENT_LENGTH=256)
    client = app.test_client()
    payload = _minimal_mp4() + (b"\x00" * 96)

    response = client.post("/api/cameras/cam-a/videos", data=payload, headers=_headers())

    assert response.status_code == 201
    assert response.get_json()["video"]["size"] == len(payload)


def test_video_upload_removes_saved_file_when_metadata_recording_fails(tmp_path, monkeypatch):
    app = _video_app(tmp_path)
    client = app.test_client()

    def fail_record_video(_stored):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(app.extensions["metadata_store"], "record_video", fail_record_video)

    response = client.post("/api/cameras/cam-a/videos", data=_minimal_mp4(), headers=_headers())

    assert response.status_code == 500
    assert response.get_json()["message"] == "Video upload failed"
    assert list((app.config["UPLOAD_DIR"] / "cam-a" / "videos").glob("*.mp4")) == []
    assert list((app.config["UPLOAD_DIR"] / "cam-a" / "videos").glob("*.json")) == []


def test_video_upload_multipart_accepts_file_and_metadata_fields(tmp_path):
    app = _video_app(tmp_path)
    client = app.test_client()
    payload = _minimal_mp4()

    response = client.post(
        "/api/cameras/cam-a/videos",
        data={
            "file": (BytesIO(payload), "client-name.mp4"),
            "captured_at": "2026-03-11T12:34:56Z",
            "duration_ms": "1234",
        },
        headers={"X-API-Key": "key-a"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    video = response.get_json()["video"]
    assert video["duration_ms"] == 1234
    assert video["filename"] != "client-name.mp4"
    assert video["relative_path"].startswith("cam-a/videos/")


def test_video_upload_missing_api_key_returns_401(tmp_path):
    app = _video_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/cameras/cam-a/videos",
        data=_minimal_mp4(),
        headers={"Content-Type": "video/mp4"},
    )

    assert response.status_code == 401
    assert response.get_json()["message"] == "Unauthorized"


def test_video_upload_wrong_camera_key_returns_403(tmp_path):
    app = _video_app(tmp_path)
    client = app.test_client()

    response = client.post("/api/cameras/cam-a/videos", data=_minimal_mp4(), headers=_headers(key="key-b"))

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden"


def test_video_upload_fails_closed_when_per_camera_key_is_unset(tmp_path):
    app = _video_app(tmp_path, DEVICE_API_KEYS={"cam-b": "key-b"})
    client = app.test_client()

    response = client.post("/api/cameras/cam-a/videos", data=_minimal_mp4(), headers=_headers())

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden"


def test_video_upload_rejects_invalid_camera_id(tmp_path):
    app = _video_app(tmp_path)
    client = app.test_client()

    response = client.post("/api/cameras/cam;drop/videos", data=_minimal_mp4(), headers=_headers())

    assert response.status_code == 404
    assert response.get_json()["message"] == "Camera not found"


def test_video_upload_accepts_camera_id_between_64_and_128_chars(tmp_path):
    # Regression test for #255: camera_id validation (script/validation.py,
    # shared with device_id validation in routes.py) used to cap at 64 chars
    # while device_id-keyed endpoints (e.g. /upload, /api/status_update)
    # accepted up to 128. A device_id in the 65-128 range could therefore
    # authenticate/upload snapshots and appear in listings, yet be reported
    # as "Camera not found" by every camera_id-keyed endpoint, including
    # this one. Both limits are now unified at 128.
    long_camera_id = "x" * 100
    app = _video_app(tmp_path, DEVICE_API_KEYS={long_camera_id: "key-long"})
    client = app.test_client()

    response = client.post(
        f"/api/cameras/{long_camera_id}/videos",
        data=_minimal_mp4(),
        headers=_headers(long_camera_id, "key-long"),
    )

    assert response.status_code == 201, response.get_json()
    assert response.get_json()["video"]["camera_id"] == long_camera_id


def test_video_upload_still_rejects_camera_id_over_128_chars(tmp_path):
    too_long_camera_id = "x" * 129
    app = _video_app(tmp_path, DEVICE_API_KEYS={too_long_camera_id: "key-long"})
    client = app.test_client()

    response = client.post(
        f"/api/cameras/{too_long_camera_id}/videos",
        data=_minimal_mp4(),
        headers=_headers(too_long_camera_id, "key-long"),
    )

    assert response.status_code == 404
    assert response.get_json()["message"] == "Camera not found"


def test_video_upload_rejects_payload_over_video_limit(tmp_path):
    app = _video_app(tmp_path, MAX_VIDEO_CONTENT_LENGTH=16)
    client = app.test_client()

    response = client.post("/api/cameras/cam-a/videos", data=_minimal_mp4(), headers=_headers())

    assert response.status_code == 413
    assert response.get_json()["message"] == "Payload too large"


def test_video_upload_sets_video_limit_before_body_read(tmp_path):
    app = _video_app(tmp_path, MAX_CONTENT_LENGTH=999, MAX_VIDEO_CONTENT_LENGTH=256)
    observed = {}

    @app.before_request
    def observe_request_limit():
        if request.endpoint == "server.api_upload_video":
            observed["max_content_length"] = request.max_content_length

    response = app.test_client().post(
        "/api/cameras/cam-a/videos",
        data=_minimal_mp4(),
        headers=_headers(),
    )

    assert response.status_code == 201
    assert observed["max_content_length"] == 257


def test_video_upload_rejects_oversized_terminated_stream_without_content_length(tmp_path):
    app = _video_app(tmp_path, MAX_VIDEO_CONTENT_LENGTH=64)

    response = app.test_client().open(
        "/api/cameras/cam-a/videos",
        method="POST",
        input_stream=BytesIO(_minimal_mp4() + (b"\x00" * 64)),
        headers=_headers(),
        environ_overrides={"CONTENT_LENGTH": None, "wsgi.input_terminated": True},
    )

    assert response.status_code == 413
    assert response.get_json()["message"] == "Payload too large"


def test_video_upload_rejects_empty_body(tmp_path):
    app = _video_app(tmp_path)
    client = app.test_client()

    response = client.post("/api/cameras/cam-a/videos", data=b"", headers=_headers())

    assert response.status_code == 400
    assert response.get_json()["message"] == "No data received"


def test_video_upload_rejects_unsupported_content_type(tmp_path):
    app = _video_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/cameras/cam-a/videos",
        data=_minimal_mp4(),
        headers={"Content-Type": "application/octet-stream", "X-API-Key": "key-a"},
    )

    assert response.status_code == 415
    assert response.get_json()["message"] == "Unsupported content type"


def test_video_upload_rejects_invalid_mp4_payload(tmp_path):
    app = _video_app(tmp_path)
    client = app.test_client()

    response = client.post("/api/cameras/cam-a/videos", data=b"not-an-mp4", headers=_headers())

    assert response.status_code == 415
    assert response.get_json()["message"] == "Unsupported video payload"


def test_video_upload_rejects_negative_duration(tmp_path):
    app = _video_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/api/cameras/cam-a/videos",
        data={"file": (BytesIO(_minimal_mp4()), "video.mp4"), "duration_ms": "-1"},
        headers={"X-API-Key": "key-a"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "duration_ms must be a non-negative integer"
