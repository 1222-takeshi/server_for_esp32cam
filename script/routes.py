from __future__ import annotations

from io import BytesIO
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request, send_file

from script.storage import Storage

server = Blueprint("server", __name__)


def _storage() -> Storage:
    return current_app.extensions["storage"]


def _looks_like_jpeg(data: bytes) -> bool:
    return len(data) >= 4 and data.startswith(b"\xff\xd8")


def _parse_captured_at(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("captured_at must include timezone")
    return parsed.astimezone(timezone.utc)


@server.route("/")
def index():
    images = _storage().list_images()
    latest = images[0] if images else None
    return render_template("index.html", latest=latest, image_count=len(images))


@server.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


@server.route("/upload", methods=["POST"])
def upload_file():
    if not request.data:
        current_app.logger.error("No data received")
        return jsonify({"message": "No data received"}), 400

    if request.mimetype != "image/jpeg":
        current_app.logger.error("Unsupported content type: %s", request.content_type)
        return jsonify({"message": "Unsupported content type"}), 415

    if not _looks_like_jpeg(request.data):
        current_app.logger.error("Invalid JPEG payload received")
        return jsonify({"message": "Invalid JPEG payload"}), 400

    device_id = request.headers.get("X-Device-Id", "").strip()
    captured_at_raw = request.headers.get("X-Captured-At", "").strip()

    if not device_id or not captured_at_raw:
        current_app.logger.error("Missing device metadata")
        return jsonify({"message": "Missing device metadata"}), 400

    try:
        captured_at = _parse_captured_at(captured_at_raw)
    except ValueError:
        current_app.logger.error("Invalid captured_at metadata: %s", captured_at_raw)
        return jsonify({"message": "Invalid captured_at metadata"}), 400

    try:
        stored = _storage().save_image(request.data, request.mimetype)
        current_app.logger.info("File uploaded successfully: %s", stored.name)
        return (
            jsonify(
                {
                    "message": "File uploaded successfully",
                    "filename": stored.name,
                    "size": stored.size,
                    "device_id": device_id,
                    "captured_at": captured_at.isoformat(),
                    "stored_at": stored.created_at.isoformat(),
                }
            ),
            200,
        )
    except Exception as exc:  # pragma: no cover
        current_app.logger.error("File upload failed: %s", exc)
        return jsonify({"message": "File upload failed", "error": str(exc)}), 500


@server.route("/files")
def list_files():
    try:
        files = _storage().list_images()
        return render_template("files.html", files=files)
    except Exception as exc:  # pragma: no cover
        current_app.logger.error("Error listing files: %s", exc)
        return jsonify({"message": "Error listing files", "error": str(exc)}), 500


@server.route("/files/<filename>")
def get_file(filename: str):
    try:
        return send_file(
            BytesIO(_storage().read_image(filename)),
            mimetype="image/jpeg",
            download_name=filename,
        )
    except FileNotFoundError:
        return jsonify({"message": "File not found"}), 404
    except Exception as exc:  # pragma: no cover
        current_app.logger.error("Error retrieving file: %s", exc)
        return jsonify({"message": "Error retrieving file", "error": str(exc)}), 500
