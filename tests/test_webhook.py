from __future__ import annotations

import http.client as http_client
import json
import logging
import math
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import replace
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


def _successful_response(status: int = 204) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.read.return_value = b""
    return response


def test_notifier_posts_when_detection_above_threshold(notifier):
    snapshot = _snapshot(captured_at=datetime(2026, 5, 23, 11, 0, tzinfo=timezone.utc))
    detections = [_detection(0.9, track_id=4)]

    connection = MagicMock()
    connection.getresponse.return_value = _successful_response()

    with patch("script.webhook.http_client.HTTPConnection", return_value=connection):
        future = notifier.notify(snapshot, detections)
        if future is not None:
            future.result(timeout=5.0)

    connection.request.assert_called_once()
    payload = json.loads(connection.request.call_args.kwargs["body"])
    assert payload["camera_id"] == "cam-a"
    assert payload["snapshot_relative_path"] == "cam-a/images/img.jpg"
    assert payload["score"] == 0.9
    assert payload["track_id"] == 4
    assert "2026-05-23" in payload["detected_at"]


def test_notifier_reuses_http_connection_for_sequential_notifications():
    notifier = WebhookNotifier(
        "http://example.com/hook",
        max_attempts=1,
        retry_backoff_seconds=0,
    )
    connection = MagicMock()
    response = MagicMock(status=204)
    response.read.return_value = b""
    connection.getresponse.return_value = response
    try:
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection) as factory:
            first = notifier.notify(_snapshot(), [_detection(0.9)])
            assert first is not None
            first.result(timeout=5.0)
            second = notifier.notify(_snapshot(), [_detection(0.9)])
            assert second is not None
            second.result(timeout=5.0)

        factory.assert_called_once_with("example.com", 80, timeout=10)
        assert connection.request.call_count == 2
        assert connection.getresponse.call_count == 2
    finally:
        notifier.close()


def test_notifier_uses_tls_connection_for_https_webhook():
    notifier = WebhookNotifier("https://example.com/hook")
    connection = MagicMock()
    connection.getresponse.return_value = _successful_response()
    try:
        with patch("script.webhook.http_client.HTTPSConnection", return_value=connection) as factory:
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            assert future is not None
            future.result(timeout=5.0)

        factory.assert_called_once()
        assert factory.call_args.args[:2] == ("example.com", 443)
        assert factory.call_args.kwargs["timeout"] == 10
        assert "context" in factory.call_args.kwargs
    finally:
        notifier.close()


def test_notifier_uses_stored_at_when_no_captured_at(notifier):
    snapshot = _snapshot(captured_at=None)

    connection = MagicMock()
    connection.getresponse.return_value = _successful_response()

    with patch("script.webhook.http_client.HTTPConnection", return_value=connection):
        future = notifier.notify(snapshot, [_detection(0.8)])
        if future is not None:
            future.result(timeout=5.0)

    payload = json.loads(connection.request.call_args.kwargs["body"])
    assert "2026-05-23T12:00:00" in payload["detected_at"]


def test_notifier_skips_when_all_detections_below_threshold():
    high_threshold_notifier = WebhookNotifier("http://example.com/hook", min_score=0.8)
    try:
        with patch("script.webhook.http_client.HTTPConnection") as mock_open:
            high_threshold_notifier.notify(_snapshot(), [_detection(0.6)])

        mock_open.assert_not_called()
    finally:
        high_threshold_notifier.close()


def test_notifier_skips_when_no_detections(notifier):
    with patch("script.webhook.http_client.HTTPConnection") as mock_open:
        notifier.notify(_snapshot(), [])

    mock_open.assert_not_called()


def test_notifier_logs_warning_on_delivery_failure(notifier, caplog):
    with caplog.at_level(logging.WARNING, logger="script.webhook"):
        connection = MagicMock()
        connection.request.side_effect = urllib.error.URLError("conn refused")
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection):
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
        connection = MagicMock()
        connection.getresponse.side_effect = [
            _successful_response(503),
            _successful_response(503),
            _successful_response(),
        ]
        with (
            patch("script.webhook.http_client.HTTPConnection", return_value=connection) as factory,
            patch("script.webhook.time.sleep") as mock_sleep,
        ):
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            assert future is not None
            future.result(timeout=5.0)

        assert factory.call_count == 3
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
        connection = MagicMock()
        connection.request.side_effect = [OSError("connection reset"), None]
        connection.getresponse.return_value = _successful_response()
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection) as factory:
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            assert future is not None
            future.result(timeout=5.0)

        assert factory.call_count == 2
    finally:
        notifier.close()


