from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from datetime import timezone
from urllib.parse import urlparse

from script.detector import Detection
from script.metadata import SnapshotRecord

_logger = logging.getLogger(__name__)


class WebhookNotifier:
    def __init__(self, url: str, *, min_score: float = 0.5) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"ESP32CAM_WEBHOOK_URL must use http or https scheme, got: {url!r}"
            )
        self._url = url
        self._min_score = min_score

    def notify(self, snapshot: SnapshotRecord, detections: list[Detection]) -> None:
        best = max(
            (d for d in detections if d.score >= self._min_score),
            key=lambda d: d.score,
            default=None,
        )
        if best is None:
            return

        detected_at = (
            snapshot.captured_at
            if snapshot.captured_at is not None
            else snapshot.stored_at
        ).astimezone(timezone.utc).isoformat()

        payload = json.dumps(
            {
                "camera_id": snapshot.camera_id,
                "snapshot_relative_path": snapshot.relative_path,
                "score": round(best.score, 4),
                "detected_at": detected_at,
            }
        ).encode()

        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _send():
            try:
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except (urllib.error.URLError, OSError) as exc:
                _logger.warning("Webhook delivery failed: %s", exc)

        threading.Thread(target=_send, daemon=True).start()


class _DisabledNotifier:
    def notify(self, _snapshot: SnapshotRecord, _detections: list[Detection]) -> None:
        pass


DISABLED_NOTIFIER: _DisabledNotifier = _DisabledNotifier()
