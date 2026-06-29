from __future__ import annotations

import json
import os
from pathlib import Path


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
    MAX_WS_CONNECTIONS = int(os.getenv("ESP32CAM_MAX_WS_CONNECTIONS", "50"))
    DEVICE_API_KEY = os.getenv("ESP32CAM_DEVICE_API_KEY", "")
    DEVICE_API_KEYS = _load_device_api_keys()
    ADMIN_TOKEN = os.getenv("ESP32CAM_ADMIN_TOKEN", "")
    WEBHOOK_URL = os.getenv("ESP32CAM_WEBHOOK_URL", "")
    WEBHOOK_MIN_SCORE = float(os.getenv("ESP32CAM_WEBHOOK_MIN_SCORE", "0.5"))
    LOG_FORMAT = os.getenv("ESP32CAM_LOG_FORMAT", "text")
    UPLOAD_RATE_LIMIT = os.getenv("ESP32CAM_UPLOAD_RATE_LIMIT", "10 per minute")
    VIDEO_UPLOAD_RATE_LIMIT = os.getenv("ESP32CAM_VIDEO_UPLOAD_RATE_LIMIT", "5 per minute")
    RATELIMIT_STORAGE_URI = os.getenv("ESP32CAM_RATELIMIT_STORAGE_URI", "memory://")
    UPLOAD_MAX_IMAGE_DIMENSION = int(os.getenv("ESP32CAM_UPLOAD_MAX_IMAGE_DIMENSION", "4096"))
    VIDEO_RETENTION_MAX_AGE_SECONDS = int(
        os.getenv("ESP32CAM_VIDEO_RETENTION_MAX_AGE_SECONDS", "0")
    )
    VIDEO_RETENTION_MAX_BYTES = int(os.getenv("ESP32CAM_VIDEO_RETENTION_MAX_BYTES", "0"))
    MAX_MJPEG_STREAMS = int(os.getenv("ESP32CAM_MAX_MJPEG_STREAMS", "10"))
