from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True)
class StoredImage:
    name: str
    relative_path: str
    size: int
    created_at: datetime
    device_id: str | None = None
    captured_at: datetime | None = None


def _normalize_device_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


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
    ) -> StoredImage:
        ...

    def list_images(self) -> list[StoredImage]:
        ...

    def read_image(self, name: str) -> bytes:
        ...

    def read_image_by_relative_path(self, relative_path: str) -> bytes:
        ...


class LocalFilesystemStorage:
    def __init__(self, upload_dir: str | Path):
        self.upload_dir = Path(upload_dir).resolve()
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def _metadata_path(self, image_path: Path) -> Path:
        return image_path.with_suffix(f"{image_path.suffix}.json")

    def _camera_image_dir(self, device_id: str) -> Path:
        return self.upload_dir / device_id / "images"

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
