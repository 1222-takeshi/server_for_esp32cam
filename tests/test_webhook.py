from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from script.detector import Detection
from script.metadata import SnapshotRecord
from script.webhook import DISABLED_NOTIFIER, WebhookNotifier


@pytest.fixture()
def notifier():
    instance = WebhookNotifier("http://example.com/hook", min_score=0.5)
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


def _detection(score: float) -> Detection:
    return Detection(label="person", score=score, x1=0.1, y1=0.1, x2=0.9, y2=0.9)


def test_notifier_posts_when_detection_above_threshold(notifier):
    snapshot = _snapshot(captured_at=datetime(2026, 5, 23, 11, 0, tzinfo=timezone.utc))
    detections = [_detection(0.9)]

    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

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
    assert "2026-05-23" in payload["detected_at"]


def test_notifier_uses_stored_at_when_no_captured_at(notifier):
    snapshot = _snapshot(captured_at=None)

    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

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
    import logging
    with caplog.at_level(logging.WARNING, logger="script.webhook"):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("conn refused")):
            future = notifier.notify(_snapshot(), [_detection(0.9)])
            if future is not None:
                future.result(timeout=5.0)

    assert any("Webhook delivery failed" in r.message for r in caplog.records)


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
