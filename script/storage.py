from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from script.validation import is_valid_camera_id

_HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class StoredImage:
    name: str
    relative_path: str
    size: int
    created_at: datetime
    device_id: str | None = None
    captured_at: datetime | None = None


@dataclass(frozen=True)
class StoredVideo:
    name: str
    relative_path: str
    size: int
    camera_id: str
    captured_at: datetime | None
    stored_at: datetime
    duration_ms: int | None
    content_type: str
    sha256: str


@dataclass(frozen=True)
class StorageRetentionPolicy:
    max_age_seconds: int | None = None
    max_bytes: int | None = None

    @property
    def enabled(self) -> bool:
        return (self.max_age_seconds is not None and self.max_age_seconds > 0) or (
            self.max_bytes is not None and self.max_bytes > 0
        )


def _normalize_device_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_camera_id(value: str) -> str:
    if not is_valid_camera_id(value):
        raise ValueError(f"Invalid camera_id: {value!r}")
    return value


def _normalize_captured_at(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)

    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Storage(Protocol):
    def save_image(
        self,
        data: bytes,
        content_type: str,
        *,
        device_id: str,
        captured_at: datetime,
    ) -> StoredImage: ...

    def list_images(self, *, include_metadata: bool = True) -> list[StoredImage]: ...

    def list_image_names(self) -> list[tuple[str, str]]: ...

    def stat_images(
        self, relative_paths: list[str], *, include_metadata: bool = True
    ) -> list[StoredImage]: ...

    def read_image_metadata(self, relative_path: str) -> tuple[str | None, datetime | None]: ...

    def read_image_by_relative_path(self, relative_path: str) -> bytes: ...

    def read_image_path_by_relative_path(self, relative_path: str) -> Path: ...

    def delete_image_by_relative_path(self, relative_path: str) -> int: ...

    def save_video_stream(
        self,
        stream: object,
        content_type: str,
        *,
        camera_id: str,
        captured_at: datetime | str | None = None,
        duration_ms: int | None = None,
        max_bytes: int,
    ) -> StoredVideo: ...

    def read_video_path_by_relative_path(self, relative_path: str) -> tuple[Path, str]: ...

    def delete_video(self, stored: StoredVideo) -> None: ...

    def delete_video_by_relative_path(self, relative_path: str) -> int: ...

    def delete_camera_files(self, camera_id: str) -> None: ...


