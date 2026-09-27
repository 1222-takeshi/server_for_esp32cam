from __future__ import annotations

from uuid import uuid4

from script.app import create_app


def _minimal_mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00\x00\x00\x08mdat"


DEVICE_KEYS = {"cam-a": "key-a", "cam-b": "key-b"}


def _app(tmp_path, **overrides):
    config = {
        "TESTING": True,
        "UPLOAD_DIR": tmp_path / f"uploads-{uuid4().hex}",
        "DB_PATH": tmp_path / f"esp32cam-{uuid4().hex}.sqlite3",
        "INFERENCE_MODE": "sync",
        "RATELIMIT_ENABLED": False,
        "DEVICE_API_KEYS": DEVICE_KEYS.copy(),
        "MAX_CONTENT_LENGTH": 10 * 1024 * 1024,
        "MAX_VIDEO_CONTENT_LENGTH": 1024 * 1024,
    }
    config.update(overrides)
    return create_app(config)


def _upload_headers(camera_id: str = "cam-a", key: str = "key-a") -> dict[str, str]:
    return {"Content-Type": "video/mp4", "X-API-Key": key, "X-Captured-At": "2026-03-11T12:34:56Z"}


def _upload_video(client, camera_id: str = "cam-a", key: str = "key-a") -> dict:
    resp = client.post(
        f"/api/cameras/{camera_id}/videos",
        data=_minimal_mp4(),
        headers=_upload_headers(camera_id, key),
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["video"]


def test_list_camera_videos_returns_videos(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    v = _upload_video(client)

    resp = client.get("/api/cameras/cam-a/videos")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["camera_id"] == "cam-a"
    assert len(body["videos"]) == 1
    assert body["videos"][0]["id"] == v["id"]


def test_list_camera_videos_limit(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    for _ in range(3):
        _upload_video(client)

    resp = client.get("/api/cameras/cam-a/videos?limit=2")

    assert resp.status_code == 200
    assert len(resp.get_json()["videos"]) == 2


def test_list_camera_videos_before_cursor(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    v1 = _upload_video(client)
    v2 = _upload_video(client)
    v3 = _upload_video(client)

    resp = client.get(f"/api/cameras/cam-a/videos?before={v3['id']}")

    assert resp.status_code == 200
    ids = [v["id"] for v in resp.get_json()["videos"]]
    assert v3["id"] not in ids
    assert v2["id"] in ids
    assert v1["id"] in ids


def test_list_camera_videos_nonexistent_camera_returns_404(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    resp = client.get("/api/cameras/no-such-cam/videos")

    assert resp.status_code == 404
    assert resp.get_json()["message"] == "Camera not found"


def test_list_camera_videos_invalid_before_returns_400(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _upload_video(client)

    resp = client.get("/api/cameras/cam-a/videos?before=notanint")

    assert resp.status_code == 400
    assert "before" in resp.get_json()["message"].lower()


def test_list_videos_global_returns_all(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _upload_video(client, camera_id="cam-a", key="key-a")
    _upload_video(client, camera_id="cam-b", key="key-b")

    resp = client.get("/api/videos")

    assert resp.status_code == 200
    assert len(resp.get_json()["videos"]) == 2


def test_list_videos_filter_by_camera_id(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _upload_video(client, camera_id="cam-a", key="key-a")
    _upload_video(client, camera_id="cam-b", key="key-b")

    resp = client.get("/api/videos?camera_id=cam-a")

    assert resp.status_code == 200
    videos = resp.get_json()["videos"]
    assert len(videos) == 1
    assert videos[0]["camera_id"] == "cam-a"


def test_video_content_returns_mp4(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    v = _upload_video(client)

    resp = client.get(f"/api/cameras/cam-a/videos/{v['id']}/content")

    assert resp.status_code == 200
    assert resp.content_type == "video/mp4"
    assert resp.data == _minimal_mp4()


def test_video_content_supports_range_requests(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    v = _upload_video(client)

    resp = client.get(
        f"/api/cameras/cam-a/videos/{v['id']}/content",
        headers={"Range": "bytes=0-3"},
    )

    assert resp.status_code == 206
    assert resp.data == _minimal_mp4()[:4]
    assert resp.content_range.units == "bytes"


def test_video_content_wrong_camera_returns_404(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    v_a = _upload_video(client, camera_id="cam-a", key="key-a")
    _upload_video(client, camera_id="cam-b", key="key-b")

    resp = client.get(f"/api/cameras/cam-b/videos/{v_a['id']}/content")

    assert resp.status_code == 404
    assert resp.get_json()["message"] == "Video not found"


def test_video_content_nonexistent_camera_returns_404(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()

    resp = client.get("/api/cameras/no-such-cam/videos/1/content")

    assert resp.status_code == 404
    assert resp.get_json()["message"] == "Camera not found"


def test_storage_read_video_path_traversal_raises(tmp_path):
    app = _app(tmp_path)
    storage = app.extensions["storage"]

    try:
        storage.read_video_path_by_relative_path("../../etc/passwd")
        raise AssertionError("should raise FileNotFoundError")
    except FileNotFoundError:
        pass


def test_storage_read_video_absolute_path_raises(tmp_path):
    app = _app(tmp_path)
    storage = app.extensions["storage"]

    try:
        storage.read_video_path_by_relative_path("/etc/passwd")
        raise AssertionError("should raise FileNotFoundError")
    except FileNotFoundError:
        pass
