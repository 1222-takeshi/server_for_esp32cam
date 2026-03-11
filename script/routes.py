from __future__ import annotations

from io import BytesIO

from flask import Blueprint, current_app, jsonify, render_template, request, send_file

from script.storage import Storage

server = Blueprint("server", __name__)


def _storage() -> Storage:
    return current_app.extensions["storage"]


def _looks_like_jpeg(data: bytes) -> bool:
    return len(data) >= 4 and data.startswith(b"\xff\xd8")


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

    try:
        stored = _storage().save_image(request.data, request.mimetype)
        current_app.logger.info("File uploaded successfully: %s", stored.name)
        return (
            jsonify(
                {
                    "message": "File uploaded successfully",
                    "filename": stored.name,
                    "size": stored.size,
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
