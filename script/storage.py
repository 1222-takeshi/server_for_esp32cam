from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True)
class StoredImage:
    name: str
    size: int
    created_at: datetime


class Storage(Protocol):
    def save_image(self, data: bytes, content_type: str) -> StoredImage:
        ...

    def list_images(self) -> list[StoredImage]:
        ...

    def read_image(self, name: str) -> bytes:
        ...


class LocalFilesystemStorage:
    def __init__(self, upload_dir: str | Path):
        self.upload_dir = Path(upload_dir).resolve()
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def save_image(self, data: bytes, content_type: str) -> StoredImage:
        if content_type != "image/jpeg":
            raise ValueError(f"Unsupported content type: {content_type}")

        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        filename = f"image_{timestamp}_{uuid4().hex}.jpg"
        path = self.upload_dir / filename
        path.write_bytes(data)
        stat = path.stat()
        return StoredImage(
            name=filename,
            size=stat.st_size,
            created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    def list_images(self) -> list[StoredImage]:
        images = [
            StoredImage(
                name=path.name,
                size=path.stat().st_size,
                created_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
            )
            for path in self.upload_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
        ]
        return sorted(images, key=lambda image: image.created_at, reverse=True)

    def read_image(self, name: str) -> bytes:
        if not name or Path(name).name != name:
            raise FileNotFoundError(name)

        path = (self.upload_dir / name).resolve()
        if self.upload_dir not in path.parents:
            raise FileNotFoundError(name)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(name)
        return path.read_bytes()
