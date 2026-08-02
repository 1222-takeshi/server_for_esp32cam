from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import Future, ThreadPoolExecutor
import urllib.error
import urllib.request
from datetime import timezone
from urllib.parse import urlparse

from script.detector import Detection
from script.metadata import SnapshotRecord

_logger = logging.getLogger(__name__)
MAX_WEBHOOK_ATTEMPTS = 10
MAX_WEBHOOK_RETRY_BACKOFF_SECONDS = 60.0


class WebhookNotifier:
    def __init__(
        self,
        url: str,
        *,
        min_score: float = 0.5,
        max_workers: int = 3,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("ESP32CAM_WEBHOOK_URL must use http or https and include a host")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= MAX_WEBHOOK_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be an integer from 1 to {MAX_WEBHOOK_ATTEMPTS}, "
                f"got {max_attempts}"
            )
        if not math.isfinite(retry_backoff_seconds) or not (
            0 <= retry_backoff_seconds <= MAX_WEBHOOK_RETRY_BACKOFF_SECONDS
        ):
            raise ValueError(
                "retry_backoff_seconds must be finite and between 0 and "
                f"{MAX_WEBHOOK_RETRY_BACKOFF_SECONDS}, "
                f"got {retry_backoff_seconds}"
            )
        self._url = url
        self._min_score = min_score
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def notify(
        self, snapshot: SnapshotRecord, detections: list[Detection]
    ) -> Future[None] | None:
        best = max(
            (d for d in detections if d.score >= self._min_score),
            key=lambda d: d.score,
            default=None,
        )
        if best is None:
            return None

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

        def _send() -> None:
            for attempt in range(1, self._max_attempts + 1):
                try:
                    with urllib.request.urlopen(req, timeout=10):
                        return
                except urllib.error.HTTPError as exc:
                    failure_kind = f"http_{exc.code}"
                    retryable = 500 <= exc.code <= 599
                except (urllib.error.URLError, OSError) as exc:
                    failure_kind = type(exc).__name__
                    retryable = True
                except Exception as exc:  # pragma: no cover - defensive boundary
                    failure_kind = type(exc).__name__
                    retryable = False

                if not retryable or attempt == self._max_attempts:
                    _logger.warning(
                        "Webhook delivery failed after %d attempt(s): %s",
                        attempt,
                        failure_kind,
                    )
                    return

                _logger.warning(
                    "Webhook delivery attempt %d/%d failed: %s; retrying",
                    attempt,
                    self._max_attempts,
                    failure_kind,
                )
                delay = min(
                    self._retry_backoff_seconds * (2 ** (attempt - 1)),
                    MAX_WEBHOOK_RETRY_BACKOFF_SECONDS,
                )
                if delay > 0:
                    time.sleep(delay)

        try:
            return self._executor.submit(_send)
        except RuntimeError:
            _logger.warning("Webhook executor is shut down; dropping notification")
            return None

    def close(self, wait: bool = True) -> None:
        """Shut down the notifier's thread pool.

        Without this, every `WebhookNotifier` instance leaks its worker
        threads for the life of the process once it goes out of scope.
        """
        self._executor.shutdown(wait=wait)


class _DisabledNotifier:
    def notify(self, _snapshot: SnapshotRecord, _detections: list[Detection]) -> None:
        pass

    def close(self, wait: bool = True) -> None:
        pass


DISABLED_NOTIFIER: _DisabledNotifier = _DisabledNotifier()
