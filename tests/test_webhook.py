from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from script.detector import Detection
from script.metadata import SnapshotRecord
from script.webhook import DISABLED_NOTIFIER, WebhookNotifier


@pytest.fixture()
def notifier():
    instance = WebhookNotifier(
        "http://example.com/hook",
        min_score=0.5,
        max_attempts=1,
        retry_backoff_seconds=0,
    )
    yield instance
    instance.close()


def _snapshot(*, captured_at=None) -> SnapshotRecord:
    stored_at = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    return SnapshotRecord(
        id=1,
        filename="img.jpg",
        relative_path="cam-a/images/img.jpg",
        size=100,
        camera_id="cam-a",
        captured_at=captured_at,
        stored_at=stored_at,
        inference_status="succeeded",
        inference_error=None,
    )


def _detection(score: float, *, track_id: int | None = None) -> Detection:
    return Detection(
        label="person",
        score=score,
        x1=0.1,
        y1=0.1,
        x2=0.9,
        y2=0.9,
        track_id=track_id,
    )


def _successful_response() -> MagicMock:
    response = MagicMock()
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    return response


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://example.com/hook?secret=hidden",
        status,
        "request failed",
        {},
        None,
    )


def test_notifier_posts_when_detection_above_threshold(notifier):
    snapshot = _snapshot(captured_at=datetime(2026, 5, 23, 11, 0, tzinfo=timezone.utc))
    detections = [_detection(0.9, track_id=4)]

    mock_response = _successful_response()

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
        future = notifier.notify(snapshot, detections)
        if future is not None:
            future.result(timeout=5.0)

    mock_open.assert_called_once()
    req = mock_open.call_args[0][0]
    payload = json.loads(req.data)
    assert payload["camera_id"] == "cam-a"
    assert payload["snapshot_relative_path"] == "cam-a/images/img.jpg"
    assert payload["score"] == 0.9
    assert payload["track_id"] == 4
    assert "2026-05-23" in payload["detected_at"]


def test_notifier_uses_stored_at_when_no_captured_at(notifier):
    snapshot = _snapshot(captured_at=None)

    mock_response = _successful_response()

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
        future = notifier.notify(snapshot, [_detection(0.8)])
        if future is not None:
            future.result(timeout=5.0)

    req = mock_open.call_args[0][0]
    payload = json.loads(req.data)
    assert "2026-05-23T12:00:00" in payload["detected_at"]


def test_notifier_skips_when_all_detections_below_threshold():
    high_threshold_notifier = WebhookNotifier("http://example.com/hook", min_score=0.8)
    try:
        with patch("urllib.request.urlopen") as mock_open:
            high_threshold_notifier.notify(_snapshot(), [_detection(0.6)])

        mock_open.assert_not_called()
    finally:
        high_threshold_notifier.close()


def test_notifier_skips_when_no_detections(notifier):
    with patch("urllib.request.urlopen") as mock_open:
        notifier.notify(_snapshot(), [])

    mock_open.assert_not_called()


def test_notifier_logs_warning_on_delivery_failure(notifier, caplog):
    with caplog.at_level(logging.WARNING, logger="script.webhook"):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("conn refused")):
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            if future is not None:
                future.result(timeout=5.0)

    assert any("Webhook delivery failed" in r.message for r in caplog.records)


def test_notifier_retries_http_5xx_then_succeeds():
    notifier = WebhookNotifier(
        "http://example.com/hook",
        max_attempts=3,
        retry_backoff_seconds=1.0,
    )
    try:
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=[_http_error(503), _http_error(503), _successful_response()],
            ) as mock_open,
            patch("script.webhook.time.sleep") as mock_sleep,
        ):
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            assert future is not None
            future.result(timeout=5.0)

        assert mock_open.call_count == 3
        assert mock_sleep.call_args_list == [call(1.0), call(2.0)]
    finally:
        notifier.close()


