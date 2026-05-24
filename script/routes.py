from __future__ import annotations

import csv
from io import BytesIO, StringIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
import uuid

from flask import Blueprint, Response, current_app, jsonify, render_template, request, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from script.auth import check_admin_token, check_device_api_key, require_admin_auth, require_device_auth
from script.metadata import ALLOWED_LOG_LEVELS, CameraSummary, DeviceLogRecord, SnapshotRecord
from script.retention import apply_upload_retention
from script.storage import Storage

DEVICE_ID_MAX_LENGTH = 128
_DEVICE_ID_RE = re.compile(rf"[A-Za-z0-9_\-]{{1,{DEVICE_ID_MAX_LENGTH}}}")


def _upload_rate_limit_key() -> str:
    # Use the device API key as the rate limit identifier only when it is valid.
    # An unvalidated key would allow unauthenticated clients to exhaust another
    # device's rate limit bucket by spoofing its key header.
    api_key = request.headers.get("X-API-Key")
    if api_key and check_device_api_key(api_key):
        return api_key
    return get_remote_address()


server = Blueprint("server", __name__)
limiter = Limiter(key_func=_upload_rate_limit_key, default_limits=[])

_PAGINATION_LIMIT_MAX = 200
_PAGINATION_LIMIT_DEFAULT = 50
_PAGINATION_OFFSET_MAX = 10_000_000
_FILES_PAGE_MAX = _PAGINATION_OFFSET_MAX // _PAGINATION_LIMIT_DEFAULT
_EXPORT_ROW_LIMIT = 10_000


def _parse_pagination() -> tuple[int, int] | tuple[None, None]:
    """Returns (limit, offset) or (None, None) on error."""
    try:
        limit = int(request.args.get("limit", _PAGINATION_LIMIT_DEFAULT))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return None, None
    if limit < 1 or limit > _PAGINATION_LIMIT_MAX or offset < 0 or offset > _PAGINATION_OFFSET_MAX:
        return None, None
    return limit, offset


def _check_disk_usage() -> None:
    upload_dir = current_app.config["UPLOAD_DIR"]
    usage = shutil.disk_usage(upload_dir)
    pct = usage.used * 100 / usage.total
    if pct >= 95:
        current_app.logger.error(
            "Disk usage critical: %.1f%% used (%d bytes free)", pct, usage.free
        )
    elif pct >= 85:
        current_app.logger.warning(
            "Disk usage high: %.1f%% used (%d bytes free)", pct, usage.free
        )


def _storage() -> Storage:
    return current_app.extensions["storage"]


def _metadata_store():
    return current_app.extensions["metadata_store"]


def _inference_service():
    return current_app.extensions["inference_service"]


def _snapshot_event_publisher():
    return current_app.extensions["snapshot_event_publisher"]


def _validate_device_id(device_id: str) -> str | None:
    """Returns an error message string if invalid, None if valid."""
    if not device_id:
        return "device_id is required"
    if not _DEVICE_ID_RE.fullmatch(device_id):
        return "device_id contains invalid characters or exceeds maximum length"
    return None


def _validate_jpeg_image(data: bytes) -> str | None:
    """Returns None if the JPEG is valid, or an error message string if invalid."""
    from PIL import Image, UnidentifiedImageError  # lazy import — Pillow is optional at import time

    max_dim = current_app.config.get("UPLOAD_MAX_IMAGE_DIMENSION", 4096)
    try:
        with Image.open(BytesIO(data)) as img:
            if img.width > max_dim or img.height > max_dim:
                return f"Image dimensions exceed maximum ({img.width}x{img.height}, max {max_dim}px)"
    except (UnidentifiedImageError, SyntaxError, ValueError, OSError):
        return "Invalid or corrupted JPEG payload"
    except Exception:
        current_app.logger.warning("Unexpected error during JPEG validation")
        return "Invalid or corrupted JPEG payload"
    return None


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
    store = _metadata_store()
    camera_summaries = store.list_camera_summaries()
    one_hour_ago = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    cameras_with_errors = store.camera_ids_with_recent_errors(since=one_hour_ago)
    return render_template(
        "index.html",
        camera_summaries=camera_summaries,
        image_count=store.total_snapshot_count(),
        cameras_with_errors=cameras_with_errors,
    )


