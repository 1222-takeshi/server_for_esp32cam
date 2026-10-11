from __future__ import annotations

import base64
import binascii
import contextlib
import csv
import json
import shutil
import threading
import time
import uuid
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import BytesIO, StringIO
from pathlib import Path
from queue import Empty, Full, Queue

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
    stream_with_context,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from PIL import Image, ImageOps, UnidentifiedImageError
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from script.auth import (
    check_admin_token,
    check_device_api_key,
    check_device_api_key_for_device,
    require_admin_auth,
    require_device_auth,
)
from script.metadata import (
    ALLOWED_LOG_LEVELS,
    CameraSummary,
    _camera_status,
    DesiredConfigVersionConflict,
    DeviceLogRecord,
    SnapshotRecord,
    VideoRecord,
)
from script.retention import apply_upload_retention, apply_video_upload_retention
from script.storage import Storage
from script.validation import CAMERA_ID_MAX_LENGTH, is_valid_camera_id

# device_id and camera_id are the same identifier (see script/validation.py);
# both are validated with is_valid_camera_id/CAMERA_ID_MAX_LENGTH so the two
# checks can't drift apart again (see #255).
DEVICE_ID_MAX_LENGTH = CAMERA_ID_MAX_LENGTH


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

# House rule (#398): this app runs under gevent (see script/app.py's
# gunicorn `-k gevent` worker), so a route body executes on a greenlet that
# only yields at gevent-patched I/O points. Any route/helper that does
# CPU-bound native work -- Pillow decode/resize/encode, a TFLite/numpy
# `invoke()`, `shutil.rmtree()` over a large tree, etc. -- blocks that
# native call's whole duration without yielding, freezing *every* other
# greenlet on the worker (uploads, dashboard polling, `/healthz`) for as
# long as it runs. This has already bitten this codebase twice for image
# processing alone: TFLite inference (#244/#259) and thumbnail generation
# (#391/#397), both fixed by moving the blocking call onto a small
# `gevent.threadpool.ThreadPool` (see `_thumbnail_pool()` below and
# `InferenceService._detect_pool` in script/inference.py) instead of
# running it inline on the request greenlet. New routes/helpers with this
# shape should default to the same ThreadPool-offload pattern from the
# start rather than needing a follow-up perf fix once it's noticed in
# production.

_mjpeg_stream_count = 0
_mjpeg_stream_lock = threading.Lock()

_PAGINATION_LIMIT_MAX = 200
_PAGINATION_LIMIT_DEFAULT = 50
_PAGINATION_OFFSET_MAX = 10_000_000
_FILES_PAGE_MAX = _PAGINATION_OFFSET_MAX // _PAGINATION_LIMIT_DEFAULT
_EXPORT_ROW_LIMIT = 10_000

# /files/<filename> serves the full-resolution original -- fine for direct
# downloads, but files.html/index.html/camera_detail.html only ever display
# it inside a small (56x42 to ~300x225) cropped preview. ESP32-CAM frames go
# up to UXGA (1600x1200) and can run several hundred KB each, so painting a
# 50-row page or a multi-camera grid from that route means transferring tens
# of originals just to downscale them in CSS (see #338). This bounding box
# keeps enough detail for those preview sizes (including >1x pixel density)
# while cutting transferred bytes by roughly an order of magnitude.
_THUMBNAIL_MAX_DIMENSION = 480
_THUMBNAIL_JPEG_QUALITY = 82


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


