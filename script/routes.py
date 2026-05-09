from __future__ import annotations

from io import BytesIO
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request, send_file

from script.metadata import CameraSummary, SnapshotRecord
from script.retention import apply_upload_retention
from script.storage import Storage

server = Blueprint("server", __name__)


def _storage() -> Storage:
    return current_app.extensions["storage"]


def _metadata_store():
    return current_app.extensions["metadata_store"]


def _inference_service():
    return current_app.extensions["inference_service"]


def _snapshot_event_publisher():
    return current_app.extensions["snapshot_event_publisher"]


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


def _serialize_detection(detection) -> dict[str, object]:
    return {
        "label": detection.label,
        "score": round(detection.score, 4),
        "bbox": [detection.x1, detection.y1, detection.x2, detection.y2],
    }


def _serialize_snapshot(snapshot: SnapshotRecord) -> dict[str, object]:
    return {
        "id": snapshot.id,
        "filename": snapshot.filename,
        "relative_path": snapshot.relative_path,
        "size": snapshot.size,
        "camera_id": snapshot.camera_id,
        "captured_at": snapshot.captured_at.isoformat() if snapshot.captured_at else None,
        "stored_at": snapshot.stored_at.isoformat(),
        "inference_status": snapshot.inference_status,
        "inference_error": snapshot.inference_error,
        "detections": [_serialize_detection(detection) for detection in snapshot.detections],
    }


def _serialize_camera_summary(summary: CameraSummary) -> dict[str, object]:
    return {
        "camera_id": summary.camera.camera_id,
        "display_name": summary.camera.display_name,
        "expected_interval_seconds": summary.camera.expected_interval_seconds,
        "first_seen_at": summary.camera.first_seen_at.isoformat(),
        "last_seen_at": summary.camera.last_seen_at.isoformat(),
        "status": summary.status,
        "latest_snapshot": _serialize_snapshot(summary.latest_snapshot) if summary.latest_snapshot else None,
        "latest_detection_at": (
            summary.latest_detection_at.isoformat() if summary.latest_detection_at else None
        ),
    }


@server.route("/")
def index():
    camera_summaries = _metadata_store().list_camera_summaries()
    return render_template(
        "index.html",
        camera_summaries=camera_summaries,
        image_count=_metadata_store().total_snapshot_count(),
    )


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
        stored = _storage().save_image(
            request.data,
            request.mimetype,
            device_id=device_id,
            captured_at=captured_at,
        )
        snapshot = _metadata_store().record_snapshot(stored)
        _inference_service().enqueue_snapshot(snapshot.id)
        _snapshot_event_publisher().publish_snapshot_created(snapshot)
        apply_upload_retention(stored)
        current_app.logger.info("File uploaded successfully: %s", stored.name)
        return (
            jsonify(
                {
                    "message": "File uploaded successfully",
                    "filename": stored.name,
                    "size": stored.size,
                    "device_id": stored.device_id,
                    "captured_at": stored.captured_at.isoformat() if stored.captured_at else None,
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
        files = _metadata_store().list_recent_snapshots()
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


@server.route("/cameras")
def list_cameras():
    camera_summaries = _metadata_store().list_camera_summaries()
    return render_template("cameras.html", camera_summaries=camera_summaries)


@server.route("/cameras/<camera_id>")
def camera_detail(camera_id: str):
    camera = _metadata_store().get_camera(camera_id)
    if camera is None:
        return jsonify({"message": "Camera not found"}), 404

    snapshots = _metadata_store().list_camera_snapshots(camera_id, limit=20)
    latest = snapshots[0] if snapshots else None
    return render_template(
        "camera_detail.html",
        camera=camera,
        snapshots=snapshots,
        latest=latest,
    )


@server.route("/api/cameras")
def api_cameras():
    return jsonify({"cameras": [_serialize_camera_summary(item) for item in _metadata_store().list_camera_summaries()]})


@server.route("/api/cameras/<camera_id>/upload-metrics")
def api_camera_upload_metrics(camera_id: str):
    return jsonify(current_app.extensions["upload_metrics"].snapshot(camera_id))


@server.route("/api/cameras/<camera_id>/snapshots")
def api_camera_snapshots(camera_id: str):
    camera = _metadata_store().get_camera(camera_id)
    if camera is None:
        return jsonify({"message": "Camera not found"}), 404
    return jsonify(
        {
            "camera_id": camera_id,
            "snapshots": [
                _serialize_snapshot(snapshot)
                for snapshot in _metadata_store().list_camera_snapshots(camera_id, limit=50)
            ],
        }
    )


@server.route("/api/detections")
def api_detections():
    camera_id = request.args.get("camera_id")
    since_raw = request.args.get("since")
    since = None
    if since_raw:
        try:
            since = _parse_captured_at(since_raw)
        except ValueError:
            return jsonify({"message": "Invalid since parameter"}), 400

    detections = _metadata_store().list_detections(camera_id=camera_id, since=since)
    return jsonify({"detections": detections})
