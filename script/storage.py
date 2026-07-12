from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4


_SAFE_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HASH_CHUNK_SIZE = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


@dataclass(frozen=True)
class StorageRetentionResult:
    deleted_relative_paths: list[str]
    deleted_bytes: int


def _normalize_device_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_camera_id(value: str) -> str:
    if not _SAFE_CAMERA_ID_RE.fullmatch(value):
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

    def list_images(self) -> list[StoredImage]: ...

    def read_image(self, name: str) -> bytes: ...

    def read_image_by_relative_path(self, relative_path: str) -> bytes: ...

    def read_image_path_by_relative_path(self, relative_path: str) -> Path: ...

    def delete_image_by_relative_path(self, relative_path: str) -> int: ...

    def apply_retention(
        self,
        policy: StorageRetentionPolicy,
        *,
        now: datetime | None = None,
        preserve_relative_paths: set[str] | None = None,
    ) -> StorageRetentionResult: ...

    def apply_video_retention(
        self,
        policy: StorageRetentionPolicy,
        *,
        now: datetime | None = None,
        preserve_relative_paths: set[str] | None = None,
    ) -> StorageRetentionResult: ...

    def save_video(
        self,
        data: bytes,
        content_type: str,
        *,
        camera_id: str,
        captured_at: datetime | str | None = None,
        duration_ms: int | None = None,
    ) -> StoredVideo: ...

    def list_videos(self) -> list[StoredVideo]: ...

    def read_video_by_relative_path(self, relative_path: str) -> tuple[bytes, str]: ...

    def read_video_path_by_relative_path(self, relative_path: str) -> tuple[Path, str]: ...

    def delete_video(self, stored: StoredVideo) -> None: ...

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

    def list_images(self) -> list[StoredImage]:
        images = [
            StoredImage(*self._stored_image_fields(path))
            for path in self.upload_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
        ]
        return sorted(images, key=lambda image: image.created_at, reverse=True)

    def _stored_image_fields(
        self,
        path: Path,
    ) -> tuple[str, str, int, datetime, str | None, datetime | None]:
        stat = path.stat()
        device_id, captured_at = self._read_metadata(path)
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

    def _read_video_metadata(
        self, video_path: Path
    ) -> tuple[datetime | None, int | None, str, str | None]:
        metadata_path = self._metadata_path(video_path)
        if not metadata_path.exists() or not metadata_path.is_file():
            return None, None, "video/mp4", None
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, None, "video/mp4", None
        if not isinstance(payload, dict):
            return None, None, "video/mp4", None
        captured_at = payload.get("captured_at")
        duration_ms = payload.get("duration_ms")
        content_type = payload.get("content_type")
        sha256 = payload.get("sha256")
        return (
            _normalize_captured_at(captured_at) if isinstance(captured_at, str) else None,
            duration_ms if isinstance(duration_ms, int) else None,
            content_type if isinstance(content_type, str) else "video/mp4",
            sha256 if isinstance(sha256, str) else None,
        )

    def _validate_mp4_payload(self, data: bytes, content_type: str) -> None:
        if content_type != "video/mp4":
            raise ValueError(f"Unsupported content type: {content_type}")
        if len(data) < 12 or data[4:8] != b"ftyp":
            raise ValueError("Unsupported video payload")

    def save_video(
        self,
        data: bytes,
        content_type: str,
        *,
        camera_id: str,
        captured_at: datetime | str | None = None,
        duration_ms: int | None = None,
    ) -> StoredVideo:
        if duration_ms is not None and duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        stored_camera_id = _validate_camera_id(camera_id)
        self._validate_mp4_payload(data, content_type)
        stored_captured_at = _normalize_captured_at(captured_at)
        video_dir = self._camera_video_dir(stored_camera_id)
        video_dir.mkdir(parents=True, exist_ok=True)
        timestamp_source = stored_captured_at or datetime.now(tz=timezone.utc)
        timestamp = timestamp_source.strftime("%Y%m%dT%H%M%S%f")
        filename = f"video_{timestamp}_{uuid4().hex}.mp4"
        path = video_dir / filename
        temp_path = video_dir / f".{filename}.{uuid4().hex}.tmp"
        try:
            temp_path.write_bytes(data)
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
                sha256=hashlib.sha256(data).hexdigest(),
            )
            self._write_video_metadata(stored)
            return stored
        except Exception:
            temp_path.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            self._metadata_path(path).unlink(missing_ok=True)
            self._cleanup_empty_parents(path)
            raise

    def list_videos(self) -> list[StoredVideo]:
        videos = [
            self._stored_video(path)
            for path in self.upload_dir.rglob("*.mp4")
            if path.is_file() and path.parent.name == "videos"
        ]
        return sorted(videos, key=lambda video: video.stored_at, reverse=True)

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

    def read_video_by_relative_path(self, relative_path: str) -> tuple[bytes, str]:
        path = self._resolve_video_relative_path(relative_path)
        return path.read_bytes(), "video/mp4"

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

    def _stored_video(self, path: Path) -> StoredVideo:
        stat = path.stat()
        try:
            camera_id = path.relative_to(self.upload_dir).parts[0]
            camera_id = _validate_camera_id(camera_id)
        except (ValueError, IndexError):
            camera_id = "unknown"
        captured_at, duration_ms, content_type, sha256 = self._read_video_metadata(path)
        data_sha256 = sha256
        if data_sha256 is None:
            # Legacy sidecar predating stream-hashing on upload: hash once
            # here and persist it back so future scans don't re-read the
            # whole file every time list_videos() is called.
            data_sha256 = _sha256_file(path)
        video = StoredVideo(
            name=path.name,
            relative_path=path.relative_to(self.upload_dir).as_posix(),
            size=stat.st_size,
            camera_id=camera_id,
            captured_at=captured_at,
            stored_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            duration_ms=duration_ms,
            content_type=content_type,
            sha256=data_sha256,
        )
        if sha256 is None:
            try:
                self._write_video_metadata(video)
            except OSError:
                # Best-effort cache backfill; a failed write just means the
                # next scan re-hashes this file, matching the old behavior.
                pass
        return video

    def read_image(self, name: str) -> bytes:
        if not name or Path(name).name != name:
            raise FileNotFoundError(name)
        if Path(name).suffix.lower() not in {".jpg", ".jpeg"}:
            raise FileNotFoundError(name)

        matches = [path for path in self.upload_dir.rglob(name) if path.is_file()]
        if len(matches) != 1:
            raise FileNotFoundError(name)
        return matches[0].read_bytes()

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

    def apply_retention(
        self,
        policy: StorageRetentionPolicy,
        *,
        now: datetime | None = None,
        preserve_relative_paths: set[str] | None = None,
    ) -> StorageRetentionResult:
        if not policy.enabled:
            return StorageRetentionResult(deleted_relative_paths=[], deleted_bytes=0)

        preserved = preserve_relative_paths or set()
        reference = now or datetime.now(tz=timezone.utc)
        images = self.list_images()
        candidates = sorted(
            [image for image in images if image.relative_path not in preserved],
            key=lambda item: item.created_at,
        )
        total_bytes = sum(image.size for image in images)
        deleted: list[str] = []
        deleted_bytes = 0

        for image in candidates:
            age_expired = (
                policy.max_age_seconds is not None
                and policy.max_age_seconds > 0
                and (reference - image.created_at).total_seconds() > policy.max_age_seconds
            )
            over_quota = (
                policy.max_bytes is not None
                and policy.max_bytes > 0
                and total_bytes > policy.max_bytes
            )
            if not age_expired and not over_quota:
                continue
            try:
                deleted_bytes += self._delete_image(image)
            except FileNotFoundError:
                total_bytes -= image.size
                continue
            deleted.append(image.relative_path)
            total_bytes -= image.size

        return StorageRetentionResult(deleted_relative_paths=deleted, deleted_bytes=deleted_bytes)

    def apply_video_retention(
        self,
        policy: StorageRetentionPolicy,
        *,
        now: datetime | None = None,
        preserve_relative_paths: set[str] | None = None,
    ) -> StorageRetentionResult:
        if not policy.enabled:
            return StorageRetentionResult(deleted_relative_paths=[], deleted_bytes=0)

        preserved = preserve_relative_paths or set()
        reference = now or datetime.now(tz=timezone.utc)
        videos = self.list_videos()
        candidates = sorted(
            [v for v in videos if v.relative_path not in preserved],
            key=lambda item: item.stored_at,
        )
        total_bytes = sum(v.size for v in videos)
        deleted: list[str] = []
        deleted_bytes = 0

        for v in candidates:
            age_expired = (
                policy.max_age_seconds is not None
                and policy.max_age_seconds > 0
                and (reference - v.stored_at).total_seconds() > policy.max_age_seconds
            )
            over_quota = (
                policy.max_bytes is not None
                and policy.max_bytes > 0
                and total_bytes > policy.max_bytes
            )
            if not age_expired and not over_quota:
                continue
            try:
                size = (self.upload_dir / v.relative_path).stat().st_size
                self.delete_video(v)
                deleted_bytes += size
            except (FileNotFoundError, OSError):
                total_bytes -= v.size
                continue
            deleted.append(v.relative_path)
            total_bytes -= v.size

        return StorageRetentionResult(deleted_relative_paths=deleted, deleted_bytes=deleted_bytes)
