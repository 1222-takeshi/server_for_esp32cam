from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading

from script.detector import Detection
from script.storage import StoredImage


@dataclass(frozen=True)
class DetectionRecord:
    label: str
    score: float
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True)
class SnapshotRecord:
    id: int
    filename: str
    relative_path: str
    size: int
    camera_id: str | None
    captured_at: datetime | None
    stored_at: datetime
    inference_status: str
    inference_error: str | None
    detections: tuple[DetectionRecord, ...] = ()


@dataclass(frozen=True)
class CameraRecord:
    camera_id: str
    display_name: str
    expected_interval_seconds: int
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool


@dataclass(frozen=True)
class CameraSummary:
    camera: CameraRecord
    latest_snapshot: SnapshotRecord | None
    latest_detection_at: datetime | None
    status: str


ALLOWED_LOG_LEVELS = frozenset({"info", "warning", "error", "critical"})


@dataclass(frozen=True)
class DeviceLogRecord:
    id: int
    camera_id: str
    level: str
    message: str
    logged_at: datetime


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _camera_status(
    last_seen_at: datetime | None,
    expected_interval_seconds: int,
    *,
    now: datetime | None = None,
) -> str:
    if last_seen_at is None:
        return "offline"

    reference = now or datetime.now(tz=timezone.utc)
    age = reference - last_seen_at
    interval = max(expected_interval_seconds, 1)
    if age <= timedelta(seconds=interval * 2):
        return "healthy"
    if age <= timedelta(seconds=interval * 6):
        return "stale"
    return "offline"


