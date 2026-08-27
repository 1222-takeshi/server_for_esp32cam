from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading

from script.detector import Detection
from script.storage import StoredImage, StoredVideo


_MIN_RETURNING_SQLITE_VERSION = (3, 35, 0)


@dataclass(frozen=True)
class DetectionRecord:
    label: str
    score: float
    x1: float
    y1: float
    x2: float
    y2: float
    track_id: int | None = None


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
class VideoRecord:
    id: int
    filename: str
    relative_path: str
    size: int
    camera_id: str
    captured_at: datetime | None
    stored_at: datetime
    duration_ms: int | None
    content_type: str
    sha256: str | None
    processing_status: str
    processing_error: str | None


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

# SQLite has a default limit on the number of bound parameters per statement
# (SQLITE_MAX_VARIABLE_NUMBER, historically 999). Every `IN (...)` clause
# built from a caller-supplied list (filenames, relative_paths, camera_ids,
# snapshot_ids) must chunk against this limit instead of assuming the list
# stays small -- a multi-camera fleet or a large retention sweep can easily
# exceed 999 items, which previously raised sqlite3.OperationalError
# ("too many SQL variables") instead of degrading gracefully.
_SQLITE_IN_CLAUSE_CHUNK_SIZE = 500
# Page size for the keyset-paginated retention candidate scan below --
# distinct from the IN-clause chunk size above (this bounds one SELECT's
# result set, not the number of bound parameters).
_RETENTION_LOOKUP_BATCH_SIZE = 500


def _chunked(items: list[object], size: int) -> list[list[object]]:
    """Split `items` into consecutive chunks of at most `size` elements."""
    return [items[offset : offset + size] for offset in range(0, len(items), size)]


@dataclass(frozen=True)
class DeviceLogRecord:
    id: int
    camera_id: str
    level: str
    message: str
    logged_at: datetime


@dataclass(frozen=True)
class DesiredConfigRecord:
    camera_id: str
    version: int
    config: dict[str, object]
    updated_at: datetime
    updated_by: str
    last_acknowledged_version: int | None
    last_acknowledged_at: datetime | None
    last_acknowledged_message: str | None
    last_acknowledged_success: bool | None
    last_firmware_version: str | None
    last_free_heap: int | None