def _encode_snapshot_cursor(snapshot: SnapshotRecord) -> str:
    payload = json.dumps(
        [snapshot.stored_at.isoformat(), snapshot.id],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_snapshot_cursor(value: str) -> tuple[str, int] | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        stored_at, snapshot_id = json.loads(
            base64.urlsafe_b64decode(padded).decode("utf-8")
        )
        parsed_stored_at = datetime.fromisoformat(stored_at.replace("Z", "+00:00"))
        if (
            parsed_stored_at.tzinfo is None
            or isinstance(snapshot_id, bool)
            or not isinstance(snapshot_id, int)
            or snapshot_id < 1
        ):
            return None
        return stored_at, snapshot_id
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None


def _encode_detection_cursor(row: dict[str, object]) -> str:
    payload = json.dumps(
        [row["stored_at"], row["snapshot_id"], row["detection_id"]],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_detection_cursor(value: str) -> tuple[str, int, int] | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        stored_at, snapshot_id, detection_id = decoded
        if (
            not isinstance(stored_at, str)
            or isinstance(snapshot_id, bool)
            or not isinstance(snapshot_id, int)
            or isinstance(detection_id, bool)
            or not isinstance(detection_id, int)
            or snapshot_id < 1
            or detection_id < 1
        ):
            return None
        return stored_at, snapshot_id, detection_id
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None


def _check_disk_usage() -> None:
    gate = current_app.extensions.get("disk_usage_scan_gate")
    if gate is not None and not gate.should_scan():
        return
    upload_dir = current_app.config["UPLOAD_DIR"]
    usage = _cached_disk_usage(str(upload_dir))
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


def _thumbnail_pool():
    return current_app.extensions["thumbnail_pool"]


def _thumbnail_cache():
    return current_app.extensions["thumbnail_cache"]


def _storage_delete_pool():
    return current_app.extensions["storage_delete_pool"]


def _snapshot_event_publisher():
    return current_app.extensions["snapshot_event_publisher"]


def _validate_identifier(value: str, label: str) -> str | None:
    """Returns an error message string if invalid, None if valid."""
    if not value:
        return f"{label} is required"
    if not is_valid_camera_id(value):
        return f"{label} contains invalid characters or exceeds maximum length"
    return None


def _validate_device_id(device_id: str) -> str | None:
    return _validate_identifier(device_id, "device_id")


def _validate_camera_id(camera_id: str) -> str | None:
    return _validate_identifier(camera_id, "camera_id")


def _get_json_object() -> dict[str, object] | None:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def _validate_jpeg_image(data: bytes) -> str | None:
    """Returns None if the JPEG is valid, or an error message string if invalid."""
    max_dim = current_app.config.get("UPLOAD_MAX_IMAGE_DIMENSION", 4096)
    try:
        with Image.open(BytesIO(data)) as img:
            if img.width > max_dim or img.height > max_dim:
                return f"Image dimensions exceed maximum ({img.width}x{img.height}, max {max_dim}px)"
    except (UnidentifiedImageError, SyntaxError, ValueError, OSError):
        return "Invalid or corrupted JPEG payload"
    except Exception:  # noqa: BLE001 - untrusted upload data, don't crash on an exotic decoder error
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
        "track_id": detection.track_id,
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


def _serialize_video(video: VideoRecord) -> dict[str, object]:
    return {
        "id": video.id,
        "filename": video.filename,
        "relative_path": video.relative_path,
        "size": video.size,
        "camera_id": video.camera_id,
        "captured_at": video.captured_at.isoformat() if video.captured_at else None,
        "stored_at": video.stored_at.isoformat(),
        "duration_ms": video.duration_ms,
        "content_type": video.content_type,
        "sha256": video.sha256,
        "processing_status": video.processing_status,
        "processing_error": video.processing_error,
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


_DESIRED_CONFIG_DEFAULT: dict[str, object] = {
    "capture_interval_seconds": 300,
    "frame_size": "SVGA",
    "jpeg_quality": 12,
    "vflip": False,
    "hmirror": False,
    "restart_requested": False,
    "ota_update_requested": False,
    "ota_window_seconds": 120,
}
_ALLOWED_FRAME_SIZES = frozenset({"QVGA", "VGA", "SVGA", "XGA", "SXGA", "UXGA"})


def _serialize_desired_config(record) -> dict[str, object]:
    return {
        "camera_id": record.camera_id,
        "version": record.version,
        "updated_at": record.updated_at.isoformat(),
        "updated_by": record.updated_by,
        "config": record.config,
        "last_acknowledged_version": record.last_acknowledged_version,
        "last_acknowledged_at": (
            record.last_acknowledged_at.isoformat() if record.last_acknowledged_at else None
        ),
        "last_acknowledged_message": record.last_acknowledged_message,
        "last_acknowledged_success": record.last_acknowledged_success,
        "last_firmware_version": record.last_firmware_version,
        "last_free_heap": record.last_free_heap,
    }


def _validate_desired_config_payload(config: object) -> tuple[dict[str, object] | None, str | None]:
    if not isinstance(config, dict):
        return None, "config must be an object"
    unknown = sorted(set(config) - set(_DESIRED_CONFIG_DEFAULT))
    if unknown:
        return None, f"unknown desired config keys: {', '.join(unknown)}"
    merged = {**_DESIRED_CONFIG_DEFAULT, **config}

    interval = merged["capture_interval_seconds"]
    if not isinstance(interval, int) or isinstance(interval, bool) or not 5 <= interval <= 86400:
        return None, "capture_interval_seconds must be an integer between 5 and 86400"
    frame_size = merged["frame_size"]
    if not isinstance(frame_size, str) or frame_size not in _ALLOWED_FRAME_SIZES:
        return None, f"frame_size must be one of {sorted(_ALLOWED_FRAME_SIZES)}"
    jpeg_quality = merged["jpeg_quality"]
    if not isinstance(jpeg_quality, int) or isinstance(jpeg_quality, bool) or not 10 <= jpeg_quality <= 63:
        return None, "jpeg_quality must be an integer between 10 and 63"
    for key in ("vflip", "hmirror", "restart_requested", "ota_update_requested"):
        if not isinstance(merged[key], bool):
            return None, f"{key} must be a boolean"
    ota_window = merged["ota_window_seconds"]
    if not isinstance(ota_window, int) or isinstance(ota_window, bool) or not 30 <= ota_window <= 600:
        return None, "ota_window_seconds must be an integer between 30 and 600"
    return merged, None


def _get_or_create_desired_config(camera_id: str):
    store = _metadata_store()
    record = store.get_desired_config(camera_id)
    if record is not None:
        return record
    return store.upsert_desired_config(camera_id, dict(_DESIRED_CONFIG_DEFAULT))


def _check_device_matches_path(device_id: str):
    if not check_device_api_key(request.headers.get("X-API-Key")):
        return jsonify({"message": "Unauthorized"}), 401
    if not check_device_api_key_for_device(device_id, request.headers.get("X-API-Key")):
        return jsonify({"message": "Forbidden"}), 403
    return None


def _check_video_upload_auth(camera_id: str):
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return jsonify({"message": "Unauthorized"}), 401
    per_device = current_app.config.get("DEVICE_API_KEYS", {}) or {}
    # Video uploads intentionally require per-camera keys. This is fail-closed:
    # a missing mapping or a camera without a key is Forbidden instead of
    # falling back to the legacy global DEVICE_API_KEY behavior.
    if not per_device or camera_id not in per_device:
        return jsonify({"message": "Forbidden"}), 403
    if not check_device_api_key_for_device(camera_id, api_key):
        return jsonify({"message": "Forbidden"}), 403
    return None


def _sanitize_log_value(value: str | None) -> str:
    """Truncate and strip newlines from untrusted input before logging it.

    Prevents log injection (CRLF-separated fake log lines) from
    attacker-controlled header/metadata values that fail validation.
    """
    return (value or "")[:64].replace("\n", "\\n").replace("\r", "\\r")


def _parse_video_duration_ms(value: str | None) -> tuple[int | None, str | None]:
    if value is None or value.strip() == "":
        return None, None
    try:
        duration_ms = int(value)
    except ValueError:
        return None, "duration_ms must be a non-negative integer"
    if duration_ms < 0:
        return None, "duration_ms must be a non-negative integer"
    return duration_ms, None


def _parse_optional_captured_at(value: str | None) -> tuple[datetime | None, str | None]:
    if value is None or value.strip() == "":
        return None, None
    try:
        return _parse_captured_at(value), None
    except ValueError:
        safe_val = _sanitize_log_value(value)
        current_app.logger.error("Invalid video captured_at metadata: %s", safe_val)
        return None, "Invalid captured_at metadata"


def _read_video_upload_payload() -> tuple[object | None, str | None, datetime | None, int | None, tuple[Response, int] | None]:
    """Resolve the readable stream + metadata for an incoming video upload.

    Returns a `stream` object (anything exposing `read(size)`) instead of a
    fully-buffered `bytes` payload -- `request.stream` for raw video/mp4
    bodies, or the multipart FileStorage's own `.stream` -- so the caller can
    hand it to `Storage.save_video_stream()` and write it to disk in bounded
    chunks rather than materializing the whole upload in memory first (see
    #282 / #250). Content-Length is still checked up front as a fast-path
    rejection; the storage layer separately enforces `max_bytes` while
    reading, since a chunked request without Content-Length can't be checked
    here before any bytes are read.
    """
    max_bytes = current_app.config.get("MAX_VIDEO_CONTENT_LENGTH", 10 * 1024 * 1024)
    if request.content_length is not None and request.content_length > max_bytes:
        return None, None, None, None, (jsonify({"message": "Payload too large"}), 413)

    if request.mimetype == "video/mp4":
        stream = request.stream
        content_type = request.mimetype
        captured_at, captured_error = _parse_optional_captured_at(request.headers.get("X-Captured-At"))
        duration_ms, duration_error = _parse_video_duration_ms(request.headers.get("X-Duration-Ms"))
    elif request.mimetype == "multipart/form-data":
        upload = request.files.get("file")
        if upload is None:
            return None, None, None, None, (jsonify({"message": "file field is required"}), 400)
        content_type = upload.mimetype or ""
        if content_type != "video/mp4":
            return None, None, None, None, (jsonify({"message": "Unsupported content type"}), 415)
        stream = upload.stream
        captured_at, captured_error = _parse_optional_captured_at(request.form.get("captured_at"))
        duration_ms, duration_error = _parse_video_duration_ms(request.form.get("duration_ms"))
    else:
        return None, None, None, None, (jsonify({"message": "Unsupported content type"}), 415)

    if captured_error:
        return None, None, None, None, (jsonify({"message": captured_error}), 400)
    if duration_error:
        return None, None, None, None, (jsonify({"message": duration_error}), 400)
    return stream, content_type, captured_at, duration_ms, None


_SNAPSHOT_COUNT_CACHE_SECONDS = 5.0
_snapshot_count_cache: dict[str, tuple[float, int]] = {}


def _cached_total_snapshot_count(store) -> int:
    """`SQLiteMetadataStore.total_snapshot_count()` with a short TTL cache.

    `SELECT COUNT(*) FROM snapshots` has no WHERE clause, so SQLite answers it
    by scanning every row in the table (or its smallest index) rather than an
    indexed point lookup -- unlike the other dashboard queries in this module,
    which are all bounded by LIMIT/OFFSET or an indexed WHERE. `/` is the
    single most frequently reloaded page (manual refreshes, monitoring, the
    Raspberry Pi CPU-only deployment target this project explicitly plans
    for), and this count is a purely informational stat tile, not data the
    page's correctness depends on -- so mirror the `_cached_disk_usage()` /
    `_cached_storage_probe()` pattern above instead of re-scanning `snapshots`
    on every single load.
    """
    key = str(current_app.config["DB_PATH"])
    now = time.monotonic()
    cached = _snapshot_count_cache.get(key)
    if cached is not None and now - cached[0] < _SNAPSHOT_COUNT_CACHE_SECONDS:
        return cached[1]
    count = store.total_snapshot_count()
    _snapshot_count_cache[key] = (now, count)
    return count


@server.route("/")
def index():
    store = _metadata_store()
    limit, offset = _parse_pagination()
    if limit is None:
        abort(400, description="Invalid pagination parameters")
    camera_total = store.count_cameras()
    if camera_total and offset >= camera_total:
        offset = ((camera_total - 1) // limit) * limit
    # templates/index.html renders neither latest_snapshot.detections nor
    # latest_detection_at, so skip both extra per-camera lookups
    # list_camera_summaries() would otherwise run for this, the single most
    # frequently reloaded page (see _cached_total_snapshot_count() above).
    camera_summaries = store.list_camera_summaries(
        limit=limit, offset=offset, include_detections=False, include_latest_detection_at=False
    )
    one_hour_ago = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    cameras_with_errors = store.camera_ids_with_recent_errors(since=one_hour_ago)
    return render_template(
        "index.html",
        camera_summaries=camera_summaries,
        camera_total=camera_total,
        camera_limit=limit,
        camera_offset=offset,
        image_count=_cached_total_snapshot_count(store),
        cameras_with_errors=cameras_with_errors,
    )


@server.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


_STORAGE_PROBE_CACHE_SECONDS = 5.0
_storage_probe_cache: dict[str, tuple[float, bool]] = {}


def _cached_storage_probe(upload_dir: Path) -> bool:
    """Write/read/delete probe of upload_dir's writability, with a short TTL cache.

    Mirrors `_cached_disk_usage()` below: storage writability doesn't flip
    state sub-second in normal operation, so polling `/healthz/ready` faster
    than the TTL (Dockerfile's HEALTHCHECK, external monitoring,
    docs/deployment.md's curl loops, deploy scripts' wait loops) would
    otherwise create a fresh, uniquely-named probe file -- and pay a
    write/read/delete cycle on the SD card -- on every single call. Failures
    are cached too (not just successes), so a real storage fault is still
    surfaced within one TTL window rather than being hidden indefinitely,
    while still bounding how often the check touches disk.
    """
    key = str(upload_dir)
    now = time.monotonic()
    cached = _storage_probe_cache.get(key)
    if cached is not None and now - cached[0] < _STORAGE_PROBE_CACHE_SECONDS:
        return cached[1]

    try:
        probe = upload_dir / f".healthz-probe-{uuid.uuid4().hex}"
        probe.write_bytes(b"\x00")
        probe.read_bytes()
        probe.unlink()
        ok = True
    except Exception as exc:  # noqa: BLE001 - health check: any failure means "not ok"
        current_app.logger.error("healthz/ready: storage check failed: %s", exc)
        ok = False

    _storage_probe_cache[key] = (now, ok)
    return ok


@server.route("/healthz/ready")
def healthz_ready():
    checks: dict[str, object] = {}
    degraded = False

    try:
        _metadata_store().ping()
        checks["db"] = "ok"
    except Exception as exc:  # noqa: BLE001 - health check: any failure means "not ok"
        current_app.logger.error("healthz/ready: db check failed: %s", exc)
        checks["db"] = "error"
        degraded = True

    upload_dir = Path(current_app.config["UPLOAD_DIR"])
    if _cached_storage_probe(upload_dir):
        checks["storage"] = "ok"
    else:
        checks["storage"] = "error"
        degraded = True

    try:
        usage = _cached_disk_usage(str(upload_dir))
        checks["disk_free_mb"] = usage.free // (1024 * 1024)
        if usage.free / usage.total < 0.05:
            degraded = True
    except Exception as exc:  # noqa: BLE001 - health check: any failure means "not ok"
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
    snapshot_max_bytes = current_app.config.get(
        "SNAPSHOT_MAX_CONTENT_LENGTH",
        current_app.config.get("MAX_CONTENT_LENGTH", 1 * 1024 * 1024),
    )
    if request.content_length is not None and request.content_length > snapshot_max_bytes:
        return jsonify({"message": "Payload too large"}), 413

    if not request.data:
        current_app.logger.error("No data received")
        return jsonify({"message": "No data received"}), 400

    if len(request.data) > snapshot_max_bytes:
        return jsonify({"message": "Payload too large"}), 413

    if request.mimetype != "image/jpeg":
        # request.content_type is the raw, device-supplied Content-Type header
        # value and, unlike device_id below, isn't restricted to a safe
        # character set or bounded length before this point. A raw HTTP
        # header value can't itself carry an embedded newline (the HTTP
        # parser rejects that), so this isn't the same CRLF log-injection
        # primitive #429 fixed for the JSON-body message field, but an
        # unbounded/unusual header value would still land in the log
        # verbatim. Route it through the same _sanitize_log_value() helper
        # used for other untrusted values for consistency and to bound its
        # length in the log line.
        current_app.logger.error(
            "Unsupported content type: %s", _sanitize_log_value(request.content_type)
        )
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
    if not check_device_api_key_for_device(device_id, request.headers.get("X-API-Key")):
        return jsonify({"message": "Forbidden"}), 403

    try:
        captured_at = _parse_captured_at(captured_at_raw)
    except ValueError:
        safe_val = _sanitize_log_value(captured_at_raw)
        current_app.logger.error("Invalid captured_at metadata: %s", safe_val)
        return jsonify({"message": "Invalid captured_at metadata"}), 400

    # Backpressure for the single detect-at-a-time inference pipeline
    # (script/inference.py): with no cap, a burst of uploads outpacing
    # detection grows this queue without bound (see #376). Checked after
    # auth/validation so only accepted uploads count against it, and
    # before the storage write below so a rejected upload doesn't leave
    # an image on disk that will never get inferred. Disabled (0) by
    # default -- see INFERENCE_QUEUE_MAX_DEPTH's definition in config.py.
    max_queue_depth = current_app.config.get("INFERENCE_QUEUE_MAX_DEPTH", 0)
    if max_queue_depth > 0 and _inference_service().queue_length() >= max_queue_depth:
        return jsonify({"message": "Server busy, try again later"}), 429

    try:
        stored = _storage().save_image(
            request.data,
            request.mimetype,
            device_id=device_id,
            captured_at=captured_at,
        )
        snapshot = _metadata_store().record_snapshot(stored)
        _inference_service().enqueue_snapshot(snapshot.id)
        _snapshot_event_publisher().publish_snapshot_created(snapshot, image_bytes=request.data)
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
    except Exception as exc:  # noqa: BLE001 - pragma: no cover - route error boundary
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
    before = None
    before_str = request.args.get("before")
    if before_str is not None:
        before = _decode_snapshot_cursor(before_str)
        if before is None:
            return jsonify({"message": "Invalid before parameter"}), 400
        offset = 0
    try:
        store = _metadata_store()
        files, total = store.list_recent_snapshots_page(
            limit=limit,
            offset=offset,
            before=before,
            include_detections=False,
        )
        total_pages = max(1, (total + limit - 1) // limit)
        return render_template(
            "files.html",
            files=files,
            page=page,
            total_pages=total_pages,
            total=total,
            before=before,
            next_before=_encode_snapshot_cursor(files[-1]) if len(files) == limit else None,
        )
    except Exception as exc:  # noqa: BLE001 - pragma: no cover - route error boundary
        current_app.logger.error("Error listing files: %s", exc)
        return jsonify({"message": "Error listing files"}), 500


@server.route("/files/<filename>")
def get_file(filename: str):
    try:
        snapshot = _metadata_store().get_snapshot_by_filename(filename)
        if snapshot is None:
            return jsonify({"message": "File not found"}), 404
        path = _storage().read_image_path_by_relative_path(snapshot.relative_path)
        response = send_file(
            path,
            mimetype="image/jpeg",
            download_name=filename,
            conditional=True,
            max_age=31536000,
        )
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
    except FileNotFoundError:
        return jsonify({"message": "File not found"}), 404
    except Exception as exc:  # noqa: BLE001 - pragma: no cover - route error boundary
        current_app.logger.error("Error retrieving file: %s", exc)
        return jsonify({"message": "Error retrieving file"}), 500


def _build_thumbnail_jpeg(path) -> bytes:
    """Decode/resize/encode a thumbnail. Runs off the request greenlet (see
    _thumbnail_pool) since Pillow's native calls don't yield to gevent."""
    with Image.open(path) as image:
        # draft() asks libjpeg to decode at a downscaled DCT size instead
        # of full resolution when the JPEG encoding allows it -- much
        # cheaper than a full decode for a target this small, and still
        # plenty of detail for a thumbnail (same trick used by
        # detector.py's TFLiteCpuDetector._prepare_input).
        image.draft("RGB", (_THUMBNAIL_MAX_DIMENSION, _THUMBNAIL_MAX_DIMENSION))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((_THUMBNAIL_MAX_DIMENSION, _THUMBNAIL_MAX_DIMENSION))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=_THUMBNAIL_JPEG_QUALITY)
    buffer.seek(0)
    return buffer.getvalue()


@server.route("/files/<filename>/thumbnail")
def get_file_thumbnail(filename: str):
    try:
        snapshot = _metadata_store().get_snapshot_by_filename(filename)
        if snapshot is None:
            return jsonify({"message": "File not found"}), 404
        path = _storage().read_image_path_by_relative_path(snapshot.relative_path)
        file_stat = path.stat()

        # Re-decoding/resizing/re-encoding the same source image on every
        # request is wasted CPU once one client has already fetched it --
        # cache the generated JPEG bytes in-process, keyed by the source
        # file's current (mtime, size) so a replaced/re-uploaded file at the
        # same path is never served a stale thumbnail (see #394).
        cache = _thumbnail_cache()
        jpeg_bytes = cache.get(
            snapshot.relative_path, mtime_ns=file_stat.st_mtime_ns, size=file_stat.st_size
        )
        if jpeg_bytes is None:
            # Pillow's decode/resize/encode is a blocking native call -- run
            # it on the thumbnail worker pool so it can't stall the gevent
            # hub and freeze every other greenlet (uploads, dashboard,
            # /healthz) for its duration, mirroring the pool already used
            # for detector inference.
            jpeg_bytes = _thumbnail_pool().apply(_build_thumbnail_jpeg, (path,))
            cache.put(
                snapshot.relative_path,
                mtime_ns=file_stat.st_mtime_ns,
                size=file_stat.st_size,
                jpeg_bytes=jpeg_bytes,
            )
        buffer = BytesIO(jpeg_bytes)

        response = send_file(
            buffer,
            mimetype="image/jpeg",
            download_name=filename,
            max_age=31536000,
        )
        # Content is a deterministic function of the (immutable, uniquely
        # named) original -- same long-lived immutable caching as get_file()
        # above, so a client only ever fetches a given thumbnail once.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
    except FileNotFoundError:
        return jsonify({"message": "File not found"}), 404
    except UnidentifiedImageError:
        return jsonify({"message": "File not found"}), 404
    except Exception as exc:  # noqa: BLE001 - pragma: no cover - route error boundary
        current_app.logger.error("Error generating thumbnail: %s", exc)
        return jsonify({"message": "Error retrieving file"}), 500


@server.route("/stream/cameras/<camera_id>")
def mjpeg_stream(camera_id: str):
    global _mjpeg_stream_count
    if _validate_camera_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404

    max_streams = current_app.config.get("MAX_MJPEG_STREAMS", 10)
    with _mjpeg_stream_lock:
        if _mjpeg_stream_count >= max_streams:
            return jsonify({"message": "Too many active streams"}), 503
        _mjpeg_stream_count += 1

    publisher = current_app.extensions["snapshot_event_publisher"]
    storage = _storage()
    frame_queue: Queue[bytes] = Queue(maxsize=1)

    def on_event(event: dict) -> None:
        if event.get("type") != "snapshot.created":
            return
        relative_path = (event.get("snapshot") or {}).get("relative_path")
        if not relative_path:
            return
        data = getattr(event, "raw_image", None)
        if data is None:
            try:
                data = storage.read_image_by_relative_path(relative_path)
            except FileNotFoundError:
                return
        with contextlib.suppress(Empty):
            frame_queue.get_nowait()
        # A concurrent producer may refill the (maxsize=1) queue between the
        # drop-stale get_nowait() above and this put -- fine to drop this
        # frame, the next event will retry.
        with contextlib.suppress(Full):
            frame_queue.put_nowait(data)

    unsubscribe = None
    try:
        unsubscribe = publisher.subscribe(on_event, camera_id=camera_id)

        camera = _metadata_store().get_camera(camera_id)
        expected_interval_seconds = (
            camera.expected_interval_seconds
            if camera is not None
            else current_app.config.get("CAMERA_EXPECTED_INTERVAL_SECONDS", 300)
        )
        configured_idle_timeout = current_app.config.get(
            "MJPEG_STREAM_IDLE_TIMEOUT_SECONDS", 0
        )
        idle_timeout_seconds = max(
            30.0,
            float(expected_interval_seconds) + 30.0,
            float(configured_idle_timeout),
        )
    except Exception:
        # Setup failed before the streaming Response (and its generator's
        # `finally` block) exists to release the slot — release it here so a
        # transient error (e.g. a DB hiccup) can't permanently occupy one of
        # MAX_MJPEG_STREAMS slots.
        if unsubscribe is not None:
            unsubscribe()
        with _mjpeg_stream_lock:
            _mjpeg_stream_count -= 1
        raise

    def generate():
        global _mjpeg_stream_count
        try:
            while True:
                try:
                    data = frame_queue.get(timeout=idle_timeout_seconds)
                except Empty:
                    break
                # A live stream starts with the next received frame, never a
                # historical saved snapshot. Explicit lengths let browsers
                # render a complete JPEG without waiting for the next boundary.
                header = (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(data)).encode("ascii") + b"\r\n\r\n"
                )
                yield header + data + b"\r\n"
        finally:
            unsubscribe()
            with _mjpeg_stream_lock:
                _mjpeg_stream_count -= 1

    return Response(
        stream_with_context(generate()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )


@server.route("/cameras")
def list_cameras():
    limit, offset = _parse_pagination()
    if limit is None:
        abort(400, description="Invalid pagination parameters")
    store = _metadata_store()
    camera_total = store.count_cameras()
    if camera_total and offset >= camera_total:
        offset = ((camera_total - 1) // limit) * limit
    # templates/cameras.html renders latest_snapshot.detections (a per-card
    # count) but never latest_detection_at, so only opt out of the latter --
    # unlike index() above, which skips both.
    camera_summaries = store.list_camera_summaries(
        limit=limit, offset=offset, include_latest_detection_at=False
    )
    return render_template(
        "cameras.html",
        camera_summaries=camera_summaries,
        camera_total=camera_total,
        camera_limit=limit,
        camera_offset=offset,
    )


def _camera_connection_details(camera) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    # An operator-supplied LAN origin, not the browser's (possibly Tailscale) host.
    origin = current_app.config.get("DEVICE_SERVER_BASE_URL", "")
    try:
        parsed = urlsplit(origin)
        valid = (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
            and parsed.port != 0
            and not any(char.isspace() for char in origin)
        )
        origin = f"{parsed.scheme}://{parsed.netloc}" if valid else None
    except (ValueError, TypeError):
        origin = None
    metrics = current_app.extensions["upload_metrics"].snapshot(camera.camera_id)
    return {
        "websocket_frames_received": metrics["frames_received"],
        "last_websocket_frame_at": metrics["last_frame_at"],
        "keep_only_detected_snapshots": current_app.config["KEEP_ONLY_DETECTED_SNAPSHOTS"],
        "status": _camera_status(camera.last_seen_at, camera.expected_interval_seconds, now=now),
        "last_seen_at": camera.last_seen_at.isoformat(),
        "seconds_since_contact": max(0, int((now - camera.last_seen_at).total_seconds())),
        "expected_interval_seconds": camera.expected_interval_seconds,
        "device_server_base_url": origin,
    }


@server.route("/api/cameras/<camera_id>/connection")
def api_camera_connection(camera_id: str):
    if _validate_device_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
    camera = _metadata_store().get_camera(camera_id)
    if camera is None:
        return jsonify({"message": "Camera not found"}), 404
    response = jsonify(_camera_connection_details(camera))
    response.headers["Cache-Control"] = "no-store"
    return response


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
    recent_videos = store.list_camera_videos(camera_id, limit=5)
    return render_template(
        "camera_detail.html",
        camera=camera,
        connection=_camera_connection_details(camera),
        snapshots=snapshots,
        latest=latest,
        recent_logs=recent_logs,
        recent_videos=recent_videos,
    )


@server.route("/api/cameras")
def api_cameras():
    store = _metadata_store()
    if "limit" not in request.args and "offset" not in request.args:
        # No explicit paging requested. Historically this fetched every
        # camera's CameraSummary (a per-camera latest-snapshot window query
        # plus a latest-detection join, see list_camera_summaries()) with no
        # bound at all -- unauthenticated and O(camera count) regardless of
        # fleet size (see #371). Cap it at _PAGINATION_LIMIT_MAX instead of
        # requiring existing small-fleet callers (including this project's
        # own dashboard JS) to start passing limit/offset: `truncated` tells
        # a caller with more cameras than fit in one capped page that it
        # needs to paginate explicitly to see the rest.
        total = store.count_cameras()
        summaries = store.list_camera_summaries(limit=_PAGINATION_LIMIT_MAX, offset=0)
        return jsonify(
            {
                "total": total,
                "limit": _PAGINATION_LIMIT_MAX,
                "offset": 0,
                "truncated": total > len(summaries),
                "cameras": [_serialize_camera_summary(item) for item in summaries],
            }
        )
    limit, offset = _parse_pagination()
    if limit is None:
        return jsonify({"message": "Invalid pagination parameters"}), 400
    total = store.count_cameras()
    summaries = store.list_camera_summaries(limit=limit, offset=offset)
    return jsonify(
        {
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": offset + len(summaries) < total,
            "cameras": [_serialize_camera_summary(item) for item in summaries],
        }
    )


@server.route("/api/cameras/<camera_id>/videos", methods=["POST"])
@limiter.limit(lambda: current_app.config.get("VIDEO_UPLOAD_RATE_LIMIT", "5 per minute"))
def api_upload_video(camera_id: str):
    if _validate_camera_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404

    auth_error = _check_video_upload_auth(camera_id)
    if auth_error is not None:
        return auth_error

    stream, content_type, captured_at, duration_ms, payload_error = _read_video_upload_payload()
    if payload_error is not None:
        return payload_error

    max_bytes = current_app.config.get("MAX_VIDEO_CONTENT_LENGTH", 10 * 1024 * 1024)
    stored = None
    try:
        stored = _storage().save_video_stream(
            stream,
            content_type or "",
            camera_id=camera_id,
            captured_at=captured_at,
            duration_ms=duration_ms,
            max_bytes=max_bytes,
        )
        video = _metadata_store().record_video(stored)
        apply_video_upload_retention(stored)
        _check_disk_usage()
        current_app.logger.info("Video uploaded successfully: %s", stored.name)
        return jsonify({"message": "Video uploaded successfully", "video": _serialize_video(video)}), 201
    except ValueError as exc:
        message = str(exc)
        status = 415 if message.startswith("Unsupported") else 400
        if message == "Unsupported video payload":
            status = 415
        elif message == "Payload too large":
            status = 413
        current_app.logger.error("Video upload validation failed: %s", message)
        return jsonify({"message": message}), status
    except Exception as exc:  # noqa: BLE001 - pragma: no cover - route error boundary
        if stored is not None:
            try:
                _storage().delete_video(stored)
            except Exception as cleanup_exc:  # noqa: BLE001 - best-effort cleanup, already erroring
                current_app.logger.error("Video cleanup failed after upload error: %s", cleanup_exc)
        current_app.logger.error("Video upload failed: %s", exc)
        return jsonify({"message": "Video upload failed"}), 500


@server.route("/api/cameras/<camera_id>/videos", methods=["GET"])
def api_list_camera_videos(camera_id: str):
    if _validate_camera_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
    camera = _metadata_store().get_camera(camera_id)
    if camera is None:
        return jsonify({"message": "Camera not found"}), 404
    try:
        limit = min(int(request.args.get("limit", 50)), 100)
    except (TypeError, ValueError):
        limit = 50
    before_str = request.args.get("before")
    before = None
    if before_str is not None:
        try:
            before = int(before_str)
        except (TypeError, ValueError):
            return jsonify({"message": "Invalid before parameter"}), 400
    videos = _metadata_store().list_camera_videos(camera_id, limit=limit, before=before)
    return jsonify({"videos": [_serialize_video(v) for v in videos], "camera_id": camera_id})


@server.route("/api/videos", methods=["GET"])
def api_list_videos():
    camera_id = request.args.get("camera_id")
    if camera_id is not None and _validate_camera_id(camera_id):
        return jsonify({"message": "Invalid camera_id parameter"}), 400
    try:
        limit = min(int(request.args.get("limit", 50)), 100)
    except (TypeError, ValueError):
        limit = 50
    before_str = request.args.get("before")
    before = None
    if before_str is not None:
        try:
            before = int(before_str)
        except (TypeError, ValueError):
            return jsonify({"message": "Invalid before parameter"}), 400
    videos = _metadata_store().list_videos(camera_id=camera_id, limit=limit, before=before)
    return jsonify({"videos": [_serialize_video(v) for v in videos]})


@server.route("/api/cameras/<camera_id>/videos/<int:video_id>/content", methods=["GET"])
def api_video_content(camera_id: str, video_id: int):
    if _validate_camera_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
    camera = _metadata_store().get_camera(camera_id)
    if camera is None:
        return jsonify({"message": "Camera not found"}), 404
    video = _metadata_store().get_video(video_id, camera_id=camera_id)
    if video is None:
        return jsonify({"message": "Video not found"}), 404
    try:
        path, content_type = _storage().read_video_path_by_relative_path(video.relative_path)
    except FileNotFoundError:
        return jsonify({"message": "Video not found"}), 404
    response = send_file(
        path,
        mimetype=content_type,
        download_name=video.filename,
        conditional=True,
        max_age=31536000,
    )
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


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

    # Drop the cached last-seen rows so the next /metrics scrape can't
    # re-create the gauge label forget_camera() is about to remove.
    _camera_last_seen_cache.pop(str(current_app.config["DB_PATH"]), None)
    app_metrics = current_app.extensions.get("app_metrics")
    if app_metrics is not None:
        app_metrics.forget_camera(camera_id)

    storage_deleted = False
    if delete_files:
        try:
            # shutil.rmtree() over a camera's whole upload directory is a
            # blocking native filesystem call that doesn't yield to gevent --
            # run it on a small worker pool (mirroring _thumbnail_pool above)
            # so it can't stall every other greenlet for the duration of a
            # large deletion.
            _storage_delete_pool().apply(_storage().delete_camera_files, (camera_id,))
            storage_deleted = True
        except Exception as exc:  # noqa: BLE001 - DB record already deleted; report partial success
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


@server.route("/api/cameras/<camera_id>/desired-config")
@require_admin_auth
def api_get_camera_desired_config(camera_id: str):
    if _validate_device_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
    record = _get_or_create_desired_config(camera_id)
    return jsonify(_serialize_desired_config(record)), 200


@server.route("/api/cameras/<camera_id>/desired-config", methods=["PUT"])
@require_admin_auth
def api_put_camera_desired_config(camera_id: str):
    if _validate_device_id(camera_id):
        return jsonify({"message": "Camera not found"}), 404
    body = request.get_json(silent=True) or {}
    config, error = _validate_desired_config_payload(body.get("config"))
    if error:
        return jsonify({"message": error}), 400
    expected_version = body.get("expected_version")
    if expected_version is not None and (
        not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0
    ):
        return jsonify({"message": "expected_version must be a non-negative integer"}), 400
    try:
        record = _metadata_store().upsert_desired_config(
            camera_id,
            config or {},
            expected_version=expected_version,
        )
    except DesiredConfigVersionConflict as exc:
        return jsonify({"message": str(exc), "current_version": exc.current_version}), 409
    return jsonify(_serialize_desired_config(record)), 200


@server.route("/api/devices/<device_id>/desired-config")
def api_get_device_desired_config(device_id: str):
    if _validate_device_id(device_id):
        return jsonify({"message": "Device not found"}), 404
    auth_error = _check_device_matches_path(device_id)
    if auth_error is not None:
        return auth_error
    try:
        current_version = int(request.args.get("current_version", "0"))
    except ValueError:
        return jsonify({"message": "current_version must be an integer"}), 400
    if current_version < 0:
        return jsonify({"message": "current_version must be non-negative"}), 400
    record = _get_or_create_desired_config(device_id)
    response = {
        "device_id": device_id,
        "version": record.version,
        "changed": current_version != record.version,
        "config": record.config,
    }
    return jsonify(response), 200


@server.route("/api/devices/<device_id>/desired-config/ack", methods=["POST"])
def api_ack_device_desired_config(device_id: str):
    if _validate_device_id(device_id):
        return jsonify({"message": "Device not found"}), 404
    auth_error = _check_device_matches_path(device_id)
    if auth_error is not None:
        return auth_error
    body = _get_json_object()
    if body is None:
        return jsonify({"message": "request body must be an object"}), 400
    version = body.get("version")
    applied = body.get("applied")
    message = body.get("message")
    firmware_version = body.get("firmware_version")
    free_heap = body.get("free_heap")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return jsonify({"message": "version must be a positive integer"}), 400
    if not isinstance(applied, bool):
        return jsonify({"message": "applied must be a boolean"}), 400
    if message is not None and (not isinstance(message, str) or len(message) > 500):
        return jsonify({"message": "message must be a string up to 500 characters"}), 400
    if firmware_version is not None and (not isinstance(firmware_version, str) or len(firmware_version) > 64):
        return jsonify({"message": "firmware_version must be a string up to 64 characters"}), 400
    if free_heap is not None and (not isinstance(free_heap, int) or isinstance(free_heap, bool) or free_heap < 0):
        return jsonify({"message": "free_heap must be a non-negative integer"}), 400
    current = _metadata_store().get_desired_config(device_id)
    if current is None:
        return jsonify({"message": "Desired config not found"}), 404
    if version != current.version:
        return jsonify({"message": "version does not match current desired config", "current_version": current.version}), 409
    try:
        record = _metadata_store().record_desired_config_ack(
            device_id,
            version=version,
            applied=applied,
            message=message,
            firmware_version=firmware_version,
            free_heap=free_heap,
        )
    except DesiredConfigVersionConflict as exc:
        return jsonify({"message": str(exc), "current_version": exc.current_version}), 409
    if record is None:
        return jsonify({"message": "Desired config not found"}), 404
    return jsonify(_serialize_desired_config(record)), 200


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
    before = None
    before_str = request.args.get("before")
    if before_str is not None:
        before = _decode_snapshot_cursor(before_str)
        if before is None:
            return jsonify({"message": "Invalid before parameter"}), 400
        if offset:
            return jsonify({"message": "before cannot be combined with offset"}), 400

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

    snapshots, total = store.list_camera_snapshots_page(
        camera_id, limit=limit, offset=offset, before=before, since=since, until=until
    )
    return jsonify(
        {
            "camera_id": camera_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_before": (
                _encode_snapshot_cursor(snapshots[-1]) if len(snapshots) == limit else None
            ),
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
        writer.writerow([
            "camera_id",
            "filename",
            "captured_at",
            "stored_at",
            "label",
            "score",
            "track_id",
            "x1",
            "y1",
            "x2",
            "y2",
        ])
        _csv_danger = frozenset("=+-@\t\r")
        for d in rows:
            bbox = d["bbox"]
            label = str(d["label"] or "")
            if label and label[0] in _csv_danger:
                label = "'" + label
            writer.writerow([
                d["camera_id"], d["filename"], d["captured_at"], d["stored_at"],
                label, round(d["score"], 4), d["track_id"],
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
    before = None
    before_str = request.args.get("before")
    if before_str is not None:
        before = _decode_detection_cursor(before_str)
        if before is None:
            return jsonify({"message": "Invalid before parameter"}), 400
        if offset:
            return jsonify({"message": "before cannot be combined with offset"}), 400

    detections, total = store.list_detections_page(
        camera_id=camera_id,
        since=since,
        limit=limit,
        offset=offset,
        before=before,
        confidence_min=confidence_min,
    )
    return jsonify(
        {
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_before": (
                _encode_detection_cursor(detections[-1]) if len(detections) == limit else None
            ),
            "detections": detections,
        }
    )


@server.route("/api/status_update", methods=["POST"])
@require_device_auth
def api_status_update():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"message": "request body must be an object"}), 400
    raw_device_id = body.get("device_id")
    if raw_device_id is not None and not isinstance(raw_device_id, str):
        return jsonify({"message": "device_id must be a string"}), 400
    device_id = (raw_device_id or "").strip()
    device_id_error = _validate_device_id(device_id)
    if device_id_error:
        return jsonify({"message": device_id_error}), 400
    if not check_device_api_key_for_device(device_id, request.headers.get("X-API-Key")):
        return jsonify({"message": "Forbidden"}), 403

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
@limiter.limit(lambda: current_app.config.get("LOG_ERROR_RATE_LIMIT", "30 per minute"))
@require_device_auth
def api_log_error():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"message": "request body must be an object"}), 400
    raw_device_id = body.get("device_id")
    raw_level = body.get("level")
    raw_message = body.get("message")
    if raw_device_id is not None and not isinstance(raw_device_id, str):
        return jsonify({"message": "device_id must be a string"}), 400
    if raw_level is not None and not isinstance(raw_level, str):
        return jsonify({"message": "level must be a string"}), 400
    if raw_message is not None and not isinstance(raw_message, str):
        return jsonify({"message": "message must be a string"}), 400
    device_id = (raw_device_id or "").strip()
    level = (raw_level or "error").strip().lower()
    message = (raw_message or "").strip()

    device_id_error = _validate_device_id(device_id)
    if device_id_error:
        return jsonify({"message": device_id_error}), 400
    if not check_device_api_key_for_device(device_id, request.headers.get("X-API-Key")):
        return jsonify({"message": "Forbidden"}), 403
    if not message:
        return jsonify({"message": "message is required"}), 400
    if len(message) > 2000:
        return jsonify({"message": "message is too long"}), 400
    if level not in ALLOWED_LOG_LEVELS:
        return jsonify({"message": f"level must be one of {sorted(ALLOWED_LOG_LEVELS)}"}), 400

    log = _metadata_store().record_device_log(device_id, level=level, message=message)
    # message is device-controlled and, unlike device_id/level above, isn't
    # restricted to a safe character set -- sanitize it the same way other
    # untrusted values are before logging (see _sanitize_log_value) so a
    # crafted message can't inject CRLF-separated fake log lines into the
    # server's own application log.
    current_app.logger.info("Device log from %s [%s]: %s", device_id, level, _sanitize_log_value(message))
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


_DISK_USAGE_CACHE_SECONDS = 5.0
_disk_usage_cache: dict[str, tuple[float, object]] = {}


def _cached_disk_usage(path: str):
    """`shutil.disk_usage()` with a short TTL cache, keyed by path.

    Called on every `/metrics` scrape; a Prometheus scrape interval shorter
    than the TTL would otherwise turn each scrape into an extra syscall for
    no observable benefit (disk usage doesn't change meaningfully sub-second).
    """
    now = time.monotonic()
    cached = _disk_usage_cache.get(path)
    if cached is not None and now - cached[0] < _DISK_USAGE_CACHE_SECONDS:
        return cached[1]
    usage = shutil.disk_usage(path)
    _disk_usage_cache[path] = (now, usage)
    return usage


_CAMERA_LAST_SEEN_CACHE_SECONDS = 5.0
_camera_last_seen_cache: dict[str, tuple[float, list]] = {}


def _cached_camera_last_seen(store) -> list:
    """`list_camera_last_seen()` with a short TTL cache, keyed by DB path.

    Mirrors `_cached_disk_usage()`: `/metrics` is scraped on a fixed interval,
    and last-seen timestamps don't need sub-TTL freshness for a gauge.
    """
    key = str(current_app.config["DB_PATH"])
    now = time.monotonic()
    cached = _camera_last_seen_cache.get(key)
    if cached is not None and now - cached[0] < _CAMERA_LAST_SEEN_CACHE_SECONDS:
        return cached[1]
    rows = store.list_camera_last_seen()
    _camera_last_seen_cache[key] = (now, rows)
    return rows


@server.route("/metrics")
@require_admin_auth
def metrics():
    app_metrics = current_app.extensions.get("app_metrics")
    if app_metrics is None:
        return jsonify({"message": "Metrics not available"}), 503

    # Refresh gauges with current system state
    upload_dir = current_app.config["UPLOAD_DIR"]
    try:
        usage = _cached_disk_usage(str(upload_dir))
        app_metrics.storage_used_bytes.set(usage.used)
    except OSError:
        pass

    inference_svc = current_app.extensions.get("inference_service")
    if inference_svc is not None:
        app_metrics.inference_queue_length.set(inference_svc.queue_length())

    for camera_id, last_seen_at in _cached_camera_last_seen(_metadata_store()):
        ts = last_seen_at.timestamp() if last_seen_at else 0
        app_metrics.camera_last_seen_timestamp.labels(camera_id=camera_id).set(ts)

    return Response(
        generate_latest(app_metrics.registry),
        mimetype=CONTENT_TYPE_LATEST,
    )


@lru_cache(maxsize=1)
def _openapi_spec_text() -> str | None:
    spec_path = Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"
    if not spec_path.is_file():
        return None
    return spec_path.read_text(encoding="utf-8")


@server.route("/api/openapi.yaml")
def api_openapi_yaml():
    spec_text = _openapi_spec_text()
    if spec_text is None:
        return jsonify({"message": "OpenAPI spec not found"}), 404
    return Response(spec_text, mimetype="application/yaml")


_SWAGGER_UI_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>API Docs — server_for_esp32cam</title>
  <link rel="stylesheet" href="{css_url}">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{bundle_url}"></script>
<script src="{preset_url}"></script>
<script>
  SwaggerUIBundle({{
    url: "/api/openapi.yaml",
    dom_id: "#swagger-ui",
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
    layout: "StandaloneLayout",
  }});
</script>
</body>
</html>
"""


def _swagger_ui_html() -> str:
    # Served from static/vendor/swagger-ui/ (vendored swagger-ui-dist,
    # pinned version documented in docs/contributing.md) instead of
    # unpkg.com, so /api/docs no longer depends on a third-party CDN being
    # reachable, floats to no unpinned "@5" version, and has no
    # Subresource-Integrity gap -- see #316. This also fixes the previous
    # `SwaggerUIBundle.SwaggerUIStandalonePreset` reference, which was
    # always `undefined` because swagger-ui-bundle.js never defines that
    # property and the standalone preset script wasn't loaded at all; the
    # preset is a separate top-level global (`SwaggerUIStandalonePreset`)
    # exposed by swagger-ui-standalone-preset.js, per swagger-ui-dist's own
    # documented usage.
    return _SWAGGER_UI_HTML_TEMPLATE.format(
        css_url=url_for("static", filename="vendor/swagger-ui/swagger-ui.css"),
        bundle_url=url_for("static", filename="vendor/swagger-ui/swagger-ui-bundle.js"),
        preset_url=url_for("static", filename="vendor/swagger-ui/swagger-ui-standalone-preset.js"),
    )


@server.route("/api/docs")
def api_docs():
    return Response(_swagger_ui_html(), mimetype="text/html")
