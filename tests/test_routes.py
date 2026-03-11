from __future__ import annotations

from datetime import datetime
import time

UPLOAD_HEADERS = {
    "Content-Type": "image/jpeg",
    "X-Device-Id": "esp32cam-front-door",
    "X-Captured-At": "2026-03-11T12:34:56Z",
}


def test_healthz_returns_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_upload_accepts_jpeg_and_returns_metadata(client):
    payload = b"\xff\xd8\xff\xe0fake-jpeg"

    response = client.post(
        "/upload",
        data=payload,
        headers=UPLOAD_HEADERS,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"] == "File uploaded successfully"
    assert body["filename"].endswith(".jpg")
    assert body["size"] == len(payload)
    assert body["device_id"] == "esp32cam-front-door"
    assert body["captured_at"] == "2026-03-11T12:34:56+00:00"
    assert datetime.fromisoformat(body["stored_at"]).tzinfo is not None


def test_upload_rejects_empty_payload(client):
    response = client.post("/upload", data=b"", headers=UPLOAD_HEADERS)

    assert response.status_code == 400
    assert response.get_json()["message"] == "No data received"


def test_upload_rejects_unsupported_content_type(client):
    response = client.post(
        "/upload",
        data=b"png",
        headers={
            "Content-Type": "image/png",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    )

    assert response.status_code == 415
    assert response.get_json()["message"] == "Unsupported content type"


def test_upload_rejects_invalid_jpeg_payload(client):
    response = client.post("/upload", data=b"not-jpeg", headers=UPLOAD_HEADERS)

    assert response.status_code == 400
    assert response.get_json()["message"] == "Invalid JPEG payload"


def test_upload_rejects_missing_device_metadata(client):
    response = client.post(
        "/upload",
        data=b"\xff\xd8jpeg",
        headers={"Content-Type": "image/jpeg"},
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "Missing device metadata"


def test_upload_rejects_invalid_captured_at_metadata(client):
    response = client.post(
        "/upload",
        data=b"\xff\xd8jpeg",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "not-a-timestamp",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "Invalid captured_at metadata"


def test_upload_rejects_timezone_naive_captured_at_metadata(client):
    response = client.post(
        "/upload",
        data=b"\xff\xd8jpeg",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "2026-03-11T12:34:56",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "Invalid captured_at metadata"


def test_upload_normalizes_non_utc_captured_at_metadata_to_utc(client):
    response = client.post(
        "/upload",
        data=b"\xff\xd8jpeg",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "2026-03-11T21:34:56+09:00",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["captured_at"] == "2026-03-11T12:34:56+00:00"


def test_files_page_lists_latest_upload_first(client):
    first = client.post(
        "/upload",
        data=b"\xff\xd8first-image",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-front-door",
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    ).get_json()
    time.sleep(0.01)
    second = client.post(
        "/upload",
        data=b"\xff\xd8second-image",
        headers={
            "Content-Type": "image/jpeg",
            "X-Device-Id": "esp32cam-garden",
            "X-Captured-At": "2026-03-11T12:35:56Z",
        },
    ).get_json()

    response = client.get("/files")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert second["filename"] in page
    assert first["filename"] in page
    assert "esp32cam-garden" in page
    assert "2026-03-11T12:35:56+00:00" in page
    assert page.index(second["filename"]) < page.index(first["filename"])


def test_index_page_shows_latest_upload_metadata(client):
    client.post(
        "/upload",
        data=b"\xff\xd8payload",
        headers=UPLOAD_HEADERS,
    )

    response = client.get("/")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "esp32cam-front-door" in page
    assert "2026-03-11T12:34:56+00:00" in page


def test_get_file_returns_uploaded_image_bytes(client):
    upload = client.post(
        "/upload",
        data=b"\xff\xd8payload",
        headers=UPLOAD_HEADERS,
    ).get_json()

    response = client.get(f"/files/{upload['filename']}")

    assert response.status_code == 200
    assert response.data == b"\xff\xd8payload"
    assert response.mimetype == "image/jpeg"


def test_get_file_rejects_metadata_sidecar_requests(client):
    upload = client.post(
        "/upload",
        data=b"\xff\xd8payload",
        headers=UPLOAD_HEADERS,
    ).get_json()

    response = client.get(f"/files/{upload['filename']}.json")

    assert response.status_code == 404
    assert response.get_json()["message"] == "File not found"


def test_get_file_returns_404_for_missing_file(client):
    response = client.get("/files/missing.jpg")

    assert response.status_code == 404
    assert response.get_json()["message"] == "File not found"


def test_upload_returns_500_when_storage_write_fails(app, client, monkeypatch):
    def fail_save_image(_data, _content_type, *, device_id, captured_at):
        raise OSError("disk full")

    monkeypatch.setattr(app.extensions["storage"], "save_image", fail_save_image)

    response = client.post("/upload", data=b"\xff\xd8broken", headers=UPLOAD_HEADERS)

    assert response.status_code == 500
    assert response.get_json()["message"] == "File upload failed"


def test_upload_returns_413_for_large_payload(app, client):
    app.config["MAX_CONTENT_LENGTH"] = 8

    response = client.post(
        "/upload",
        data=b"\xff\xd8toolarge",
        headers=UPLOAD_HEADERS,
    )

    assert response.status_code == 413
    assert response.get_json()["message"] == "Payload too large"