def test_notifier_retries_timeout_then_succeeds():
    notifier = WebhookNotifier(
        "http://example.com/hook",
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    try:
        connection = MagicMock()
        connection.request.side_effect = [TimeoutError(), None]
        connection.getresponse.return_value = _successful_response()
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection) as factory:
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            assert future is not None
            future.result(timeout=5.0)

        assert factory.call_count == 2
    finally:
        notifier.close()


def test_notifier_does_not_retry_http_4xx():
    notifier = WebhookNotifier(
        "http://example.com/hook",
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    try:
        connection = MagicMock()
        connection.getresponse.return_value = _successful_response(400)
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection) as factory:
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            assert future is not None
            future.result(timeout=5.0)

        assert factory.call_count == 1
    finally:
        notifier.close()


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_notifier_treats_redirect_as_delivery_failure(status, caplog):
    notifier = WebhookNotifier(
        "http://example.com/hook",
        max_attempts=3,
        retry_backoff_seconds=0,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="script.webhook"):
            connection = MagicMock()
            connection.getresponse.return_value = _successful_response(status)
            with patch("script.webhook.http_client.HTTPConnection", return_value=connection) as factory:
                future = notifier.notify(_snapshot(), [_detection(0.9)])
                assert future is not None
                future.result(timeout=5.0)

        assert factory.call_count == 1
        assert connection.request.call_count == 1
        assert any(f"http_{status}" in record.message for record in caplog.records)
    finally:
        notifier.close()


def test_notifier_retries_http_exception_from_reused_connection():
    notifier = WebhookNotifier(
        "http://example.com/hook",
        max_attempts=2,
        retry_backoff_seconds=0,
    )
    try:
        connection = MagicMock()
        connection.request.side_effect = [http_client.HTTPException("stale connection"), None]
        connection.getresponse.return_value = _successful_response()
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection) as factory:
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            assert future is not None
            future.result(timeout=5.0)

        assert factory.call_count == 2
        assert connection.request.call_count == 2
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
            connection = MagicMock()
            connection.getresponse.return_value = _successful_response(503)
            with patch("script.webhook.http_client.HTTPConnection", return_value=connection) as factory:
                future = notifier.notify(_snapshot(), [_detection(0.9)])
                assert future is not None
                future.result(timeout=5.0)

        assert factory.call_count == 3
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

    def blocked_request(*_args, **_kwargs):
        started.set()
        release.wait(timeout=5.0)

    try:
        connection = MagicMock()
        connection.request.side_effect = blocked_request
        connection.getresponse.return_value = _successful_response()
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection):
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
    with (
        caplog.at_level(logging.WARNING, logger="script.webhook"),
        patch("script.webhook.http_client.HTTPConnection") as mock_open,
    ):
        result = closable_notifier.notify(_snapshot(), [_detection(0.9)])

    assert result is None
    mock_open.assert_not_called()
    assert any("executor is shut down" in r.message for r in caplog.records)


@pytest.mark.parametrize("budgets", [{"max_pending": 1},
                                    {"max_pending": 10, "max_payload_bytes": 512,
                                     "max_pending_bytes": 512}])
def test_saturation_bounds_count_and_bytes_without_waiting(budgets):
    notifier = WebhookNotifier("http://example.com/hook", max_workers=1, max_attempts=1,
                               **budgets)
    release = threading.Event()
    connection = MagicMock()
    connection.request.side_effect = lambda *a, **kw: release.wait(timeout=10)
    connection.getresponse.return_value = _successful_response()
    futures = []
    try:
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection):
            start = time.monotonic()
            for _ in range(20):
                future = notifier.notify(_snapshot(), [_detection(.9)])
                if future is not None:
                    futures.append(future)
            assert time.monotonic() - start < 1
            status = notifier.budget_status()
            assert 0 < status["pending"] <= budgets["max_pending"]
            assert 0 < status["pending_bytes"] <= budgets.get("max_pending_bytes", 65536)
            assert status["dropped_full"] == 20 - len(futures) > 0
            release.set()
            for future in futures:
                future.result(timeout=5)
        assert notifier.budget_status()["pending"] == 0
        assert notifier.budget_status()["pending_bytes"] == 0
    finally:
        release.set()
        notifier.close()


