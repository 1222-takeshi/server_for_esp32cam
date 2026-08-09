from __future__ import annotations

import pytest

from script.app import create_app
from script.detector import Detection
from script.tracking import ObjectTracker


def _detection(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    label: str = "person",
    score: float = 0.9,
) -> Detection:
    return Detection(label=label, score=score, x1=x1, y1=y1, x2=x2, y2=y2)


def test_assigns_same_id_to_overlapping_detection():
    tracker = ObjectTracker(iou_threshold=0.3)

    first = tracker.update("cam-a", [_detection(0.1, 0.1, 0.4, 0.4)])
    second = tracker.update("cam-a", [_detection(0.12, 0.12, 0.42, 0.42)])

    assert first[0].track_id == 1
    assert second[0].track_id == 1


def test_track_ids_are_isolated_per_camera():
    tracker = ObjectTracker()

    camera_a = tracker.update("cam-a", [_detection(0.1, 0.1, 0.4, 0.4)])
    camera_b = tracker.update("cam-b", [_detection(0.1, 0.1, 0.4, 0.4)])

    assert camera_a[0].track_id == 1
    assert camera_b[0].track_id == 1


def test_iou_below_threshold_creates_a_new_track():
    tracker = ObjectTracker(iou_threshold=0.5)

    first = tracker.update("cam-a", [_detection(0.1, 0.1, 0.3, 0.3)])
    second = tracker.update("cam-a", [_detection(0.7, 0.7, 0.9, 0.9)])

    assert first[0].track_id == 1
    assert second[0].track_id == 2


def test_track_survives_configured_missed_frames_then_expires():
    tracker = ObjectTracker(max_missed_frames=1)
    detection = _detection(0.1, 0.1, 0.4, 0.4)

    assert tracker.update("cam-a", [detection])[0].track_id == 1
    assert tracker.update("cam-a", []) == []
    assert tracker.update("cam-a", [detection])[0].track_id == 1
    assert tracker.update("cam-a", []) == []
    assert tracker.update("cam-a", []) == []
    assert tracker.update("cam-a", [detection])[0].track_id == 2


def test_equal_iou_matching_prefers_the_lower_track_id():
    tracker = ObjectTracker(iou_threshold=0.1)
    tracker.update("cam-a", [_detection(0.0, 0.0, 0.4, 1.0), _detection(0.6, 0.0, 1.0, 1.0)])

    matched = tracker.update("cam-a", [_detection(0.3, 0.0, 0.7, 1.0)])

    assert matched[0].track_id == 1


def test_invalid_bbox_is_not_tracked_and_does_not_consume_track_id():
    tracker = ObjectTracker()

    invalid = tracker.update("cam-a", [_detection(-0.1, 0.1, 0.4, 0.4)])
    valid = tracker.update("cam-a", [_detection(0.1, 0.1, 0.4, 0.4)])

    assert invalid[0].track_id is None
    assert valid[0].track_id == 1


def test_max_tracks_per_camera_leaves_excess_detection_untracked():
    tracker = ObjectTracker(max_tracks_per_camera=1)

    tracked = tracker.update(
        "cam-a",
        [_detection(0.1, 0.1, 0.3, 0.3), _detection(0.7, 0.7, 0.9, 0.9)],
    )

    assert [d.track_id for d in tracked] == [1, None]


def test_camera_state_is_evicted_when_camera_limit_is_reached():
    tracker = ObjectTracker(max_cameras=1)
    detection = _detection(0.1, 0.1, 0.4, 0.4)

    assert tracker.update("cam-a", [detection])[0].track_id == 1
    assert tracker.update("cam-b", [detection])[0].track_id == 1
    assert tracker.update("cam-a", [detection])[0].track_id == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"iou_threshold": -0.1},
        {"iou_threshold": 1.1},
        {"max_missed_frames": -1},
        {"max_tracks_per_camera": 0},
        {"max_cameras": 0},
    ],
)
def test_tracker_rejects_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        ObjectTracker(**kwargs)


def test_app_constructs_tracker_only_when_enabled(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "UPLOAD_DIR": tmp_path / "uploads",
            "DB_PATH": tmp_path / "state.sqlite3",
            "INFERENCE_MODE": "sync",
            "RATELIMIT_ENABLED": False,
            "TRACKING_ENABLED": True,
            "TRACKING_IOU_THRESHOLD": 0.6,
            "TRACKING_MAX_MISSED_FRAMES": 3,
            "TRACKING_MAX_TRACKS_PER_CAMERA": 4,
            "TRACKING_MAX_CAMERAS": 2,
        }
    )

    tracker = app.extensions["object_tracker"]
    assert tracker is not None
    assert tracker.iou_threshold == 0.6
    assert tracker.max_missed_frames == 3
    assert tracker.max_tracks_per_camera == 4
    assert tracker.max_cameras == 2