@server.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


@server.route("/healthz/ready")
def healthz_ready():
    checks: dict[str, object] = {}
    degraded = False

    try:
        _metadata_store().ping()
        checks["db"] = "ok"
    except Exception as exc:
        current_app.logger.error("healthz/ready: db check failed: %s", exc)
        checks["db"] = "error"
        degraded = True

    upload_dir = Path(current_app.config["UPLOAD_DIR"])
    try:
        probe = upload_dir / f".healthz-probe-{uuid.uuid4().hex}"
        probe.write_bytes(b"\x00")
        probe.read_bytes()
        probe.unlink()
        checks["storage"] = "ok"
    except Exception as exc:
        current_app.logger.error("healthz/ready: storage check failed: %s", exc)
        checks["storage"] = "error"
        degraded = True

    try:
        usage = shutil.disk_usage(upload_dir)
        checks["disk_free_mb"] = usage.free // (1024 * 1024)
        if usage.free / usage.total < 0.05:
            degraded = True
    except Exception as exc:
        checks["disk_free_mb"] = -1
        current_app.logger.warning("healthz/ready: disk_usage failed: %s", exc)
        degraded = True

    inference_svc = current_app.extensions.get("inference_service")
    if inference_svc is not None and inference_svc.mode == "async":
        alive = inference_svc.worker_is_alive()
        checks["inference_worker"] = "running" if alive else "stopped"
        if not alive:
            degraded = True
    else:
        checks["inference_worker"] = "running"

    status = "degraded" if degraded else "ready"
    return jsonify({"status": status, **checks}), 503 if degraded else 200


@server.route("/upload", methods=["POST"])
@limiter.limit(lambda: current_app.config.get("UPLOAD_RATE_LIMIT", "10 per minute"))
@require_device_auth
def upload_file():
    if not request.data:
        current_app.logger.error("No data received")
        return jsonify({"message": "No data received"}), 400

    if request.mimetype != "image/jpeg":
        current_app.logger.error("Unsupported content type: %s", request.content_type)
        return jsonify({"message": "Unsupported content type"}), 415

    jpeg_error = _validate_jpeg_image(request.data)
    if jpeg_error:
        current_app.logger.error("JPEG validation failed: %s", jpeg_error)
        return jsonify({"message": jpeg_error}), 400

    device_id = request.headers.get("X-Device-Id", "").strip()
    captured_at_raw = request.headers.get("X-Captured-At", "").strip()

    if not captured_at_raw:
        current_app.logger.error("Missing device metadata")
        return jsonify({"message": "Missing device metadata"}), 400

    device_id_error = _validate_device_id(device_id)
    if device_id_error:
        current_app.logger.error("Invalid device_id in upload: %s", device_id_error)
        return jsonify({"message": device_id_error}), 400

    try:
        captured_at = _parse_captured_at(captured_at_raw)
    except ValueError:
        safe_val = (captured_at_raw or "")[:64].replace("\n", "\\n").replace("\r", "\\r")
        current_app.logger.error("Invalid captured_at metadata: %s", safe_val)
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
        _check_disk_usage()
        metrics = current_app.extensions.get("app_metrics")
        if metrics is not None:
            # Use DB-derived camera_id to avoid unbounded label cardinality from headers
            metrics.upload_total.labels(camera_id=snapshot.camera_id or "unknown").inc()
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
        return jsonify({"message": "File upload failed"}), 500


