from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
from collections.abc import Sequence


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _load_device_api_keys() -> dict[str, str]:
    raw = os.getenv("ESP32CAM_DEVICE_API_KEYS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("ESP32CAM_DEVICE_API_KEYS must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("ESP32CAM_DEVICE_API_KEYS must be a JSON object")
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


class Config:
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
    PORT = int(os.getenv("PORT", "8080"))
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
    MAX_MJPEG_STREAMS = int(os.getenv("ESP32CAM_MAX_MJPEG_STREAMS", "10"))
    MAX_TRACKED_CAMERAS = int(os.getenv("ESP32CAM_MAX_TRACKED_CAMERAS", "500"))
    DISK_USAGE_CHECK_INTERVAL_UPLOADS = int(
        os.getenv("ESP32CAM_DISK_USAGE_CHECK_INTERVAL_UPLOADS", "1")
    )
