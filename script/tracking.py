from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace

from script.detector import Detection

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class _Track:
    track_id: int
    label: str
    bbox: BBox
    missed_frames: int = 0


def _bbox_from_detection(detection: Detection) -> BBox:
    return (detection.x1, detection.y1, detection.x2, detection.y2)


def _is_valid_bbox(bbox: BBox) -> bool:
    return (
        all(math.isfinite(value) for value in bbox)
        and 0.0 <= bbox[0] < bbox[2] <= 1.0
        and 0.0 <= bbox[1] < bbox[3] <= 1.0
    )


def bbox_iou(left: BBox, right: BBox) -> float:
    """Return the intersection-over-union of two normalized bounding boxes."""

    if not _is_valid_bbox(left) or not _is_valid_bbox(right):
        return 0.0

    intersection_width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    intersection_height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    intersection = intersection_width * intersection_height
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0.0 else 0.0


class ObjectTracker:
    """Bounded, deterministic, per-camera greedy IoU object tracker."""

    def __init__(
        self,
        *,
        iou_threshold: float = 0.3,
        max_missed_frames: int = 2,
        max_tracks_per_camera: int = 50,
        max_cameras: int = 500,
    ) -> None:
        if not math.isfinite(iou_threshold) or not 0.0 <= iou_threshold <= 1.0:
            raise ValueError("iou_threshold must be finite and between 0.0 and 1.0")
        if not isinstance(max_missed_frames, int) or max_missed_frames < 0:
            raise ValueError("max_missed_frames must be a non-negative integer")
        if not isinstance(max_tracks_per_camera, int) or max_tracks_per_camera < 1:
            raise ValueError("max_tracks_per_camera must be a positive integer")
        if not isinstance(max_cameras, int) or max_cameras < 1:
            raise ValueError("max_cameras must be a positive integer")

        self.iou_threshold = iou_threshold
        self.max_missed_frames = max_missed_frames
        self.max_tracks_per_camera = max_tracks_per_camera
        self.max_cameras = max_cameras
        self._tracks_by_camera: OrderedDict[str, dict[int, _Track]] = OrderedDict()
        self._next_ids: dict[str, int] = {}
        self._lock = threading.RLock()

    def update(self, camera_id: str, detections: list[Detection]) -> list[Detection]:
        """Attach track IDs to detections while keeping state bounded per camera."""

        with self._lock:
            tracks = self._tracks_for_camera(camera_id)
            valid_indices = [
                index
                for index, detection in enumerate(detections)
                if _is_valid_bbox(_bbox_from_detection(detection))
            ]
            candidates = self._matching_candidates(detections, valid_indices, tracks)
            matched_detections: dict[int, int] = {}
            matched_track_indices: dict[int, int] = {}
            matched_tracks: set[int] = set()
            for _iou, track_id, detection_index in candidates:
                if track_id in matched_tracks or detection_index in matched_detections:
                    continue
                matched_tracks.add(track_id)
                matched_detections[detection_index] = track_id
                matched_track_indices[track_id] = detection_index

            next_tracks: dict[int, _Track] = {}
            for track_id, track in tracks.items():
                detection_index = matched_track_indices.get(track_id)
                if detection_index is not None:
                    detection = detections[detection_index]
                    next_tracks[track_id] = _Track(
                        track_id=track_id,
                        label=detection.label,
                        bbox=_bbox_from_detection(detection),
                    )
                elif track.missed_frames + 1 <= self.max_missed_frames:
                    next_tracks[track_id] = replace(
                        track, missed_frames=track.missed_frames + 1
                    )

            unmatched_indices = [
                index for index in valid_indices if index not in matched_detections
            ]
            unmatched_indices.sort(key=lambda index: self._detection_sort_key(detections[index], index))
            for detection_index in unmatched_indices:
                if len(next_tracks) >= self.max_tracks_per_camera:
                    break
                detection = detections[detection_index]
                track_id = self._next_track_id(camera_id)
                next_tracks[track_id] = _Track(
                    track_id=track_id,
                    label=detection.label,
                    bbox=_bbox_from_detection(detection),
                )
                matched_detections[detection_index] = track_id

            self._tracks_by_camera[camera_id] = next_tracks
            self._tracks_by_camera.move_to_end(camera_id)
            return [
                replace(detection, track_id=matched_detections.get(index))
                for index, detection in enumerate(detections)
            ]

    def _tracks_for_camera(self, camera_id: str) -> dict[int, _Track]:
        tracks = self._tracks_by_camera.get(camera_id)
        if tracks is not None:
            self._tracks_by_camera.move_to_end(camera_id)
            return tracks

        if len(self._tracks_by_camera) >= self.max_cameras:
            evicted_camera_id, _ = self._tracks_by_camera.popitem(last=False)
            self._next_ids.pop(evicted_camera_id, None)
        tracks = {}
        self._tracks_by_camera[camera_id] = tracks
        return tracks

    def _matching_candidates(
        self,
        detections: list[Detection],
        valid_indices: list[int],
        tracks: dict[int, _Track],
    ) -> list[tuple[float, int, int]]:
        candidates = []
        for detection_index in valid_indices:
            detection = detections[detection_index]
            bbox = _bbox_from_detection(detection)
            for track_id, track in tracks.items():
                if track.label != detection.label:
                    continue
                iou = bbox_iou(track.bbox, bbox)
                if iou >= self.iou_threshold:
                    candidates.append((iou, track_id, detection_index))
        return sorted(
            candidates,
            key=lambda candidate: (
                -candidate[0],
                candidate[1],
                self._detection_sort_key(detections[candidate[2]], candidate[2]),
            ),
        )

    @staticmethod
    def _detection_sort_key(detection: Detection, index: int) -> tuple[object, ...]:
        return (
            detection.label,
            detection.x1,
            detection.y1,
            detection.x2,
            detection.y2,
            -detection.score,
            index,
        )

    def _next_track_id(self, camera_id: str) -> int:
        track_id = self._next_ids.get(camera_id, 1)
        self._next_ids[camera_id] = track_id + 1
        return track_id