class SQLiteMetadataStore:
    def __init__(self, db_path: str | Path, *, default_expected_interval_seconds: int = 300):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._default_expected_interval_seconds = default_expected_interval_seconds
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cameras (
                    camera_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    expected_interval_seconds INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL UNIQUE,
                    relative_path TEXT NOT NULL UNIQUE,
                    size INTEGER NOT NULL,
                    camera_id TEXT,
                    captured_at TEXT,
                    stored_at TEXT NOT NULL,
                    inference_status TEXT NOT NULL,
                    inference_error TEXT,
                    FOREIGN KEY(camera_id) REFERENCES cameras(camera_id)
                );

                CREATE TABLE IF NOT EXISTS detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    score REAL NOT NULL,
                    x1 REAL NOT NULL,
                    y1 REAL NOT NULL,
                    x2 REAL NOT NULL,
                    y2 REAL NOT NULL,
                    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS device_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_id TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    logged_at TEXT NOT NULL,
                    FOREIGN KEY(camera_id) REFERENCES cameras(camera_id) ON DELETE CASCADE
                );
                """
            )

    def _snapshot_from_row(self, row: sqlite3.Row, detections: tuple[DetectionRecord, ...] = ()) -> SnapshotRecord:
        return SnapshotRecord(
            id=row["id"],
            filename=row["filename"],
            relative_path=row["relative_path"],
            size=row["size"],
            camera_id=row["camera_id"],
            captured_at=_parse_datetime(row["captured_at"]),
            stored_at=_parse_datetime(row["stored_at"]) or datetime.now(tz=timezone.utc),
            inference_status=row["inference_status"],
            inference_error=row["inference_error"],
            detections=detections,
        )

    def _camera_from_row(self, row: sqlite3.Row) -> CameraRecord:
        return CameraRecord(
            camera_id=row["camera_id"],
            display_name=row["display_name"],
            expected_interval_seconds=row["expected_interval_seconds"],
            first_seen_at=_parse_datetime(row["first_seen_at"]) or datetime.now(tz=timezone.utc),
            last_seen_at=_parse_datetime(row["last_seen_at"]) or datetime.now(tz=timezone.utc),
            is_active=bool(row["is_active"]),
        )

    def ensure_camera(self, camera_id: str, *, seen_at: datetime | None = None) -> CameraRecord:
        effective_seen_at = (seen_at or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
        seen_iso = effective_seen_at.isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO cameras (
                    camera_id, display_name, expected_interval_seconds, first_seen_at, last_seen_at, is_active
                ) VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(camera_id) DO UPDATE SET
                    first_seen_at=CASE
                        WHEN excluded.first_seen_at < cameras.first_seen_at THEN excluded.first_seen_at
                        ELSE cameras.first_seen_at
                    END,
                    last_seen_at=CASE
                        WHEN excluded.last_seen_at > cameras.last_seen_at THEN excluded.last_seen_at
                        ELSE cameras.last_seen_at
                    END,
                    is_active=1
                """,
                (
                    camera_id,
                    camera_id,
                    self._default_expected_interval_seconds,
                    seen_iso,
                    seen_iso,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM cameras WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
        return self._camera_from_row(row)

    def record_snapshot(self, stored: StoredImage, *, default_pending: bool = True) -> SnapshotRecord:
        if stored.device_id is not None:
            self.ensure_camera(stored.device_id, seen_at=stored.created_at)

        inference_status = "pending" if default_pending and stored.device_id else "succeeded"
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO snapshots (
                    filename, relative_path, size, camera_id, captured_at, stored_at, inference_status, inference_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(filename) DO UPDATE SET
                    relative_path=excluded.relative_path,
                    size=excluded.size,
                    camera_id=excluded.camera_id,
                    captured_at=excluded.captured_at,
                    stored_at=excluded.stored_at
                """,
                (
                    stored.name,
                    stored.relative_path,
                    stored.size,
                    stored.device_id,
                    stored.captured_at.isoformat() if stored.captured_at else None,
                    stored.created_at.isoformat(),
                    inference_status,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM snapshots WHERE filename = ?",
                (stored.name,),
            ).fetchone()
        return self._snapshot_from_row(row)

    def sync_storage_index(self, images: list[StoredImage]) -> None:
        for image in images:
            self.record_snapshot(image)

    def delete_snapshots_by_relative_paths(self, relative_paths: list[str]) -> int:
        if not relative_paths:
            return 0
        placeholders = ",".join("?" for _ in relative_paths)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"DELETE FROM snapshots WHERE relative_path IN ({placeholders})",
                tuple(relative_paths),
            )
        return int(cursor.rowcount)

    def total_snapshot_count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS count FROM snapshots").fetchone()
        return int(row["count"])

    def list_recent_snapshots(self, *, limit: int | None = None) -> list[SnapshotRecord]:
        query = "SELECT * FROM snapshots ORDER BY stored_at DESC"
        params: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        detections = self._detections_by_snapshot_id([row["id"] for row in rows])
        return [self._snapshot_from_row(row, detections.get(row["id"], ())) for row in rows]

    def get_camera(self, camera_id: str) -> CameraRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM cameras WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
        if row is None:
            return None
        return self._camera_from_row(row)

    def list_camera_summaries(self) -> list[CameraSummary]:
        with self._lock:
            camera_rows = self._connection.execute(
                "SELECT * FROM cameras ORDER BY last_seen_at DESC, camera_id ASC"
            ).fetchall()

        summaries: list[CameraSummary] = []
        for row in camera_rows:
            camera = self._camera_from_row(row)
            snapshots = self.list_camera_snapshots(camera.camera_id, limit=1)
            latest_detection = self._latest_detection_time(camera.camera_id)
            summaries.append(
                CameraSummary(
                    camera=camera,
                    latest_snapshot=snapshots[0] if snapshots else None,
                    latest_detection_at=latest_detection,
                    status=_camera_status(camera.last_seen_at, camera.expected_interval_seconds),
                )
            )
        return summaries

    def list_camera_snapshots(self, camera_id: str, *, limit: int = 20) -> list[SnapshotRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM snapshots
                WHERE camera_id = ?
                ORDER BY stored_at DESC
                LIMIT ?
                """,
                (camera_id, limit),
            ).fetchall()
        detections = self._detections_by_snapshot_id([row["id"] for row in rows])
        return [self._snapshot_from_row(row, detections.get(row["id"], ())) for row in rows]

    def list_detections(
        self,
        *,
        camera_id: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        query = """
            SELECT
                detections.label,
                detections.score,
                detections.x1,
                detections.y1,
                detections.x2,
                detections.y2,
                snapshots.filename,
                snapshots.camera_id,
                snapshots.captured_at,
                snapshots.stored_at
            FROM detections
            JOIN snapshots ON snapshots.id = detections.snapshot_id
            WHERE 1 = 1
        """
        params: list[object] = []
        if camera_id is not None:
            query += " AND snapshots.camera_id = ?"
            params.append(camera_id)
        if since is not None:
            query += " AND snapshots.stored_at >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        query += " ORDER BY snapshots.stored_at DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._connection.execute(query, tuple(params)).fetchall()

        return [
            {
                "label": row["label"],
                "score": row["score"],
                "bbox": [row["x1"], row["y1"], row["x2"], row["y2"]],
                "filename": row["filename"],
                "camera_id": row["camera_id"],
                "captured_at": row["captured_at"],
                "stored_at": row["stored_at"],
            }
            for row in rows
        ]

    def list_pending_snapshot_ids(self, *, limit: int = 100) -> list[int]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id
                FROM snapshots
                WHERE inference_status = 'pending'
                ORDER BY stored_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def reset_processing_snapshots(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE snapshots
                SET inference_status = 'pending', inference_error = NULL
                WHERE inference_status = 'processing'
                """
            )

    def get_snapshot(self, snapshot_id: int) -> SnapshotRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        detections = self._detections_by_snapshot_id([snapshot_id])
        return self._snapshot_from_row(row, detections.get(snapshot_id, ()))

    def mark_snapshot_processing(self, snapshot_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE snapshots
                SET inference_status = 'processing', inference_error = NULL
                WHERE id = ?
                """,
                (snapshot_id,),
            )

    def mark_snapshot_skipped(self, snapshot_id: int, *, reason: str | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE snapshots
                SET inference_status = 'skipped', inference_error = ?
                WHERE id = ?
                """,
                (reason, snapshot_id),
            )
            self._connection.execute(
                "DELETE FROM detections WHERE snapshot_id = ?",
                (snapshot_id,),
            )

    def mark_snapshot_result(
        self,
        snapshot_id: int,
        *,
        detections: list[Detection],
        error: str | None = None,
    ) -> None:
        status = "failed" if error else "succeeded"
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE snapshots
                SET inference_status = ?, inference_error = ?
                WHERE id = ?
                """,
                (status, error, snapshot_id),
            )
            self._connection.execute(
                "DELETE FROM detections WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            for detection in detections:
                self._connection.execute(
                    """
                    INSERT INTO detections (
                        snapshot_id, label, score, x1, y1, x2, y2
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        detection.label,
                        detection.score,
                        detection.x1,
                        detection.y1,
                        detection.x2,
                        detection.y2,
                    ),
                )

    def _latest_detection_time(self, camera_id: str) -> datetime | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT snapshots.stored_at
                FROM detections
                JOIN snapshots ON snapshots.id = detections.snapshot_id
                WHERE snapshots.camera_id = ?
                ORDER BY snapshots.stored_at DESC
                LIMIT 1
                """,
                (camera_id,),
            ).fetchone()
        if row is None:
            return None
        return _parse_datetime(row["stored_at"])

    def _detections_by_snapshot_id(
        self,
        snapshot_ids: list[int],
    ) -> dict[int, tuple[DetectionRecord, ...]]:
        if not snapshot_ids:
            return {}

        placeholders = ",".join("?" for _ in snapshot_ids)
        query = f"""
            SELECT snapshot_id, label, score, x1, y1, x2, y2
            FROM detections
            WHERE snapshot_id IN ({placeholders})
            ORDER BY id ASC
        """
        with self._lock:
            rows = self._connection.execute(query, tuple(snapshot_ids)).fetchall()

        grouped: dict[int, list[DetectionRecord]] = {snapshot_id: [] for snapshot_id in snapshot_ids}
        for row in rows:
            grouped[row["snapshot_id"]].append(
                DetectionRecord(
                    label=row["label"],
                    score=row["score"],
                    x1=row["x1"],
                    y1=row["y1"],
                    x2=row["x2"],
                    y2=row["y2"],
                )
            )
        return {
            snapshot_id: tuple(records)
            for snapshot_id, records in grouped.items()
        }

    def record_heartbeat(self, camera_id: str, *, seen_at: datetime | None = None) -> CameraRecord:
        return self.ensure_camera(camera_id, seen_at=seen_at)

    def record_device_log(
        self,
        camera_id: str,
        *,
        level: str,
        message: str,
        logged_at: datetime | None = None,
    ) -> DeviceLogRecord:
        if level not in ALLOWED_LOG_LEVELS:
            raise ValueError(f"Invalid log level: {level!r}. Must be one of {sorted(ALLOWED_LOG_LEVELS)}")
        self.ensure_camera(camera_id)
        effective_logged_at = (logged_at or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO device_logs (camera_id, level, message, logged_at) VALUES (?, ?, ?, ?)",
                (camera_id, level, message, effective_logged_at.isoformat()),
            )
            row = self._connection.execute(
                "SELECT * FROM device_logs WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return DeviceLogRecord(
            id=row["id"],
            camera_id=row["camera_id"],
            level=row["level"],
            message=row["message"],
            logged_at=_parse_datetime(row["logged_at"]) or effective_logged_at,
        )

    def list_device_logs(self, camera_id: str, *, limit: int = 50) -> list[DeviceLogRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM device_logs WHERE camera_id = ? ORDER BY logged_at DESC LIMIT ?",
                (camera_id, limit),
            ).fetchall()
        return [
            DeviceLogRecord(
                id=row["id"],
                camera_id=row["camera_id"],
                level=row["level"],
                message=row["message"],
                logged_at=_parse_datetime(row["logged_at"]) or datetime.now(tz=timezone.utc),
            )
            for row in rows
        ]