class LocalFilesystemStorage:
    def __init__(self, upload_dir: str | Path):
        self.upload_dir = Path(upload_dir).resolve()
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def _metadata_path(self, image_path: Path) -> Path:
        return image_path.with_suffix(f"{image_path.suffix}.json")

    def _camera_image_dir(self, device_id: str) -> Path:
        return self.upload_dir / device_id / "images"

    def _camera_video_dir(self, camera_id: str) -> Path:
        return self.upload_dir / camera_id / "videos"

    def _cleanup_empty_parents(self, path: Path) -> None:
        current = path.parent
        while current != self.upload_dir.parent and current != self.upload_dir:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _write_metadata(
        self,
        image_path: Path,
        device_id: str | None,
        captured_at: datetime | None,
    ) -> None:
        if device_id is None and captured_at is None:
            return

        payload: dict[str, str] = {}
        if device_id is not None:
            payload["device_id"] = device_id
        if captured_at is not None:
            payload["captured_at"] = captured_at.isoformat()

        self._metadata_path(image_path).write_text(json.dumps(payload), encoding="utf-8")

    def _read_metadata(self, image_path: Path) -> tuple[str | None, datetime | None]:
        metadata_path = self._metadata_path(image_path)
        if not metadata_path.exists() or not metadata_path.is_file():
            return None, None

        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, None

        if not isinstance(payload, dict):
            return None, None

        device_id = payload.get("device_id")
        captured_at = payload.get("captured_at")
        return (
            _normalize_device_id(device_id) if isinstance(device_id, str) else None,
            _normalize_captured_at(captured_at) if isinstance(captured_at, str) else None,
        )

    def _resolve_relative_path(self, relative_path: str) -> Path:
        normalized = Path(relative_path)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise FileNotFoundError(relative_path)
        path = (self.upload_dir / normalized).resolve()
        if self.upload_dir != path and self.upload_dir not in path.parents:
            raise FileNotFoundError(relative_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(relative_path)
        if path.suffix.lower() not in {".jpg", ".jpeg"}:
            raise FileNotFoundError(relative_path)
        return path

    def save_image(
        self,
        data: bytes,
        content_type: str,
        *,
        device_id: str,
        captured_at: datetime,
    ) -> StoredImage:
        if content_type != "image/jpeg":
            raise ValueError(f"Unsupported content type: {content_type}")

        stored_device_id = _normalize_device_id(device_id)
        stored_captured_at = _normalize_captured_at(captured_at)
        if stored_device_id is None or stored_captured_at is None:
            raise ValueError("device metadata is required")

        image_dir = self._camera_image_dir(stored_device_id)
        image_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        filename = f"image_{timestamp}_{uuid4().hex}.jpg"
        path = image_dir / filename

        try:
            path.write_bytes(data)
            self._write_metadata(path, stored_device_id, stored_captured_at)
            stat = path.stat()
            return StoredImage(
                name=filename,
                relative_path=str(path.relative_to(self.upload_dir).as_posix()),
                size=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                device_id=stored_device_id,
                captured_at=stored_captured_at,
            )
        except Exception:
            path.unlink(missing_ok=True)
            self._metadata_path(path).unlink(missing_ok=True)
            self._cleanup_empty_parents(path)
            raise

    def list_images(self, *, include_metadata: bool = True) -> list[StoredImage]:
        # Full filesystem rglob() walk. Only called once at app startup
        # (see app.py's sync_storage_index() call) to import pre-existing
        # files into the SQLite index; request-path listing/retention use
        # the index instead. Do not call this from a request handler.
        images = [
            StoredImage(*self._stored_image_fields(path, include_metadata=include_metadata))
            for path in self.upload_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
        ]
        return sorted(images, key=lambda image: image.created_at, reverse=True)

    def list_image_names(self) -> list[tuple[str, str]]:
        """Cheap filesystem walk that returns only `(name, relative_path)`
        pairs, without `path.stat()`-ing each file.

        Used by app.py's startup index sync so it can ask
        `SQLiteMetadataStore.filter_new_filenames()` which files are
        genuinely new *before* paying for a `stat()` call (and, when
        requested, a metadata sidecar read) per file -- instead of
        `stat()`-ing every file on disk on every boot regardless of whether
        it is already indexed (#383).
        """
        return [
            (path.name, path.relative_to(self.upload_dir).as_posix())
            for path in self.upload_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
        ]

    def stat_images(
        self, relative_paths: list[str], *, include_metadata: bool = True
    ) -> list[StoredImage]:
        """Build `StoredImage` records (i.e. `stat()` + optional metadata
        sidecar read) for only the given `relative_paths`, instead of every
        file on disk. Paired with `list_image_names()` so callers can skip
        this per-file cost for files already known to be indexed (#383).

        Silently skips a path if the file has disappeared since it was
        listed (e.g. deleted concurrently with startup sync).
        """
        images = []
        for relative_path in relative_paths:
            path = self.upload_dir / relative_path
            try:
                images.append(
                    StoredImage(*self._stored_image_fields(path, include_metadata=include_metadata))
                )
            except OSError:
                continue
        return images

    def read_image_metadata(self, relative_path: str) -> tuple[str | None, datetime | None]:
        path = self._resolve_relative_path(relative_path)
        return self._read_metadata(path)

    def _stored_image_fields(
        self,
        path: Path,
        *,
        include_metadata: bool = True,
    ) -> tuple[str, str, int, datetime, str | None, datetime | None]:
        stat = path.stat()
        device_id, captured_at = self._read_metadata(path) if include_metadata else (None, None)
        return (
            path.name,
            path.relative_to(self.upload_dir).as_posix(),
            stat.st_size,
            datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            device_id,
            captured_at,
        )

    def _write_video_metadata(self, video: StoredVideo) -> None:
        path = self.upload_dir / video.relative_path
        payload: dict[str, object] = {
            "camera_id": video.camera_id,
            "captured_at": video.captured_at.isoformat() if video.captured_at else None,
            "stored_at": video.stored_at.isoformat(),
            "duration_ms": video.duration_ms,
            "content_type": video.content_type,
            "size": video.size,
            "sha256": video.sha256,
        }
        metadata_path = self._metadata_path(path)
        temp_path = metadata_path.with_name(f".{metadata_path.name}.{uuid4().hex}.tmp")
        try:
            temp_path.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temp_path, metadata_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def save_video_stream(
        self,
        stream: object,
        content_type: str,
        *,
        camera_id: str,
        captured_at: datetime | str | None = None,
        duration_ms: int | None = None,
        max_bytes: int,
        chunk_size: int = _HASH_CHUNK_SIZE,
    ) -> StoredVideo:
        """Write an incoming video upload to disk as it is read.

        This reads `stream` (anything exposing `read(size)` -- the raw WSGI
        request stream, or a multipart FileStorage.stream) in bounded chunks,
        hashing incrementally so peak memory per upload is bounded by
        `chunk_size` instead of the full payload size (see #282 / #250).
        `chunk_size` only exists so tests can exercise multi-chunk behavior
        with small payloads; callers normally leave it at the default.
        """
        if duration_ms is not None and duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        stored_camera_id = _validate_camera_id(camera_id)
        if content_type != "video/mp4":
            raise ValueError(f"Unsupported content type: {content_type}")
        stored_captured_at = _normalize_captured_at(captured_at)

        video_dir = self._camera_video_dir(stored_camera_id)
        video_dir.mkdir(parents=True, exist_ok=True)
        timestamp_source = stored_captured_at or datetime.now(tz=timezone.utc)
        timestamp = timestamp_source.strftime("%Y%m%dT%H%M%S%f")
        filename = f"video_{timestamp}_{uuid4().hex}.mp4"
        path = video_dir / filename
        temp_path = video_dir / f".{filename}.{uuid4().hex}.tmp"

        digest = hashlib.sha256()
        header = bytearray()
        total = 0
        try:
            with temp_path.open("wb") as handle:
                while True:
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        # Mirrors the existing len(data) > max_bytes check in
                        # routes.py: a chunked request without Content-Length
                        # can otherwise have its excess silently truncated by
                        # Werkzeug's LimitedStream instead of rejected.
                        raise ValueError("Payload too large")
                    digest.update(chunk)
                    if len(header) < 12:
                        header.extend(chunk[: 12 - len(header)])
                    handle.write(chunk)

            if total == 0:
                raise ValueError("No data received")
            if len(header) < 12 or bytes(header[4:8]) != b"ftyp":
                raise ValueError("Unsupported video payload")

            os.replace(temp_path, path)
            stat = path.stat()
            stored = StoredVideo(
                name=filename,
                relative_path=path.relative_to(self.upload_dir).as_posix(),
                size=stat.st_size,
                camera_id=stored_camera_id,
                captured_at=stored_captured_at,
                stored_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                duration_ms=duration_ms,
                content_type=content_type,
                sha256=digest.hexdigest(),
            )
            self._write_video_metadata(stored)
            return stored
        except Exception:
            temp_path.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            self._metadata_path(path).unlink(missing_ok=True)
            self._cleanup_empty_parents(path)
            raise

    def _resolve_video_relative_path(self, relative_path: str) -> Path:
        normalized = Path(relative_path)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise FileNotFoundError(relative_path)
        path = (self.upload_dir / normalized).resolve()
        if self.upload_dir != path and self.upload_dir not in path.parents:
            raise FileNotFoundError(relative_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(relative_path)
        if path.suffix.lower() != ".mp4":
            raise FileNotFoundError(relative_path)
        return path

    def read_video_path_by_relative_path(self, relative_path: str) -> tuple[Path, str]:
        path = self._resolve_video_relative_path(relative_path)
        return path, "video/mp4"

    def delete_video(self, stored: StoredVideo) -> None:
        path = (self.upload_dir / stored.relative_path).resolve()
        if self.upload_dir != path and self.upload_dir not in path.parents:
            raise ValueError(f"video resolves outside upload_dir: {stored.relative_path!r}")
        if path.suffix.lower() != ".mp4":
            raise ValueError(f"not a video path: {stored.relative_path!r}")
        self._metadata_path(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        self._cleanup_empty_parents(path)

    def delete_video_by_relative_path(self, relative_path: str) -> int:
        """Delete a video (and its sidecar) given only its relative_path.

        Mirrors delete_image_by_relative_path(): retention candidates sourced
        from the SQLite index (script/metadata.py) already carry
        relative_path/size, so callers don't need to reconstruct a full
        StoredVideo (camera_id/captured_at/duration_ms/sha256/...) just to
        call delete_video(). Raises FileNotFoundError if the file is missing.
        """
        path = self._resolve_video_relative_path(relative_path)
        size = path.stat().st_size
        self._metadata_path(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        self._cleanup_empty_parents(path)
        return size

    def read_image_by_relative_path(self, relative_path: str) -> bytes:
        return self._resolve_relative_path(relative_path).read_bytes()

    def read_image_path_by_relative_path(self, relative_path: str) -> Path:
        return self._resolve_relative_path(relative_path)

    def delete_image_by_relative_path(self, relative_path: str) -> int:
        path = self._resolve_relative_path(relative_path)
        size = path.stat().st_size
        self._metadata_path(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        self._cleanup_empty_parents(path)
        return size

    def delete_camera_files(self, camera_id: str) -> None:
        camera_dir = self._camera_image_dir(camera_id).parent.resolve()
        if camera_dir == self.upload_dir or self.upload_dir not in camera_dir.parents:
            raise ValueError(f"camera_id resolves outside upload_dir: {camera_id!r}")
        if camera_dir.exists() and camera_dir.is_dir():
            shutil.rmtree(camera_dir)

    def _delete_image(self, image: StoredImage) -> int:
        return self.delete_image_by_relative_path(image.relative_path)
