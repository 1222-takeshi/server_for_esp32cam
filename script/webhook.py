from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import Future, ThreadPoolExecutor
import http.client as http_client
import ssl
import threading
import urllib.error
import urllib.request
from datetime import timezone
from urllib.parse import urlparse

from script.detector import Detection
from script.metadata import SnapshotRecord

_logger = logging.getLogger(__name__)
MAX_WEBHOOK_ATTEMPTS = 10
MAX_WEBHOOK_RETRY_BACKOFF_SECONDS = 60.0


class _WebhookHTTPError(Exception):
    def __init__(self, status: int) -> None:
        self.code = status
        super().__init__(f"HTTP {status}")


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
        if parsed.hostname is None:
            raise ValueError("ESP32CAM_WEBHOOK_URL must include a valid host")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("ESP32CAM_WEBHOOK_URL must include a valid port") from exc
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
        self._parsed_url = parsed
        self._request_target = parsed.path or "/"
        if parsed.query:
            self._request_target += f"?{parsed.query}"
        self._connection_local = threading.local()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def _connection(self):
        connection = getattr(self._connection_local, "connection", None)
        if connection is None:
            host = self._parsed_url.hostname
            port = self._parsed_url.port
            if self._parsed_url.scheme == "https":
                connection = http_client.HTTPSConnection(
                    host,
                    port or 443,
                    timeout=10,
                    context=ssl.create_default_context(),
                )
            else:
                connection = http_client.HTTPConnection(host, port or 80, timeout=10)
            self._connection_local.connection = connection
        return connection

    def _close_connection(self) -> None:
        connection = getattr(self._connection_local, "connection", None)
        if connection is not None:
            try:
                connection.close()
            finally:
                del self._connection_local.connection

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
                "track_id": best.track_id,
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
                    connection = self._connection()
                    connection.request(
                        req.get_method(),
                        self._request_target,
                        body=req.data,
                        headers=dict(req.header_items()),
                    )
                    response = connection.getresponse()
                    response.read()
                    if response.status >= 300:
                        raise _WebhookHTTPError(response.status)
                    return
                except _WebhookHTTPError as exc:
                    failure_kind = f"http_{exc.code}"
                    retryable = 500 <= exc.code <= 599
                except urllib.error.HTTPError as exc:
                    failure_kind = f"http_{exc.code}"
                    retryable = 500 <= exc.code <= 599
                except (urllib.error.URLError, http_client.HTTPException, OSError) as exc:
                    failure_kind = type(exc).__name__
                    retryable = True
                except Exception as exc:  # pragma: no cover - defensive boundary
                    failure_kind = type(exc).__name__
                    retryable = False

                self._close_connection()

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
