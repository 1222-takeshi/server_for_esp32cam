from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int_or_none(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _load_device_api_keys() -> dict[str, str]:
    raw = os.getenv("ESP32CAM_DEVICE_API_KEYS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("ESP32CAM_DEVICE_API_KEYS must be a JSON object") from exc
    if not isinstance(parsed, dict):
        # ValueError, not TypeError: this validates externally-supplied env
        # var content (malformed config), not an internal argument type.
        raise ValueError("ESP32CAM_DEVICE_API_KEYS must be a JSON object")  # noqa: TRY004
    normalized: dict[str, str] = {}
    for device_id, api_key in parsed.items():
        key = str(device_id).strip()
        value = str(api_key).strip()
        if not key or not value:
            raise ValueError("ESP32CAM_DEVICE_API_KEYS must not contain empty device IDs or keys")
        normalized[key] = value
    return normalized


def gunicorn_preload_requested(
    argv: Sequence[str] | None = None,
    command_args: str | None = None,
) -> bool:
    """Return whether the Gunicorn invocation requests app preloading."""

    arguments = list(argv if argv is not None else sys.argv)
    if not arguments or Path(arguments[0]).name != "gunicorn":
        return False
    configured_args = command_args
    if configured_args is None:
        configured_args = os.getenv("GUNICORN_CMD_ARGS", "")
    return "--preload" in arguments[1:] or "--preload" in shlex.split(configured_args)


def gunicorn_worker_count(
    argv: Sequence[str] | None = None,
    command_args: str | None = None,
) -> int | None:
    """Return the configured Gunicorn worker count, if this is a Gunicorn process."""

    arguments = list(argv if argv is not None else sys.argv)
    if not arguments or Path(arguments[0]).name != "gunicorn":
        return None

    configured_args = command_args
    if configured_args is None:
        configured_args = os.getenv("GUNICORN_CMD_ARGS", "")
    # Explicit command-line options take precedence over GUNICORN_CMD_ARGS.
    options = shlex.split(configured_args) + arguments[1:]
    try:
        worker_count = int(os.getenv("WEB_CONCURRENCY", "1"))
    except ValueError:
        return None
    for index, option in enumerate(options):
        raw_value: str | None = None
        if option in {"--workers", "-w"} and index + 1 < len(options):
            raw_value = options[index + 1]
        elif option.startswith("--workers="):
            raw_value = option.partition("=")[2]
        elif option.startswith("-w") and option != "-w":
            raw_value = option[2:]
        if raw_value is None:
            continue
        try:
            worker_count = int(raw_value)
        except ValueError:
            return None
    return worker_count if worker_count > 0 else None


class Config:
    OPERATING_SOCKET = os.getenv("ESP32CAM_OPERATING_SOCKET", "")
    OPERATING_CAMERA_ID = os.getenv("ESP32CAM_OPERATING_CAMERA_ID", "esp32cam-01")
    BASE_DIR = Path(__file__).resolve().parent.parent
    UPLOAD_DIR = Path(os.getenv("ESP32CAM_UPLOAD_DIR", BASE_DIR / "uploads"))
    DB_PATH = Path(os.getenv("ESP32CAM_DB_PATH", BASE_DIR / "esp32cam.sqlite3"))
    STORAGE_BACKEND = os.getenv("ESP32CAM_STORAGE_BACKEND", "local")
    DETECTOR = os.getenv("ESP32CAM_DETECTOR", "disabled")
    MODEL_PATH = os.getenv("ESP32CAM_MODEL_PATH", "")
    LABELS_PATH = os.getenv("ESP32CAM_LABELS_PATH", "")
    DETECTION_THRESHOLD = float(os.getenv("ESP32CAM_DETECTION_THRESHOLD", "0.5"))
    PERSON_LABEL = os.getenv("ESP32CAM_PERSON_LABEL", "person")
    PERSON_CLASS_ID = int(os.getenv("ESP32CAM_PERSON_CLASS_ID", "0"))
    # Number of CPU threads the TFLite interpreter uses per inference call.
    # None lets TFLiteCpuDetector fall back to os.cpu_count() (see
    # script/detector.py, #400).
    DETECTOR_NUM_THREADS = _env_int_or_none("ESP32CAM_DETECTOR_NUM_THREADS")
    INFERENCE_MODE = os.getenv("ESP32CAM_INFERENCE_MODE", "async")
    KEEP_ONLY_DETECTED_SNAPSHOTS = _env_bool("ESP32CAM_KEEP_ONLY_DETECTED_SNAPSHOTS")
    TRACKING_ENABLED = _env_bool("ESP32CAM_TRACKING_ENABLED")
    TRACKING_IOU_THRESHOLD = float(os.getenv("ESP32CAM_TRACKING_IOU_THRESHOLD", "0.3"))
    TRACKING_MAX_MISSED_FRAMES = int(
        os.getenv("ESP32CAM_TRACKING_MAX_MISSED_FRAMES", "2")
    )
    TRACKING_MAX_TRACKS_PER_CAMERA = int(
        os.getenv("ESP32CAM_TRACKING_MAX_TRACKS_PER_CAMERA", "50")
    )
    TRACKING_MAX_CAMERAS = int(os.getenv("ESP32CAM_TRACKING_MAX_CAMERAS", "500"))
    CAMERA_EXPECTED_INTERVAL_SECONDS = int(
        os.getenv("ESP32CAM_CAMERA_EXPECTED_INTERVAL_SECONDS", "300")
    )
    # Bounds the in-process LRU cache of generated thumbnail JPEGs (see
    # script/thumbnail_cache.py, #394) -- avoids re-decoding/resizing/
    # re-encoding the same source image on every request while capping
    # memory use on long-lived deployments with many distinct images.
    THUMBNAIL_CACHE_MAX_ENTRIES = int(
        os.getenv("ESP32CAM_THUMBNAIL_CACHE_MAX_ENTRIES", "256")
    )
    PORT = int(os.getenv("PORT", "8080"))
    # Display-only LAN origin for camera setup; never used as a probe target.
    DEVICE_SERVER_BASE_URL = os.getenv("ESP32CAM_DEVICE_SERVER_BASE_URL", "")
    MAX_CONTENT_LENGTH = int(os.getenv("ESP32CAM_MAX_CONTENT_LENGTH", str(1 * 1024 * 1024)))
    SNAPSHOT_MAX_CONTENT_LENGTH = MAX_CONTENT_LENGTH
    MAX_VIDEO_CONTENT_LENGTH = int(
        os.getenv("ESP32CAM_MAX_VIDEO_CONTENT_LENGTH", str(10 * 1024 * 1024))
    )
    STORAGE_RETENTION_MAX_AGE_SECONDS = int(
        os.getenv("ESP32CAM_STORAGE_RETENTION_MAX_AGE_SECONDS", "2592000")
    )
    STORAGE_RETENTION_MAX_BYTES = int(os.getenv("ESP32CAM_STORAGE_RETENTION_MAX_BYTES", "0"))
    STORAGE_RETENTION_SCAN_INTERVAL_UPLOADS = int(
        os.getenv("ESP32CAM_STORAGE_RETENTION_SCAN_INTERVAL_UPLOADS", "1")
    )
    RETENTION_SCHEDULER_INTERVAL_SECONDS = int(
        os.getenv("ESP32CAM_RETENTION_SCHEDULER_INTERVAL_SECONDS", "3600")
    )
    # Gunicorn's WSGI entrypoint disables this before forking and starts the
    # scheduler lazily in the worker process. Direct create_app() users keep
    # the default auto-start behavior.
    RETENTION_SCHEDULER_AUTO_START = True
    MAX_WS_CONNECTIONS = int(os.getenv("ESP32CAM_MAX_WS_CONNECTIONS", "50"))
    MAX_WS_PENDING_EVENTS = int(os.getenv("ESP32CAM_MAX_WS_PENDING_EVENTS", "256"))
    DEVICE_API_KEY = os.getenv("ESP32CAM_DEVICE_API_KEY", "")
    DEVICE_API_KEYS = _load_device_api_keys()
    SHARED_KEY_DEVICE_ID = os.getenv("ESP32CAM_SHARED_KEY_DEVICE_ID", "")
    ADMIN_TOKEN = os.getenv("ESP32CAM_ADMIN_TOKEN", "")
    WEBHOOK_URL = os.getenv("ESP32CAM_WEBHOOK_URL", "")
    WEBHOOK_MIN_SCORE = float(os.getenv("ESP32CAM_WEBHOOK_MIN_SCORE", "0.5"))
    WEBHOOK_MAX_ATTEMPTS = int(os.getenv("ESP32CAM_WEBHOOK_MAX_ATTEMPTS", "3"))
    WEBHOOK_RETRY_BACKOFF_SECONDS = float(
        os.getenv("ESP32CAM_WEBHOOK_RETRY_BACKOFF_SECONDS", "1.0")
    )
    LOG_FORMAT = os.getenv("ESP32CAM_LOG_FORMAT", "text")
    UPLOAD_RATE_LIMIT = os.getenv("ESP32CAM_UPLOAD_RATE_LIMIT", "10 per minute")
    VIDEO_UPLOAD_RATE_LIMIT = os.getenv("ESP32CAM_VIDEO_UPLOAD_RATE_LIMIT", "5 per minute")
    LOG_ERROR_RATE_LIMIT = os.getenv("ESP32CAM_LOG_ERROR_RATE_LIMIT", "30 per minute")
    RATELIMIT_STORAGE_URI = os.getenv("ESP32CAM_RATELIMIT_STORAGE_URI", "memory://")
    UPLOAD_MAX_IMAGE_DIMENSION = int(os.getenv("ESP32CAM_UPLOAD_MAX_IMAGE_DIMENSION", "4096"))
    VIDEO_RETENTION_MAX_AGE_SECONDS = int(
        os.getenv("ESP32CAM_VIDEO_RETENTION_MAX_AGE_SECONDS", "0")
    )
    VIDEO_RETENTION_MAX_BYTES = int(os.getenv("ESP32CAM_VIDEO_RETENTION_MAX_BYTES", "0"))
    VIDEO_RETENTION_SCAN_INTERVAL_UPLOADS = int(
        os.getenv("ESP32CAM_VIDEO_RETENTION_SCAN_INTERVAL_UPLOADS", "1")
    )
    # Device logs have independent age/all-camera row budgets. Both opt in:
    # enabling them prunes existing history, so upgrading alone must not
    # silently start deleting rows (#357, #510). Physical DB/WAL size is a
    # separate budget; deleting rows does not shrink the database file.
    DEVICE_LOG_RETENTION_MAX_AGE_SECONDS = int(
        os.getenv("ESP32CAM_DEVICE_LOG_RETENTION_MAX_AGE_SECONDS", "0")
    )
    DEVICE_LOG_RETENTION_MAX_ROWS = int(os.getenv("ESP32CAM_DEVICE_LOG_RETENTION_MAX_ROWS", "0"))
    MAX_MJPEG_STREAMS = int(os.getenv("ESP32CAM_MAX_MJPEG_STREAMS", "10"))
    # Default disabled (0) like the retention settings above: the single
    # detect-at-a-time inference pipeline (script/inference.py) has no
    # built-in bound on how deep its queue can grow if uploads outpace it,
    # but the "right" depth to cap it at depends on camera count/upload
    # cadence this project can't guess -- opt in once you've measured your
    # own deployment. See #376.
    INFERENCE_QUEUE_MAX_DEPTH = int(os.getenv("ESP32CAM_INFERENCE_QUEUE_MAX_DEPTH", "0"))
    # 0 derives the timeout from each camera's expected interval plus a grace
    # period, so a sparse camera does not disconnect while it is healthy.
    MJPEG_STREAM_IDLE_TIMEOUT_SECONDS = int(
        os.getenv("ESP32CAM_MJPEG_STREAM_IDLE_TIMEOUT_SECONDS", "0")
    )
    MAX_TRACKED_CAMERAS = int(os.getenv("ESP32CAM_MAX_TRACKED_CAMERAS", "500"))
    DISK_USAGE_CHECK_INTERVAL_UPLOADS = int(
        os.getenv("ESP32CAM_DISK_USAGE_CHECK_INTERVAL_UPLOADS", "1")
    )
