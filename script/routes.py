from __future__ import annotations

from io import BytesIO
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request, send_file

from script.metadata import ALLOWED_LOG_LEVELS, CameraSummary, DeviceLogRecord, SnapshotRecord
from script.retention import apply_upload_retention
from script.storage import Storage

DEVICE_ID_MAX_LENGTH = 128

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


def _serialize_device_log(log: DeviceLogRecord) -> dict[str, object]:
    return {
        "id": log.id,
        "camera_id": log.camera_id,
        "level": log.level,
        "message": log.message,
        "logged_at": log.logged_at.isoformat(),
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

    if len(device_id) > DEVICE_ID_MAX_LENGTH:
        current_app.logger.error("device_id too long: %d chars", len(device_id))
        return jsonify({"message": "device_id is too long"}), 400

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


@server.route("/api/cameras/<camera_id>", methods=["DELETE"])
def api_delete_camera(camera_id: str):
    delete_files = request.args.get("delete_files", "false").lower() == "true"
    deleted = _metadata_store().delete_camera(camera_id)
    if not deleted:
        return jsonify({"message": "Camera not found"}), 404

    if delete_files:
        try:
            _storage().delete_camera_files(camera_id)
        except Exception as exc:  # pragma: no cover
            current_app.logger.warning("Failed to delete storage for camera %s: %s", camera_id, exc)

    return jsonify({"message": "Camera deleted", "camera_id": camera_id}), 200


@server.route("/api/cameras/<camera_id>", methods=["PATCH"])
def api_patch_camera(camera_id: str):
    body = request.get_json(silent=True) or {}
    display_name = body.get("display_name")
    expected_interval_seconds = body.get("expected_interval_seconds")

    if display_name is not None and (not isinstance(display_name, str) or not display_name.strip()):
        return jsonify({"message": "display_name must be a non-empty string"}), 400
    if expected_interval_seconds is not None:
        is_valid_int = isinstance(expected_interval_seconds, int) and not isinstance(expected_interval_seconds, bool)
        if not is_valid_int or expected_interval_seconds <= 0:
            return jsonify({"message": "expected_interval_seconds must be a positive integer"}), 400

    camera = _metadata_store().update_camera(
        camera_id,
        display_name=display_name.strip() if display_name is not None else None,
        expected_interval_seconds=expected_interval_seconds,
    )
    if camera is None:
        return jsonify({"message": "Camera not found"}), 404
    return jsonify(
        {
            "camera_id": camera.camera_id,
            "display_name": camera.display_name,
            "expected_interval_seconds": camera.expected_interval_seconds,
        }
    ), 200


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


@server.route("/api/status_update", methods=["POST"])
def api_status_update():
    body = request.get_json(silent=True) or {}
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"message": "device_id is required"}), 400
    if len(device_id) > DEVICE_ID_MAX_LENGTH:
        return jsonify({"message": "device_id is too long"}), 400

    camera = _metadata_store().record_heartbeat(device_id)
    current_app.logger.info("Heartbeat from device: %s", device_id)
    return jsonify(
        {
            "message": "Status updated",
            "camera_id": camera.camera_id,
            "last_seen_at": camera.last_seen_at.isoformat(),
        }
    ), 200


@server.route("/api/log_error", methods=["POST"])
def api_log_error():
    body = request.get_json(silent=True) or {}
    device_id = (body.get("device_id") or "").strip()
    level = (body.get("level") or "error").strip().lower()
    message = (body.get("message") or "").strip()

    if not device_id:
        return jsonify({"message": "device_id is required"}), 400
    if len(device_id) > DEVICE_ID_MAX_LENGTH:
        return jsonify({"message": "device_id is too long"}), 400
    if not message:
        return jsonify({"message": "message is required"}), 400
    if len(message) > 2000:
        return jsonify({"message": "message is too long"}), 400
    if level not in ALLOWED_LOG_LEVELS:
        return jsonify({"message": f"level must be one of {sorted(ALLOWED_LOG_LEVELS)}"}), 400

    log = _metadata_store().record_device_log(device_id, level=level, message=message)
    current_app.logger.info("Device log from %s [%s]: %.200s", device_id, level, message)
    return jsonify(_serialize_device_log(log)), 201


@server.route("/api/cameras/<camera_id>/logs")
def api_camera_logs(camera_id: str):
    camera = _metadata_store().get_camera(camera_id)
    if camera is None:
        return jsonify({"message": "Camera not found"}), 404

    logs = _metadata_store().list_device_logs(camera_id)
    return jsonify({"camera_id": camera_id, "logs": [_serialize_device_log(log) for log in logs]})