def test_notifier_retries_transport_error_then_succeeds():
    notifier = WebhookNotifier(
        "http://example.com/hook",
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    try:
        with patch(
            "urllib.request.urlopen",
            side_effect=[OSError("connection reset"), _successful_response()],
        ) as mock_open:
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            assert future is not None
            future.result(timeout=5.0)

        assert mock_open.call_count == 2
    finally:
        notifier.close()


def test_notifier_retries_timeout_then_succeeds():
    notifier = WebhookNotifier(
        "http://example.com/hook",
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    try:
        with patch(
            "urllib.request.urlopen",
            side_effect=[TimeoutError(), _successful_response()],
        ) as mock_open:
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            assert future is not None
            future.result(timeout=5.0)

        assert mock_open.call_count == 2
    finally:
        notifier.close()


def test_notifier_does_not_retry_http_4xx():
    notifier = WebhookNotifier(
        "http://example.com/hook",
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    try:
        with patch("urllib.request.urlopen", side_effect=_http_error(400)) as mock_open:
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            assert future is not None
            future.result(timeout=5.0)

        assert mock_open.call_count == 1
    finally:
        notifier.close()


def test_notifier_logs_final_failure_without_url_or_payload(caplog):
    notifier = WebhookNotifier(
        "http://example.com/hook?secret=hidden",
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="script.webhook"):
            with patch("urllib.request.urlopen", side_effect=_http_error(503)) as mock_open:
                future = notifier.notify(_snapshot(), [_detection(0.9)])
                assert future is not None
                future.result(timeout=5.0)

        assert mock_open.call_count == 3
        messages = " ".join(record.message for record in caplog.records)
        assert "example.com" not in messages
        assert "secret=hidden" not in messages
        assert "cam-a" not in messages
        assert "3 attempt(s)" in messages
    finally:
        notifier.close()


def test_notifier_validates_retry_settings():
    with pytest.raises(ValueError, match="max_attempts"):
        WebhookNotifier("http://example.com/hook", max_attempts=0)
    with pytest.raises(ValueError, match="max_attempts"):
        WebhookNotifier("http://example.com/hook", max_attempts=11)

    with pytest.raises(ValueError, match="retry_backoff_seconds"):
        WebhookNotifier("http://example.com/hook", retry_backoff_seconds=-1)
    with pytest.raises(ValueError, match="retry_backoff_seconds"):
        WebhookNotifier("http://example.com/hook", retry_backoff_seconds=math.nan)
    with pytest.raises(ValueError, match="retry_backoff_seconds"):
        WebhookNotifier("http://example.com/hook", retry_backoff_seconds=math.inf)
    with pytest.raises(ValueError, match="retry_backoff_seconds"):
        WebhookNotifier("http://example.com/hook", retry_backoff_seconds=60.1)


def test_notifier_rejects_invalid_url_without_leaking_secret():
    with pytest.raises(ValueError) as exc_info:
        WebhookNotifier("ftp://user:secret@example.com/hook")

    assert "secret" not in str(exc_info.value)
    assert "example.com" not in str(exc_info.value)


def test_notifier_submits_delivery_without_blocking_caller():
    notifier = WebhookNotifier("http://example.com/hook", max_attempts=1)
    started = threading.Event()
    release = threading.Event()

    def blocked_urlopen(*_args, **_kwargs):
        started.set()
        release.wait(timeout=5.0)
        return _successful_response()

    try:
        with patch("urllib.request.urlopen", side_effect=blocked_urlopen):
            started_at = time.monotonic()
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            elapsed = time.monotonic() - started_at

            assert future is not None
            assert started.wait(timeout=1.0)
            assert elapsed < 1.0
            assert not future.done()
            release.set()
            future.result(timeout=5.0)
    finally:
        release.set()
        notifier.close()


def test_create_app_passes_webhook_retry_config(tmp_path):
    from script.app import create_app

    with patch("script.app.WebhookNotifier") as notifier_class:
        create_app(
            {
                "TESTING": True,
                "UPLOAD_DIR": tmp_path / "uploads",
                "DB_PATH": tmp_path / "esp32cam.sqlite3",
                "INFERENCE_MODE": "sync",
                "RATELIMIT_ENABLED": False,
                "WEBHOOK_URL": "http://example.com/hook",
                "WEBHOOK_MIN_SCORE": 0.7,
                "WEBHOOK_MAX_ATTEMPTS": 4,
                "WEBHOOK_RETRY_BACKOFF_SECONDS": 0.25,
            }
        )

    notifier_class.assert_called_once_with(
        "http://example.com/hook",
        min_score=0.7,
        max_attempts=4,
        retry_backoff_seconds=0.25,
    )


def test_disabled_notifier_does_nothing():
    DISABLED_NOTIFIER.notify(_snapshot(), [_detection(0.99)])
    DISABLED_NOTIFIER.close()


def test_close_shuts_down_executor_and_drops_further_notifications(caplog):
    closable_notifier = WebhookNotifier("http://example.com/hook", min_score=0.5)
    closable_notifier.close()

    import logging
    with caplog.at_level(logging.WARNING, logger="script.webhook"):
        with patch("urllib.request.urlopen") as mock_open:
            result = closable_notifier.notify(_snapshot(), [_detection(0.9)])

    assert result is None
    mock_open.assert_not_called()
    assert any("executor is shut down" in r.message for r in caplog.records)
