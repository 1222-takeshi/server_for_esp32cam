from __future__ import annotations

import os
from pathlib import Path


class Config:
    BASE_DIR = Path(__file__).resolve().parent.parent
    UPLOAD_DIR = Path(os.getenv("ESP32CAM_UPLOAD_DIR", BASE_DIR / "uploads"))
    STORAGE_BACKEND = os.getenv("ESP32CAM_STORAGE_BACKEND", "local")
    PORT = int(os.getenv("PORT", "8080"))
    MAX_CONTENT_LENGTH = int(os.getenv("ESP32CAM_MAX_CONTENT_LENGTH", str(1 * 1024 * 1024)))