class DesiredConfigVersionConflict(Exception):
    def __init__(self, current_version: int, expected_version: int):
        self.current_version = current_version
        self.expected_version = expected_version
        super().__init__(
            f"expected_version {expected_version} does not match current version {current_version}"
        )


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
    # Each OS thread (and, under a monkey-patched gevent worker, each
    # greenlet -- see _get_connection() below) gets its own sqlite3
    # connection, so concurrent writers are common: an upload's
    # record_snapshot()/apply_upload_retention() can race a retention scan
    # or another camera's upload. WAL mode still serializes writers, and
    # without busy_timeout set, SQLite's default is to raise "database is
    # locked" (SQLITE_BUSY) immediately instead of retrying -- turning a
    # transient lock into a failed request (and, on /upload, a wasted
    # device wake/retry cycle) instead of a few milliseconds of wait.
    _BUSY_TIMEOUT_MS = 5000

    def __init__(self, db_path: str | Path, *, default_expected_interval_seconds: int = 300):
        if sqlite3.sqlite_version_info < _MIN_RETURNING_SQLITE_VERSION:
            detected_version = ".".join(str(part) for part in sqlite3.sqlite_version_info)
            raise RuntimeError(
                "SQLite 3.35.0 or newer is required for metadata writes; "
                f"detected {detected_version}"
            )

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._default_expected_interval_seconds = default_expected_interval_seconds
        self._local = threading.local()

        old_mask = os.umask(0o177)
        try:
            init_conn = sqlite3.connect(self.db_path, check_same_thread=True)
        finally:
            os.umask(old_mask)
        os.chmod(self.db_path, 0o600)
        try:
            init_conn.row_factory = sqlite3.Row
            init_conn.execute("PRAGMA journal_mode = WAL")
            init_conn.execute("PRAGMA synchronous = NORMAL")
            init_conn.execute(f"PRAGMA busy_timeout = {self._BUSY_TIMEOUT_MS}")
            with init_conn:
                init_conn.execute("PRAGMA foreign_keys = ON")
            self._initialize_schema(init_conn)
            self._migrate_schema(init_conn)
            with init_conn:
                init_conn.execute("PRAGMA foreign_keys = ON")
        finally:
            init_conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {self._BUSY_TIMEOUT_MS}")
            self._local.connection = conn
        return conn

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        # device_logs was introduced with ON DELETE CASCADE from the start, so only
        # snapshots needs a migration for databases created before this change.
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='snapshots'"
        ).fetchone()
        if row is not None and "ON DELETE CASCADE" not in row["sql"].upper():
            with conn:
                conn.execute(
                    """
                    CREATE TABLE snapshots_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        filename TEXT NOT NULL UNIQUE,
                        relative_path TEXT NOT NULL UNIQUE,
                        size INTEGER NOT NULL,
                        camera_id TEXT,
                        captured_at TEXT,
                        stored_at TEXT NOT NULL,
                        inference_status TEXT NOT NULL,
                        inference_error TEXT,
                        FOREIGN KEY(camera_id) REFERENCES cameras(camera_id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute("INSERT INTO snapshots_new SELECT * FROM snapshots")
                conn.execute("DROP TABLE snapshots")
                conn.execute("ALTER TABLE snapshots_new RENAME TO snapshots")

        detection_columns = {
            column["name"]
            for column in conn.execute("PRAGMA table_info(detections)").fetchall()
        }
        if "track_id" not in detection_columns:
            with conn:
                conn.execute("ALTER TABLE detections ADD COLUMN track_id INTEGER")

    def _initialize_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
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
                    FOREIGN KEY(camera_id) REFERENCES cameras(camera_id) ON DELETE CASCADE
                );



                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL UNIQUE,
                    relative_path TEXT NOT NULL UNIQUE,
                    size INTEGER NOT NULL,
                    camera_id TEXT NOT NULL,
                    captured_at TEXT,
                    stored_at TEXT NOT NULL,
                    duration_ms INTEGER,
                    content_type TEXT NOT NULL,
                    sha256 TEXT,
                    processing_status TEXT NOT NULL DEFAULT 'skipped',
                    processing_error TEXT,
                    FOREIGN KEY(camera_id) REFERENCES cameras(camera_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_videos_camera_stored_at
                    ON videos(camera_id, stored_at DESC);
                CREATE INDEX IF NOT EXISTS idx_videos_stored_at
                    ON videos(stored_at DESC);

                CREATE TABLE IF NOT EXISTS detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    score REAL NOT NULL,
                    x1 REAL NOT NULL,
                    y1 REAL NOT NULL,
                    x2 REAL NOT NULL,
                    y2 REAL NOT NULL,
                    track_id INTEGER,
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

                CREATE INDEX IF NOT EXISTS idx_snapshots_camera_stored_at
                    ON snapshots(camera_id, stored_at DESC);
                CREATE INDEX IF NOT EXISTS idx_snapshots_stored_at
                    ON snapshots(stored_at DESC);
                CREATE INDEX IF NOT EXISTS idx_snapshots_inference_status
                    ON snapshots(inference_status);

                CREATE INDEX IF NOT EXISTS idx_detections_snapshot_id
                    ON detections(snapshot_id);

                CREATE INDEX IF NOT EXISTS idx_device_logs_camera_logged_at
                    ON device_logs(camera_id, logged_at DESC);

                CREATE INDEX IF NOT EXISTS idx_detections_snapshot_score
                    ON detections(snapshot_id, score DESC);

                CREATE INDEX IF NOT EXISTS idx_device_logs_level_logged_at
                    ON device_logs(level, logged_at DESC);

                CREATE INDEX IF NOT EXISTS idx_snapshots_inference_pending
                    ON snapshots(stored_at ASC)
                    WHERE inference_status = 'pending';

                CREATE TABLE IF NOT EXISTS camera_desired_configs (
                    camera_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL DEFAULT 'admin',
                    last_acknowledged_version INTEGER,
                    last_acknowledged_at TEXT,
                    last_acknowledged_message TEXT,
                    last_acknowledged_success INTEGER,
                    last_firmware_version TEXT,
                    last_free_heap INTEGER,
                    FOREIGN KEY(camera_id) REFERENCES cameras(camera_id) ON DELETE CASCADE
                );
                """
        )

    def _snapshot_from_row(
        self, row: sqlite3.Row, detections: tuple[DetectionRecord, ...] = ()
    ) -> SnapshotRecord:
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

    def _video_from_row(self, row: sqlite3.Row) -> VideoRecord:
        return VideoRecord(
            id=row["id"],
            filename=row["filename"],
            relative_path=row["relative_path"],
            size=row["size"],
            camera_id=row["camera_id"],
            captured_at=_parse_datetime(row["captured_at"]),
            stored_at=_parse_datetime(row["stored_at"]) or datetime.now(tz=timezone.utc),
            duration_ms=row["duration_ms"],
            content_type=row["content_type"],
            sha256=row["sha256"],
            processing_status=row["processing_status"],
            processing_error=row["processing_error"],
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

    def _upsert_camera_row(
        self, conn: sqlite3.Connection, camera_id: str, seen_at: datetime | None
    ) -> sqlite3.Row:
        """Upsert a camera row without opening its own transaction.

        Callers that need to write a related row (a snapshot, video, or
        device log) in the same commit should call this directly inside
        their own ``with conn:`` block instead of going through
        `ensure_camera`, which commits on its own and would otherwise turn
        one logical write into two separate SQLite transactions.
        """
        effective_seen_at = (seen_at or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
        seen_iso = effective_seen_at.isoformat()
        return conn.execute(
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
            RETURNING *
            """,
            (
                camera_id,
                camera_id,
                self._default_expected_interval_seconds,
                seen_iso,
                seen_iso,
            ),
        ).fetchone()

    def ensure_camera(self, camera_id: str, *, seen_at: datetime | None = None) -> CameraRecord:
        conn = self._get_connection()
        with conn:
            row = self._upsert_camera_row(conn, camera_id, seen_at)
        return self._camera_from_row(row)

    def record_snapshot(
        self, stored: StoredImage, *, default_pending: bool = True
    ) -> SnapshotRecord:
        inference_status = "pending" if default_pending and stored.device_id else "succeeded"
        conn = self._get_connection()
        with conn:
            if stored.device_id is not None:
                self._upsert_camera_row(conn, stored.device_id, stored.created_at)
            row = conn.execute(
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
                RETURNING *
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
            ).fetchone()
        return self._snapshot_from_row(row)

    def record_video(self, stored: StoredVideo) -> VideoRecord:
        conn = self._get_connection()
        with conn:
            self._upsert_camera_row(conn, stored.camera_id, stored.stored_at)
            row = conn.execute(
                """
                INSERT INTO videos (
                    filename, relative_path, size, camera_id, captured_at, stored_at,
                    duration_ms, content_type, sha256, processing_status, processing_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'skipped', NULL)
                ON CONFLICT(filename) DO UPDATE SET
                    relative_path=excluded.relative_path,
                    size=excluded.size,
                    camera_id=excluded.camera_id,
                    captured_at=excluded.captured_at,
                    stored_at=excluded.stored_at,
                    duration_ms=excluded.duration_ms,
                    content_type=excluded.content_type,
                    sha256=excluded.sha256
                RETURNING *
                """,
                (
                    stored.name,
                    stored.relative_path,
                    stored.size,
                    stored.camera_id,
                    stored.captured_at.isoformat() if stored.captured_at else None,
                    stored.stored_at.isoformat(),
                    stored.duration_ms,
                    stored.content_type,
                    stored.sha256,
                ),
            ).fetchone()
        return self._video_from_row(row)

    def get_video(self, video_id: int, *, camera_id: str | None = None) -> VideoRecord | None:
        query = "SELECT * FROM videos WHERE id = ?"
        params: list[object] = [video_id]
        if camera_id is not None:
            query += " AND camera_id = ?"
            params.append(camera_id)
        conn = self._get_connection()
        row = conn.execute(query, tuple(params)).fetchone()
        if row is None:
            return None
        return self._video_from_row(row)

    def list_camera_videos(
        self,
        camera_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        before: int | None = None,
    ) -> list[VideoRecord]:
        query = "SELECT * FROM videos WHERE camera_id = ?"
        params: list[object] = [camera_id]
        if before is not None:
            query += " AND id < ?"
            params.append(before)
        query += " ORDER BY stored_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        conn = self._get_connection()
        rows = conn.execute(query, tuple(params)).fetchall()
        return [self._video_from_row(row) for row in rows]

    def list_videos(
        self,
        *,
        camera_id: str | None = None,
        limit: int = 50,
        before: int | None = None,
    ) -> list[VideoRecord]:
        query = "SELECT * FROM videos WHERE 1=1"
        params: list[object] = []
        if camera_id is not None:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if before is not None:
            query += " AND id < ?"
            params.append(before)
        query += " ORDER BY stored_at DESC, id DESC LIMIT ?"
        params.append(limit)
        conn = self._get_connection()
        rows = conn.execute(query, tuple(params)).fetchall()
        return [self._video_from_row(row) for row in rows]

    def delete_videos_by_relative_paths(self, relative_paths: list[str]) -> int:
        if not relative_paths:
            return 0
        conn = self._get_connection()
        deleted = 0
        with conn:
            for chunk in _chunked(relative_paths, _SQLITE_IN_CLAUSE_CHUNK_SIZE):
                placeholders = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"DELETE FROM videos WHERE relative_path IN ({placeholders})",
                    tuple(chunk),
                )
                deleted += cursor.rowcount
        return deleted

    def _total_retention_bytes(self, table: str) -> int:
        conn = self._get_connection()
        total_row = conn.execute(
            f"SELECT COALESCE(SUM(size), 0) AS total FROM {table}"
        ).fetchone()
        return int(total_row["total"])

    def _iter_retention_candidates(
        self,
        table: str,
        *,
        exclude_relative_paths: set[str] | None = None,
        older_than: datetime | None = None,
        batch_size: int = _RETENTION_LOOKUP_BATCH_SIZE,
    ) -> Iterator[tuple[str, int, datetime]]:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        conn = self._get_connection()
        exclude = exclude_relative_paths or set()
        cursor_stored_at: str | None = None
        cursor_id = 0

        while True:
            conditions: list[str] = []
            params: list[object] = []
            if older_than is not None:
                conditions.append("stored_at < ?")
                params.append(older_than.isoformat())
            if cursor_stored_at is not None:
                conditions.append("(stored_at > ? OR (stored_at = ? AND id > ?))")
                params.extend([cursor_stored_at, cursor_stored_at, cursor_id])

            query = f"SELECT id, relative_path, size, stored_at FROM {table}"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY stored_at ASC, id ASC LIMIT ?"
            params.append(batch_size)
            rows = conn.execute(query, tuple(params)).fetchall()
            if not rows:
                return

            for row in rows:
                if row["relative_path"] in exclude:
                    continue
                yield (
                    row["relative_path"],
                    row["size"],
                    _parse_datetime(row["stored_at"]) or datetime.now(tz=timezone.utc),
                )

            last_row = rows[-1]
            cursor_stored_at = last_row["stored_at"]
            cursor_id = last_row["id"]
            if len(rows) < batch_size:
                return

    def iter_snapshot_retention_candidates(
        self,
        *,
        exclude_relative_paths: set[str] | None = None,
        older_than: datetime | None = None,
        batch_size: int = _RETENTION_LOOKUP_BATCH_SIZE,
    ) -> Iterator[tuple[str, int, datetime]]:
        return self._iter_retention_candidates(
            "snapshots",
            exclude_relative_paths=exclude_relative_paths,
            older_than=older_than,
            batch_size=batch_size,
        )

    def iter_video_retention_candidates(
        self,
        *,
        exclude_relative_paths: set[str] | None = None,
        older_than: datetime | None = None,
        batch_size: int = _RETENTION_LOOKUP_BATCH_SIZE,
    ) -> Iterator[tuple[str, int, datetime]]:
        return self._iter_retention_candidates(
            "videos",
            exclude_relative_paths=exclude_relative_paths,
            older_than=older_than,
            batch_size=batch_size,
        )

    def list_snapshot_retention_candidates(
        self, *, exclude_relative_paths: set[str] | None = None
    ) -> tuple[list[tuple[str, int, datetime]], int]:
        """Snapshot rows for retention decisions, sourced from the index
        instead of a filesystem walk (#200 / #225 / #246 / #260).

        Returns ``(candidates, total_bytes)``: ``candidates`` is
        ``(relative_path, size, stored_at)`` for every indexed snapshot
        except ``exclude_relative_paths`` (the file that just triggered the
        scan, which retention must never evict on its own upload), ordered
        oldest-first by ``stored_at`` so callers can evict from the front
        until they're back under quota/age limits. ``total_bytes`` sums
        *every* indexed snapshot's size, including excluded ones, matching
        what a full `list_images()` walk would have summed.
        """
        candidates = list(
            self.iter_snapshot_retention_candidates(
                exclude_relative_paths=exclude_relative_paths,
            )
        )
        return candidates, self._total_retention_bytes("snapshots")

    def list_video_retention_candidates(
        self, *, exclude_relative_paths: set[str] | None = None
    ) -> tuple[list[tuple[str, int, datetime]], int]:
        """Video counterpart of list_snapshot_retention_candidates()."""
        candidates = list(
            self.iter_video_retention_candidates(
                exclude_relative_paths=exclude_relative_paths,
            )
        )
        return candidates, self._total_retention_bytes("videos")

    def sync_storage_index(
        self,
        images: list[StoredImage],
        *,
        hydrate_metadata: Callable[[str], tuple[str | None, datetime | None]] | None = None,
    ) -> None:
        if not images:
            return
        conn = self._get_connection()
        # Only check whether *these* on-disk filenames already exist in the
        # index, instead of materializing the entire (potentially huge)
        # snapshots table into a Python set. `filename` is UNIQUE/indexed,
        # so each chunked lookup is an indexed query, not a table scan, and
        # the memory/row count is bounded by the on-disk listing rather than
        # the historical size of the snapshots table.
        names = [img.name for img in images]
        existing: set[str] = set()
        for chunk in _chunked(names, _SQLITE_IN_CLAUSE_CHUNK_SIZE):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT filename FROM snapshots WHERE filename IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            existing.update(row[0] for row in rows)
        new_images = [img for img in images if img.name not in existing]
        if not new_images:
            return
        # `images` may have been listed with include_metadata=False (the
        # caller only needs `name` for the existence check above) — hydrate
        # device_id/captured_at here, one metadata-sidecar read per image,
        # but only for the handful that turn out to actually be new instead
        # of every image on disk.
        if hydrate_metadata is not None:
            hydrated = []
            for img in new_images:
                device_id, captured_at = hydrate_metadata(img.relative_path)
                hydrated.append(replace(img, device_id=device_id, captured_at=captured_at))
            new_images = hydrated
        for img in new_images:
            if img.device_id is not None:
                self.ensure_camera(img.device_id, seen_at=img.created_at)
        rows = [
            (
                img.name,
                img.relative_path,
                img.size,
                img.device_id,
                img.captured_at.isoformat() if img.captured_at else None,
                img.created_at.isoformat(),
                "succeeded",
            )
            for img in new_images
        ]
        conn = self._get_connection()
        with conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO snapshots
                    (filename, relative_path, size, camera_id, captured_at, stored_at, inference_status, inference_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                rows,
            )

    def delete_snapshots_by_relative_paths(self, relative_paths: list[str]) -> int:
        if not relative_paths:
            return 0
        conn = self._get_connection()
        deleted = 0
        with conn:
            for chunk in _chunked(relative_paths, _SQLITE_IN_CLAUSE_CHUNK_SIZE):
                placeholders = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"DELETE FROM snapshots WHERE relative_path IN ({placeholders})",
                    tuple(chunk),
                )
                deleted += cursor.rowcount
        return deleted

    def ping(self) -> None:
        conn = self._get_connection()
        conn.execute("SELECT 1")

    def total_snapshot_count(self) -> int:
        conn = self._get_connection()
        row = conn.execute("SELECT COUNT(*) AS count FROM snapshots").fetchone()
        return int(row["count"])

    def count_snapshots(
        self,
        *,
        camera_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        query = "SELECT COUNT(*) AS count FROM snapshots WHERE 1 = 1"
        params: list[object] = []
        if camera_id is not None:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if since is not None:
            query += " AND stored_at >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        if until is not None:
            query += " AND stored_at <= ?"
            params.append(until.astimezone(timezone.utc).isoformat())
        conn = self._get_connection()
        row = conn.execute(query, tuple(params)).fetchone()
        return int(row["count"])

    def list_recent_snapshots(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        before: tuple[str, int] | None = None,
        camera_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        include_detections: bool = True,
    ) -> list[SnapshotRecord]:
        query = "SELECT * FROM snapshots WHERE 1 = 1"
        params: list[object] = []
        if camera_id is not None:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if since is not None:
            query += " AND stored_at >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        if until is not None:
            query += " AND stored_at <= ?"
            params.append(until.astimezone(timezone.utc).isoformat())
        if before is not None:
            before_stored_at, before_id = before
            query += " AND (stored_at < ? OR (stored_at = ? AND id < ?))"
            params.extend([before_stored_at, before_stored_at, before_id])
        query += " ORDER BY stored_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        conn = self._get_connection()
        rows = conn.execute(query, tuple(params)).fetchall()
        detections = (
            self._detections_by_snapshot_id([row["id"] for row in rows])
            if include_detections
            else {}
        )
        return [self._snapshot_from_row(row, detections.get(row["id"], ())) for row in rows]

    def list_recent_snapshots_page(
        self,
        *,
        limit: int,
        offset: int = 0,
        before: tuple[str, int] | None = None,
        camera_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        include_detections: bool = True,
    ) -> tuple[list[SnapshotRecord], int]:
        """Like list_recent_snapshots(), but also returns the total number
        of matching rows without a separate COUNT(*) round trip (#342).

        Uses SQLite's ``COUNT(*) OVER()`` window function so the total is
        computed by the same query that fetches the page. When the page
        itself comes back empty (e.g. an out-of-range offset) the window
        function has no row to attach the total to, so this falls back to
        a plain count_snapshots() call for that edge case only.
        """
        query = "SELECT *, COUNT(*) OVER() AS total_count FROM snapshots WHERE 1 = 1"
        params: list[object] = []
        if camera_id is not None:
            query += " AND camera_id = ?"
            params.append(camera_id)
        if since is not None:
            query += " AND stored_at >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        if until is not None:
            query += " AND stored_at <= ?"
            params.append(until.astimezone(timezone.utc).isoformat())
        if before is not None:
            before_stored_at, before_id = before
            query += " AND (stored_at < ? OR (stored_at = ? AND id < ?))"
            params.extend([before_stored_at, before_stored_at, before_id])
        query += " ORDER BY stored_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        conn = self._get_connection()
        rows = conn.execute(query, tuple(params)).fetchall()
        if rows:
            total = int(rows[0]["total_count"])
        else:
            total = self.count_snapshots(camera_id=camera_id, since=since, until=until)

        detections = (
            self._detections_by_snapshot_id([row["id"] for row in rows])
            if include_detections
            else {}
        )
        snapshots = [self._snapshot_from_row(row, detections.get(row["id"], ())) for row in rows]
        return snapshots, total

    def get_camera(self, camera_id: str) -> CameraRecord | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM cameras WHERE camera_id = ?",
            (camera_id,),
        ).fetchone()
        if row is None:
            return None
        return self._camera_from_row(row)

    def update_camera(
        self,
        camera_id: str,
        *,
        display_name: str | None = None,
        expected_interval_seconds: int | None = None,
    ) -> CameraRecord | None:
        camera = self.get_camera(camera_id)
        if camera is None:
            return None
        new_display_name = display_name if display_name is not None else camera.display_name
        new_interval = (
            expected_interval_seconds
            if expected_interval_seconds is not None
            else camera.expected_interval_seconds
        )
        conn = self._get_connection()
        with conn:
            conn.execute(
                "UPDATE cameras SET display_name = ?, expected_interval_seconds = ? WHERE camera_id = ?",
                (new_display_name, new_interval, camera_id),
            )
        return self.get_camera(camera_id)

    def delete_camera(self, camera_id: str) -> bool:
        conn = self._get_connection()
        with conn:
            cursor = conn.execute(
                "DELETE FROM cameras WHERE camera_id = ?",
                (camera_id,),
            )
        return cursor.rowcount > 0

    def list_camera_last_seen(self) -> list[tuple[str, datetime | None]]:
        """Lightweight camera_id/last_seen_at listing for /metrics.

        Unlike list_camera_summaries(), this skips the per-camera latest
        snapshot and detection lookups (window function + join scans) that
        callers who only need the last-seen timestamp don't use.
        """
        conn = self._get_connection()
        rows = conn.execute("SELECT camera_id, last_seen_at FROM cameras").fetchall()
        return [(row["camera_id"], _parse_datetime(row["last_seen_at"])) for row in rows]

    def count_cameras(self) -> int:
        conn = self._get_connection()
        row = conn.execute("SELECT COUNT(*) AS count FROM cameras").fetchone()
        return int(row["count"])

    def list_camera_summaries(
        self, *, limit: int | None = None, offset: int = 0
    ) -> list[CameraSummary]:
        conn = self._get_connection()
        query = "SELECT * FROM cameras ORDER BY last_seen_at DESC, camera_id ASC"
        params: list[int] = []
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        camera_rows = conn.execute(query, tuple(params)).fetchall()

        if not camera_rows:
            return []

        camera_ids = [row["camera_id"] for row in camera_rows]

        # camera_ids can grow past SQLite's bound-parameter limit for a
        # larger camera fleet, so both lookups below are chunked
        # (_SQLITE_IN_CLAUSE_CHUNK_SIZE) and their results merged, instead of
        # binding every camera_id into a single IN (...) clause.
        snapshot_rows = []
        detection_rows = []
        for chunk in _chunked(camera_ids, _SQLITE_IN_CLAUSE_CHUNK_SIZE):
            placeholders = ",".join("?" for _ in chunk)
            snapshot_rows.extend(
                conn.execute(
                    f"""
                    SELECT *
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (PARTITION BY camera_id ORDER BY stored_at DESC) AS rn
                        FROM snapshots
                        WHERE camera_id IN ({placeholders})
                    ) ranked
                    WHERE rn = 1
                    """,
                    chunk,
                ).fetchall()
            )
            detection_rows.extend(
                conn.execute(
                    f"""
                    SELECT snapshots.camera_id, MAX(snapshots.stored_at) AS latest_detection_at
                    FROM detections
                    JOIN snapshots ON snapshots.id = detections.snapshot_id
                    WHERE snapshots.camera_id IN ({placeholders})
                    GROUP BY snapshots.camera_id
                    """,
                    chunk,
                ).fetchall()
            )

        detections_by_id = self._detections_by_snapshot_id([row["id"] for row in snapshot_rows])
        latest_snapshot_by_camera: dict[str, SnapshotRecord] = {
            row["camera_id"]: self._snapshot_from_row(row, detections_by_id.get(row["id"], ()))
            for row in snapshot_rows
        }
        latest_detection_by_camera: dict[str, datetime] = {
            row["camera_id"]: dt
            for row in detection_rows
            if (dt := _parse_datetime(row["latest_detection_at"])) is not None
        }

        summaries: list[CameraSummary] = []
        for row in camera_rows:
            camera = self._camera_from_row(row)
            summaries.append(
                CameraSummary(
                    camera=camera,
                    latest_snapshot=latest_snapshot_by_camera.get(camera.camera_id),
                    latest_detection_at=latest_detection_by_camera.get(camera.camera_id),
                    status=_camera_status(camera.last_seen_at, camera.expected_interval_seconds),
                )
            )
        return summaries

    def count_camera_snapshots(
        self,
        camera_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        return self.count_snapshots(camera_id=camera_id, since=since, until=until)

    def list_camera_snapshots(
        self,
        camera_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        before: tuple[str, int] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[SnapshotRecord]:
        query = "SELECT * FROM snapshots WHERE camera_id = ?"
        params: list[object] = [camera_id]
        if since is not None:
            query += " AND stored_at >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        if until is not None:
            query += " AND stored_at <= ?"
            params.append(until.astimezone(timezone.utc).isoformat())
        if before is not None:
            before_stored_at, before_id = before
            query += " AND (stored_at < ? OR (stored_at = ? AND id < ?))"
            params.extend([before_stored_at, before_stored_at, before_id])
        query += " ORDER BY stored_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        conn = self._get_connection()
        rows = conn.execute(query, tuple(params)).fetchall()
        detections = self._detections_by_snapshot_id([row["id"] for row in rows])
        return [self._snapshot_from_row(row, detections.get(row["id"], ())) for row in rows]

    def list_camera_snapshots_page(
        self,
        camera_id: str,
        *,
        limit: int,
        offset: int = 0,
        before: tuple[str, int] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        include_detections: bool = True,
    ) -> tuple[list[SnapshotRecord], int]:
        """Like list_camera_snapshots(), but also returns the total number
        of matching rows without a separate COUNT(*) round trip (#342).

        `GET /api/cameras/<camera_id>/snapshots` previously called
        count_camera_snapshots() and list_camera_snapshots() back to back on
        every request, so every page load paid for two full scans of the
        camera_id index instead of one. See list_recent_snapshots_page() for
        why COUNT(*) OVER() is used and when it falls back to a plain
        count_camera_snapshots() call (an out-of-range offset leaves the
        window with no row to attach the total to).
        """
        query = "SELECT *, COUNT(*) OVER() AS total_count FROM snapshots WHERE camera_id = ?"
        params: list[object] = [camera_id]
        if since is not None:
            query += " AND stored_at >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        if until is not None:
            query += " AND stored_at <= ?"
            params.append(until.astimezone(timezone.utc).isoformat())
        if before is not None:
            before_stored_at, before_id = before
            query += " AND (stored_at < ? OR (stored_at = ? AND id < ?))"
            params.extend([before_stored_at, before_stored_at, before_id])
        query += " ORDER BY stored_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        conn = self._get_connection()
        rows = conn.execute(query, tuple(params)).fetchall()
        if rows:
            total = int(rows[0]["total_count"])
        else:
            total = self.count_camera_snapshots(camera_id, since=since, until=until)

        detections = (
            self._detections_by_snapshot_id([row["id"] for row in rows])
            if include_detections
            else {}
        )
        snapshots = [self._snapshot_from_row(row, detections.get(row["id"], ())) for row in rows]
        return snapshots, total

    def _detection_where_clause(
        self,
        *,
        camera_id: str | None = None,
        since: datetime | None = None,
        confidence_min: float | None = None,
        before: tuple[str, int, int] | None = None,
    ) -> tuple[str, list[object]]:
        where = "WHERE 1 = 1"
        params: list[object] = []
        if camera_id is not None:
            where += " AND snapshots.camera_id = ?"
            params.append(camera_id)
        if since is not None:
            where += " AND snapshots.stored_at >= ?"
            params.append(since.astimezone(timezone.utc).isoformat())
        if confidence_min is not None:
            where += " AND detections.score >= ?"
            params.append(confidence_min)
        if before is not None:
            before_stored_at, before_snapshot_id, before_detection_id = before
            where += """
                AND (
                    snapshots.stored_at < ?
                    OR (
                        snapshots.stored_at = ?
                        AND (
                            snapshots.id < ?
                            OR (snapshots.id = ? AND detections.id < ?)
                        )
                    )
                )
            """
            params.extend(
                [
                    before_stored_at,
                    before_stored_at,
                    before_snapshot_id,
                    before_snapshot_id,
                    before_detection_id,
                ]
            )
        return where, params

    def count_detections(
        self,
        *,
        camera_id: str | None = None,
        since: datetime | None = None,
        confidence_min: float | None = None,
    ) -> int:
        where, params = self._detection_where_clause(
            camera_id=camera_id, since=since, confidence_min=confidence_min
        )
        query = f"""
            SELECT COUNT(*) AS count
            FROM detections
            JOIN snapshots ON snapshots.id = detections.snapshot_id
            {where}
        """
        conn = self._get_connection()
        row = conn.execute(query, tuple(params)).fetchone()
        return int(row["count"])

    def list_detections(
        self,
        *,
        camera_id: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        before: tuple[str, int, int] | None = None,
        confidence_min: float | None = None,
    ) -> list[dict[str, object]]:
        where, params = self._detection_where_clause(
            camera_id=camera_id,
            since=since,
            confidence_min=confidence_min,
            before=before,
        )
        query = f"""
            SELECT
                detections.id AS detection_id,
                snapshots.id AS snapshot_id,
                detections.label,
                detections.score,
                detections.x1,
                detections.y1,
                detections.x2,
                detections.y2,
                detections.track_id,
                snapshots.filename,
                snapshots.camera_id,
                snapshots.captured_at,
                snapshots.stored_at
            FROM detections
            JOIN snapshots ON snapshots.id = detections.snapshot_id
            {where}
            ORDER BY snapshots.stored_at DESC, snapshots.id DESC, detections.id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        conn = self._get_connection()
        rows = conn.execute(query, tuple(params)).fetchall()

        return [
            {
                "detection_id": row["detection_id"],
                "snapshot_id": row["snapshot_id"],
                "label": row["label"],
                "score": row["score"],
                "bbox": [row["x1"], row["y1"], row["x2"], row["y2"]],
                "track_id": row["track_id"],
                "filename": row["filename"],
                "camera_id": row["camera_id"],
                "captured_at": row["captured_at"],
                "stored_at": row["stored_at"],
            }
            for row in rows
        ]

    def list_detections_page(
        self,
        *,
        camera_id: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        before: tuple[str, int, int] | None = None,
        confidence_min: float | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        """Like list_detections(), but also returns the total number of
        matching rows without a separate COUNT(*) round trip (#342).

        See list_recent_snapshots_page() for why COUNT(*) OVER() is used
        and when it falls back to a plain count_detections() call.
        """
        where, params = self._detection_where_clause(
            camera_id=camera_id,
            since=since,
            confidence_min=confidence_min,
            before=before,
        )
        query = f"""
            SELECT
                detections.id AS detection_id,
                snapshots.id AS snapshot_id,
                detections.label,
                detections.score,
                detections.x1,
                detections.y1,
                detections.x2,
                detections.y2,
                detections.track_id,
                snapshots.filename,
                snapshots.camera_id,
                snapshots.captured_at,
                snapshots.stored_at,
                COUNT(*) OVER() AS total_count
            FROM detections
            JOIN snapshots ON snapshots.id = detections.snapshot_id
            {where}
            ORDER BY snapshots.stored_at DESC, snapshots.id DESC, detections.id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        conn = self._get_connection()
        rows = conn.execute(query, tuple(params)).fetchall()
        if rows:
            total = int(rows[0]["total_count"])
        else:
            total = self.count_detections(
                camera_id=camera_id, since=since, confidence_min=confidence_min
            )

        detections = [
            {
                "detection_id": row["detection_id"],
                "snapshot_id": row["snapshot_id"],
                "label": row["label"],
                "score": row["score"],
                "bbox": [row["x1"], row["y1"], row["x2"], row["y2"]],
                "track_id": row["track_id"],
                "filename": row["filename"],
                "camera_id": row["camera_id"],
                "captured_at": row["captured_at"],
                "stored_at": row["stored_at"],
            }
            for row in rows
        ]
        return detections, total

    def list_pending_snapshot_ids(self, *, limit: int | None = None) -> list[int]:
        conn = self._get_connection()
        query = """
            SELECT id
            FROM snapshots
            WHERE inference_status = 'pending'
            ORDER BY stored_at ASC
        """
        params: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        rows = conn.execute(query, params).fetchall()
        return [int(row["id"]) for row in rows]

    def reset_processing_snapshots(self) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                UPDATE snapshots
                SET inference_status = 'pending', inference_error = NULL
                WHERE inference_status = 'processing'
                """
            )

    def get_snapshot(self, snapshot_id: int) -> SnapshotRecord | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        detections = self._detections_by_snapshot_id([snapshot_id])
        return self._snapshot_from_row(row, detections.get(snapshot_id, ()))

    def get_snapshot_by_filename(self, filename: str) -> SnapshotRecord | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM snapshots WHERE filename = ?",
            (filename,),
        ).fetchone()
        if row is None:
            return None
        return self._snapshot_from_row(row)

    def mark_snapshot_processing(self, snapshot_id: int) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                UPDATE snapshots
                SET inference_status = 'processing', inference_error = NULL
                WHERE id = ?
                """,
                (snapshot_id,),
            )

    def mark_snapshot_skipped(self, snapshot_id: int, *, reason: str | None = None) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                UPDATE snapshots
                SET inference_status = 'skipped', inference_error = ?
                WHERE id = ?
                """,
                (reason, snapshot_id),
            )
            conn.execute(
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
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                UPDATE snapshots
                SET inference_status = ?, inference_error = ?
                WHERE id = ?
                """,
                (status, error, snapshot_id),
            )
            conn.execute(
                "DELETE FROM detections WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            if detections:
                conn.executemany(
                    """
                    INSERT INTO detections (
                        snapshot_id, label, score, x1, y1, x2, y2, track_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            snapshot_id,
                            detection.label,
                            detection.score,
                            detection.x1,
                            detection.y1,
                            detection.x2,
                            detection.y2,
                            detection.track_id,
                        )
                        for detection in detections
                    ],
                )

    def _detections_by_snapshot_id(
        self,
        snapshot_ids: list[int],
    ) -> dict[int, tuple[DetectionRecord, ...]]:
        if not snapshot_ids:
            return {}

        conn = self._get_connection()
        rows = []
        for chunk in _chunked(snapshot_ids, _SQLITE_IN_CLAUSE_CHUNK_SIZE):
            placeholders = ",".join("?" for _ in chunk)
            query = f"""
                SELECT snapshot_id, label, score, x1, y1, x2, y2, track_id
                FROM detections
                WHERE snapshot_id IN ({placeholders})
                ORDER BY id ASC
            """
            rows.extend(conn.execute(query, tuple(chunk)).fetchall())

        grouped: dict[int, list[DetectionRecord]] = {
            snapshot_id: [] for snapshot_id in snapshot_ids
        }
        for row in rows:
            grouped[row["snapshot_id"]].append(
                DetectionRecord(
                    label=row["label"],
                    score=row["score"],
                    x1=row["x1"],
                    y1=row["y1"],
                    x2=row["x2"],
                    y2=row["y2"],
                    track_id=row["track_id"],
                )
            )
        return {snapshot_id: tuple(records) for snapshot_id, records in grouped.items()}

    def _desired_config_from_row(self, row: sqlite3.Row) -> DesiredConfigRecord:
        success_raw = row["last_acknowledged_success"]
        return DesiredConfigRecord(
            camera_id=row["camera_id"],
            version=int(row["version"]),
            config=json.loads(row["config_json"]),
            updated_at=_parse_datetime(row["updated_at"]) or datetime.now(tz=timezone.utc),
            updated_by=row["updated_by"],
            last_acknowledged_version=row["last_acknowledged_version"],
            last_acknowledged_at=_parse_datetime(row["last_acknowledged_at"]),
            last_acknowledged_message=row["last_acknowledged_message"],
            last_acknowledged_success=None if success_raw is None else bool(success_raw),
            last_firmware_version=row["last_firmware_version"],
            last_free_heap=row["last_free_heap"],
        )

    def get_desired_config(self, camera_id: str) -> DesiredConfigRecord | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM camera_desired_configs WHERE camera_id = ?",
            (camera_id,),
        ).fetchone()
        if row is None:
            return None
        return self._desired_config_from_row(row)

    def upsert_desired_config(
        self,
        camera_id: str,
        config: dict[str, object],
        *,
        expected_version: int | None = None,
        updated_by: str = "admin",
    ) -> DesiredConfigRecord:
        now = datetime.now(tz=timezone.utc).isoformat()
        config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
        self.ensure_camera(camera_id)
        conn = self._get_connection()
        with conn:
            current = conn.execute(
                "SELECT version FROM camera_desired_configs WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
            if current is None:
                if expected_version is not None and expected_version != 0:
                    raise DesiredConfigVersionConflict(0, expected_version)
                version = 1
                conn.execute(
                    """
                    INSERT INTO camera_desired_configs (
                        camera_id, version, config_json, updated_at, updated_by
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (camera_id, version, config_json, now, updated_by),
                )
            else:
                current_version = int(current["version"])
                if expected_version is not None and expected_version != current_version:
                    raise DesiredConfigVersionConflict(current_version, expected_version)
                version = current_version + 1
                conn.execute(
                    """
                    UPDATE camera_desired_configs
                    SET version = ?, config_json = ?, updated_at = ?, updated_by = ?
                    WHERE camera_id = ?
                    """,
                    (version, config_json, now, updated_by, camera_id),
                )
        record = self.get_desired_config(camera_id)
        if record is None:  # pragma: no cover
            raise RuntimeError("desired config upsert failed")
        return record

    def record_desired_config_ack(
        self,
        camera_id: str,
        *,
        version: int,
        applied: bool,
        message: str | None = None,
        firmware_version: str | None = None,
        free_heap: int | None = None,
        clear_one_shot_requests: bool = True,
    ) -> DesiredConfigRecord | None:
        now = datetime.now(tz=timezone.utc).isoformat()
        conn = self._get_connection()
        with conn:
            row = conn.execute(
                "SELECT * FROM camera_desired_configs WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
            if row is None:
                return None
            current_version = int(row["version"])
            if current_version != version:
                raise DesiredConfigVersionConflict(current_version, version)
            config = json.loads(row["config_json"])
            if clear_one_shot_requests:
                config["restart_requested"] = False
                config["ota_update_requested"] = False
            config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
            cursor = conn.execute(
                """
                UPDATE camera_desired_configs
                SET config_json = ?,
                    last_acknowledged_version = ?,
                    last_acknowledged_at = ?,
                    last_acknowledged_message = ?,
                    last_acknowledged_success = ?,
                    last_firmware_version = ?,
                    last_free_heap = ?
                WHERE camera_id = ? AND version = ?
                """,
                (
                    config_json,
                    version,
                    now,
                    message,
                    1 if applied else 0,
                    firmware_version,
                    free_heap,
                    camera_id,
                    version,
                ),
            )
            if cursor.rowcount == 0:  # pragma: no cover - defensive against concurrent writers
                latest = conn.execute(
                    "SELECT version FROM camera_desired_configs WHERE camera_id = ?",
                    (camera_id,),
                ).fetchone()
                latest_version = int(latest["version"]) if latest is not None else 0
                raise DesiredConfigVersionConflict(latest_version, version)
        return self.get_desired_config(camera_id)

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
            raise ValueError(
                f"Invalid log level: {level!r}. Must be one of {sorted(ALLOWED_LOG_LEVELS)}"
            )
        effective_logged_at = (logged_at or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
        conn = self._get_connection()
        with conn:
            self._upsert_camera_row(conn, camera_id, None)
            row = conn.execute(
                """
                INSERT INTO device_logs (camera_id, level, message, logged_at)
                VALUES (?, ?, ?, ?)
                RETURNING *
                """,
                (camera_id, level, message, effective_logged_at.isoformat()),
            ).fetchone()
        return DeviceLogRecord(
            id=row["id"],
            camera_id=row["camera_id"],
            level=row["level"],
            message=row["message"],
            logged_at=_parse_datetime(row["logged_at"]) or effective_logged_at,
        )

    def camera_ids_with_recent_errors(self, *, since: datetime) -> set[str]:
        conn = self._get_connection()
        rows = conn.execute(
            """
            SELECT DISTINCT camera_id FROM device_logs
            WHERE level IN ('error', 'critical') AND logged_at >= ?
            """,
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
        return {row["camera_id"] for row in rows}

    def list_device_logs(self, camera_id: str, *, limit: int = 50) -> list[DeviceLogRecord]:
        conn = self._get_connection()
        rows = conn.execute(
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
