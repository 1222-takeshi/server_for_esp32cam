from io import BytesIO

from script import server


def test_index_returns_success(client):
    response = client.get("/")

    assert response.status_code == 200


def test_upload_requires_image_part(client):
    response = client.post("/upload", data={})

    assert response.status_code == 400
    assert response.get_json()["message"] == "No image file part"


def test_upload_rejects_invalid_extension(client):
    response = client.post(
        "/upload",
        data={"image": (BytesIO(b"not an image"), "sample.txt")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "Invalid file type"


def test_upload_saves_timestamped_and_latest_files(client, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "UPLOAD_FOLDER", str(tmp_path))
    server.app.config["UPLOAD_FOLDER"] = str(tmp_path)

    response = client.post(
        "/upload",
        data={"image": (BytesIO(b"fake-jpeg-data"), "sample.jpg")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.get_json()["message"] == "File uploaded successfully"
    assert (tmp_path / "latest.jpg").read_bytes() == b"fake-jpeg-data"
    timestamped_files = [path for path in tmp_path.iterdir() if path.name != "latest.jpg"]
    assert len(timestamped_files) == 1
    assert timestamped_files[0].name.startswith("image_")
    assert timestamped_files[0].suffix == ".jpg"
