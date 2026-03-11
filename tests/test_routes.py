from __future__ import annotations

import time


def test_healthz_returns_ok(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_upload_accepts_jpeg_and_returns_metadata(client):
    payload = b"\xff\xd8\xff\xe0fake-jpeg"

    response = client.post(
        "/upload",
        data=payload,
        headers={"Content-Type": "image/jpeg"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"] == "File uploaded successfully"
    assert body["filename"].endswith(".jpg")
    assert body["size"] == len(payload)
    assert body["stored_at"]


def test_upload_rejects_empty_payload(client):
    response = client.post("/upload", data=b"", headers={"Content-Type": "image/jpeg"})

    assert response.status_code == 400
    assert response.get_json()["message"] == "No data received"


def test_upload_rejects_unsupported_content_type(client):
    response = client.post("/upload", data=b"png", headers={"Content-Type": "image/png"})

    assert response.status_code == 415
    assert response.get_json()["message"] == "Unsupported content type"


def test_upload_rejects_invalid_jpeg_payload(client):
    response = client.post("/upload", data=b"not-jpeg", headers={"Content-Type": "image/jpeg"})

    assert response.status_code == 400
    assert response.get_json()["message"] == "Invalid JPEG payload"


def test_files_page_lists_latest_upload_first(client):
    first = client.post(
        "/upload",
        data=b"\xff\xd8first-image",
        headers={"Content-Type": "image/jpeg"},
    ).get_json()
    time.sleep(0.01)
    second = client.post(
        "/upload",
        data=b"\xff\xd8second-image",
        headers={"Content-Type": "image/jpeg"},
    ).get_json()

    response = client.get("/files")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert second["filename"] in page
    assert first["filename"] in page
    assert page.index(second["filename"]) < page.index(first["filename"])


def test_get_file_returns_uploaded_image_bytes(client):
    upload = client.post(
        "/upload",
        data=b"\xff\xd8payload",
        headers={"Content-Type": "image/jpeg"},
    ).get_json()

    response = client.get(f"/files/{upload['filename']}")

    assert response.status_code == 200
    assert response.data == b"\xff\xd8payload"
    assert response.mimetype == "image/jpeg"


def test_get_file_returns_404_for_missing_file(client):
    response = client.get("/files/missing.jpg")

    assert response.status_code == 404
    assert response.get_json()["message"] == "File not found"


def test_upload_returns_500_when_storage_write_fails(app, client, monkeypatch):
    def fail_save_image(_data, _content_type):
        raise OSError("disk full")

    monkeypatch.setattr(app.extensions["storage"], "save_image", fail_save_image)

    response = client.post("/upload", data=b"\xff\xd8broken", headers={"Content-Type": "image/jpeg"})

    assert response.status_code == 500
    assert response.get_json()["message"] == "File upload failed"


def test_upload_returns_413_for_large_payload(app, client):
    app.config["MAX_CONTENT_LENGTH"] = 8

    response = client.post(
        "/upload",
        data=b"\xff\xd8toolarge",
        headers={"Content-Type": "image/jpeg"},
    )

    assert response.status_code == 413
    assert response.get_json()["message"] == "Payload too large"
