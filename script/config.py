from __future__ import annotations

import os
from pathlib import Path


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
