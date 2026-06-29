from __future__ import annotations

from queue import Empty
from unittest.mock import MagicMock, patch

from tests.conftest import MINIMAL_JPEG

UPLOAD_HEADERS = {
    "Content-Type": "image/jpeg",
    "X-Device-Id": "esp32cam-front-door",
    "X-Captured-At": "2026-03-11T12:34:56Z",
}


def _mock_queue(frames=()):
    mock_q = MagicMock()
    mock_q.get.side_effect = list(frames) + [Empty()]
    mock_q.get_nowait.side_effect = Empty()
    mock_q.put_nowait.return_value = None
    return mock_q


def test_mjpeg_stream_invalid_camera_returns_404(client):
    response = client.get("/stream/cameras/invalid!camera")
    assert response.status_code == 404
    assert response.get_json()["message"] == "Camera not found"


def test_mjpeg_stream_valid_camera_returns_200_multipart(client):
    with patch("script.routes.Queue", return_value=_mock_queue()):
        response = client.get("/stream/cameras/cam1")
    assert response.status_code == 200
    assert "multipart/x-mixed-replace" in response.content_type
    assert "boundary=frame" in response.content_type


def test_mjpeg_stream_includes_initial_frame(app):
    with app.test_client() as c:
        c.post("/upload", data=MINIMAL_JPEG, headers=UPLOAD_HEADERS)

    with app.test_client() as c:
        with patch("script.routes.Queue", return_value=_mock_queue()):
            response = c.get("/stream/cameras/esp32cam-front-door")

    assert response.status_code == 200
    assert b"--frame" in response.data
    assert b"Content-Type: image/jpeg" in response.data


def test_mjpeg_stream_yields_new_frame_on_event(client):
    with patch("script.routes.Queue", return_value=_mock_queue(frames=[MINIMAL_JPEG])):
        response = client.get("/stream/cameras/cam1")

    assert response.status_code == 200
    assert b"--frame" in response.data
    assert MINIMAL_JPEG in response.data


def test_mjpeg_stream_too_many_returns_503(app):
    app.config["MAX_MJPEG_STREAMS"] = 0
    try:
        with app.test_client() as c:
            response = c.get("/stream/cameras/cam1")
        assert response.status_code == 503
        assert response.get_json()["message"] == "Too many active streams"
    finally:
        del app.config["MAX_MJPEG_STREAMS"]