@server.route("/files")
def list_files():
    try:
        page = min(max(1, int(request.args.get("page", 1))), _FILES_PAGE_MAX)
    except ValueError:
        page = 1
    limit = _PAGINATION_LIMIT_DEFAULT
    offset = (page - 1) * limit
    try:
        store = _metadata_store()
        total = store.count_snapshots()
        files = store.list_recent_snapshots(limit=limit, offset=offset)
        total_pages = max(1, (total + limit - 1) // limit)
        return render_template(
            "files.html",
            files=files,
            page=page,
            total_pages=total_pages,
            total=total,
        )
    except Exception as exc:  # pragma: no cover
        current_app.logger.error("Error listing files: %s", exc)
        return jsonify({"message": "Error listing files"}), 500


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
        return jsonify({"message": "Error retrieving file"}), 500


@server.route("/cameras")
def list_cameras():
    camera_summaries = _metadata_store().list_camera_summaries()
    return render_template("cameras.html", camera_summaries=camera_summaries)


@server.route("/cameras/<camera_id>")
def camera_detail(camera_id: str):
    if _validate_device_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
    camera = _metadata_store().get_camera(camera_id)
    if camera is None:
        return jsonify({"message": "Camera not found"}), 404

    store = _metadata_store()
    snapshots = store.list_camera_snapshots(camera_id, limit=20)
    latest = snapshots[0] if snapshots else None
    recent_logs = store.list_device_logs(camera_id, limit=10)
    return render_template(
        "camera_detail.html",
        camera=camera,
        snapshots=snapshots,
        latest=latest,
        recent_logs=recent_logs,
    )


@server.route("/api/cameras")
def api_cameras():
    return jsonify({"cameras": [_serialize_camera_summary(item) for item in _metadata_store().list_camera_summaries()]})


@server.route("/api/cameras/<camera_id>", methods=["DELETE"])
@require_admin_auth
def api_delete_camera(camera_id: str):
    if _validate_device_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
    delete_files = request.args.get("delete_files", "false").lower() == "true"
    deleted = _metadata_store().delete_camera(camera_id)
    if not deleted:
        return jsonify({"message": "Camera not found"}), 404

    current_app.logger.info("Camera deleted: camera_id=%s delete_files=%s", camera_id, delete_files)

    storage_deleted = False
    if delete_files:
        try:
            _storage().delete_camera_files(camera_id)
            storage_deleted = True
        except Exception as exc:
            current_app.logger.error("Storage deletion failed for camera %s: %s", camera_id, exc)

    return jsonify({"message": "Camera deleted", "camera_id": camera_id, "storage_deleted": storage_deleted}), 200


@server.route("/api/cameras/<camera_id>", methods=["PATCH"])
@require_admin_auth
def api_patch_camera(camera_id: str):
    if _validate_device_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
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
    if _validate_device_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
    return jsonify(current_app.extensions["upload_metrics"].snapshot(camera_id))


@server.route("/api/cameras/<camera_id>/snapshots")
def api_camera_snapshots(camera_id: str):
    if _validate_device_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
    camera = _metadata_store().get_camera(camera_id)
    if camera is None:
        return jsonify({"message": "Camera not found"}), 404

    limit, offset = _parse_pagination()
    if limit is None:
        return jsonify({"message": "Invalid pagination parameters"}), 400

    since, until = None, None
    since_raw = request.args.get("since")
    until_raw = request.args.get("until")
    if since_raw:
        try:
            since = _parse_captured_at(since_raw)
        except ValueError:
            return jsonify({"message": "Invalid since parameter"}), 400
    if until_raw:
        try:
            until = _parse_captured_at(until_raw)
        except ValueError:
            return jsonify({"message": "Invalid until parameter"}), 400

    store = _metadata_store()

    if request.args.get("export") == "json":
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else None
        if not check_admin_token(token):
            return jsonify({"message": "Unauthorized"}), 401
        all_snapshots = store.list_camera_snapshots(
            camera_id, limit=_EXPORT_ROW_LIMIT, offset=0, since=since, until=until
        )
        return jsonify(
            {
                "camera_id": camera_id,
                "count": len(all_snapshots),
                "snapshots": [_serialize_snapshot(s) for s in all_snapshots],
            }
        )

    total = store.count_camera_snapshots(camera_id, since=since, until=until)
    snapshots = store.list_camera_snapshots(
        camera_id, limit=limit, offset=offset, since=since, until=until
    )
    return jsonify(
        {
            "camera_id": camera_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "snapshots": [_serialize_snapshot(s) for s in snapshots],
        }
    )


@server.route("/api/detections")
def api_detections():
    camera_id = request.args.get("camera_id")
    if camera_id is not None and _validate_device_id(camera_id):
        return jsonify({"message": "Invalid camera_id parameter"}), 400
    since_raw = request.args.get("since")
    since = None
    if since_raw:
        try:
            since = _parse_captured_at(since_raw)
        except ValueError:
            return jsonify({"message": "Invalid since parameter"}), 400

    confidence_min = None
    confidence_raw = request.args.get("confidence_min")
    if confidence_raw is not None:
        try:
            confidence_min = float(confidence_raw)
            if not (0.0 <= confidence_min <= 1.0):
                raise ValueError
        except ValueError:
            return jsonify({"message": "Invalid confidence_min parameter"}), 400

    store = _metadata_store()

    if request.args.get("export") == "csv":
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else None
        if not check_admin_token(token):
            return jsonify({"message": "Unauthorized"}), 401
        rows = store.list_detections(
            camera_id=camera_id,
            since=since,
            limit=_EXPORT_ROW_LIMIT,
            offset=0,
            confidence_min=confidence_min,
        )
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(["camera_id", "filename", "captured_at", "stored_at", "label", "score", "x1", "y1", "x2", "y2"])
        _csv_danger = frozenset("=+-@\t\r")
        for d in rows:
            bbox = d["bbox"]
            label = str(d["label"] or "")
            if label and label[0] in _csv_danger:
                label = "'" + label
            writer.writerow([
                d["camera_id"], d["filename"], d["captured_at"], d["stored_at"],
                label, round(d["score"], 4),
                bbox[0], bbox[1], bbox[2], bbox[3],
            ])
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=detections.csv"},
        )

    limit, offset = _parse_pagination()
    if limit is None:
        return jsonify({"message": "Invalid pagination parameters"}), 400

    total = store.count_detections(camera_id=camera_id, since=since, confidence_min=confidence_min)
    detections = store.list_detections(
        camera_id=camera_id, since=since, limit=limit, offset=offset, confidence_min=confidence_min
    )
    return jsonify(
        {
            "total": total,
            "limit": limit,
            "offset": offset,
            "detections": detections,
        }
    )


