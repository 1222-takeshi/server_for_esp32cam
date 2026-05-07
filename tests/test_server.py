from __future__ import annotations


UPLOAD_HEADERS = {
    "Content-Type": "image/jpeg",
    "X-Device-Id": "esp32cam-smoke-test",
    "X-Captured-At": "2026-03-11T12:34:56Z",
}


def test_server_entrypoint_app_serves_index(client):
    response = client.get("/")

    assert response.status_code == 200


def test_upload_requires_request_body(client):
    response = client.post("/upload", data=b"", headers=UPLOAD_HEADERS)

    assert response.status_code == 400
    assert response.get_json()["message"] == "No data received"


def test_upload_rejects_non_jpeg_content_type(client):
    response = client.post(
        "/upload",
        data=b"not an image",
        headers={
            "Content-Type": "text/plain",
            "X-Device-Id": "esp32cam-smoke-test",
            "X-Captured-At": "2026-03-11T12:34:56Z",
        },
    )

    assert response.status_code == 415
    assert response.get_json()["message"] == "Unsupported content type"


def test_upload_accepts_raw_jpeg_payload(client):
    payload = b"\xff\xd8\xff\xe0fake-jpeg-data"

    response = client.post("/upload", data=payload, headers=UPLOAD_HEADERS)

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"] == "File uploaded successfully"
    assert body["device_id"] == "esp32cam-smoke-test"
    assert body["size"] == len(payload)
    assert body["filename"].endswith(".jpg")