def test_oversized_payload_does_not_enter_executor(notifier):
    snapshot = replace(_snapshot(), relative_path="猫" * 2000)
    with patch.object(notifier._executor, "submit") as submit:
        assert notifier.notify(snapshot, [_detection(.9)]) is None
        submit.assert_not_called()
    assert notifier.budget_status()["dropped_oversize"] == 1


@pytest.mark.parametrize("body_size,closed", [(65536, False), (65537, True)])
def test_response_body_is_bounded_and_oversize_is_not_retried(body_size, closed):
    notifier = WebhookNotifier("http://example.com/hook", max_attempts=3)
    connection = MagicMock()
    response = _successful_response(200)
    response.read.return_value = b"x" * body_size
    connection.getresponse.return_value = response
    try:
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection):
            future = notifier.notify(_snapshot(), [_detection(.9)])
            future.result(timeout=5)
        response.read.assert_called_once_with(65537)
        assert connection.request.call_count == 1
        assert connection.close.called == closed
        assert notifier.budget_status()["pending_bytes"] == 0
    finally:
        notifier.close()


def test_cancellation_shutdown_and_completion_release_reservations():
    notifier = WebhookNotifier("http://example.com/hook", max_workers=1, max_pending=2,
                               max_attempts=1)
    started, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        started.set()
        release.wait(timeout=10)
    connection = MagicMock()
    connection.request.side_effect = blocked
    connection.getresponse.return_value = _successful_response()
    try:
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection):
            first = notifier.notify(_snapshot(), [_detection(.9)])
            assert started.wait(timeout=1)
            second = notifier.notify(_snapshot(), [_detection(.9)])
            assert second.cancel()
            assert notifier.budget_status()["pending"] == 1
            third = notifier.notify(_snapshot(), [_detection(.9)])
            notifier.close(wait=False)
            assert third.cancelled()
            assert notifier.budget_status()["pending"] == 1
            release.set()
            first.result(timeout=5)
        assert notifier.budget_status()["pending"] == 0
        assert notifier.budget_status()["pending_bytes"] == 0
    finally:
        release.set()
        notifier.close()


def test_submit_failure_releases_reserved_bytes(notifier):
    with patch.object(notifier._executor, "submit", side_effect=RuntimeError("shutdown")):
        assert notifier.notify(_snapshot(), [_detection(.9)]) is None
    assert notifier.budget_status()["pending_bytes"] == 0
    assert notifier.budget_status()["pending"] == 0
    assert notifier.budget_status()["dropped_closed"] == 1


def test_concurrent_producers_cannot_overbook_the_global_queue():
    notifier = WebhookNotifier("http://example.com/hook", max_workers=1, max_pending=3,
                               max_attempts=1)
    release = threading.Event()
    connection = MagicMock()
    connection.request.side_effect = lambda *a, **kw: release.wait(timeout=10)
    connection.getresponse.return_value = _successful_response()
    try:
        with patch("script.webhook.http_client.HTTPConnection", return_value=connection):
            with ThreadPoolExecutor(max_workers=8) as producers:
                results = list(producers.map(lambda _: notifier.notify(_snapshot(), [_detection(.9)]),
                                             range(80)))
            admitted = [future for future in results if future is not None]
            assert len(admitted) == notifier.budget_status()["pending"] == 3
            assert notifier.budget_status()["dropped_full"] == 77
            release.set()
            for future in admitted:
                future.result(timeout=5)
        assert notifier.budget_status()["pending_bytes"] == 0
        assert notifier.budget_status()["pending"] == 0
    finally:
        release.set()
        notifier.close()


@pytest.mark.parametrize("budget", ["max_pending", "max_pending_bytes", "max_payload_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_notification_budgets_fail_before_executor_creation(budget, value):
    with patch("script.webhook.ThreadPoolExecutor") as executor:
        with pytest.raises(ValueError):
            WebhookNotifier("http://example.com/hook", **{budget: value})
        executor.assert_not_called()