@server.route("/api/status_update", methods=["POST"])
@require_device_auth
def api_status_update():
    body = request.get_json(silent=True) or {}
    device_id = (body.get("device_id") or "").strip()
    device_id_error = _validate_device_id(device_id)
    if device_id_error:
        return jsonify({"message": device_id_error}), 400

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
@require_device_auth
def api_log_error():
    body = request.get_json(silent=True) or {}
    device_id = (body.get("device_id") or "").strip()
    level = (body.get("level") or "error").strip().lower()
    message = (body.get("message") or "").strip()

    device_id_error = _validate_device_id(device_id)
    if device_id_error:
        return jsonify({"message": device_id_error}), 400
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
@require_admin_auth
def api_camera_logs(camera_id: str):
    if _validate_device_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
    camera = _metadata_store().get_camera(camera_id)
    if camera is None:
        return jsonify({"message": "Camera not found"}), 404

    logs = _metadata_store().list_device_logs(camera_id)
    return jsonify({"camera_id": camera_id, "logs": [_serialize_device_log(log) for log in logs]})


@server.route("/metrics")
@require_admin_auth
def metrics():
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    app_metrics = current_app.extensions.get("app_metrics")
    if app_metrics is None:
        return jsonify({"message": "Metrics not available"}), 503

    # Refresh gauges with current system state
    upload_dir = current_app.config["UPLOAD_DIR"]
    try:
        usage = shutil.disk_usage(upload_dir)
        app_metrics.storage_used_bytes.set(usage.used)
    except OSError:
        pass

    inference_svc = current_app.extensions.get("inference_service")
    if inference_svc is not None:
        app_metrics.inference_queue_length.set(inference_svc.queue_length())

    for summary in _metadata_store().list_camera_summaries():
        cam = summary.camera
        ts = cam.last_seen_at.timestamp() if cam.last_seen_at else 0
        app_metrics.camera_last_seen_timestamp.labels(camera_id=cam.camera_id).set(ts)

    from flask import Response
    return Response(
        generate_latest(app_metrics.registry),
        mimetype=CONTENT_TYPE_LATEST,
    )
