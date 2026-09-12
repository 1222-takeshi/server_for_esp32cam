from __future__ import annotations

import threading
from collections import OrderedDict


class ThumbnailCache:
    """Bounded, thread-safe in-process cache of generated thumbnail JPEG
    bytes, keyed by the source image's relative path and validated against
    its current ``(mtime_ns, size)`` signature.

    This is purely in-memory and self-invalidating: if the source file's
    mtime/size no longer match what was cached, ``get()`` treats it as a
    miss and drops the stale entry instead of serving outdated bytes. If the
    source file is deleted entirely, the route already returns 404 (via the
    metadata store lookup) before ever consulting the cache, so a deleted
    file's entry is simply never looked up again -- it ages out through
    normal LRU eviction rather than needing an explicit invalidation hook
    into the storage/retention deletion paths (see #394).

    Bounded by ``max_entries`` (evicting least-recently-used) so memory use
    can't grow unbounded on a deployment with many distinct images.
    """

    def __init__(self, max_entries: int = 256) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._lock = threading.Lock()
        # value: (mtime_ns, size, jpeg_bytes)
        self._entries: OrderedDict[str, tuple[int, int, bytes]] = OrderedDict()

    def get(self, key: str, *, mtime_ns: int, size: int) -> bytes | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            cached_mtime_ns, cached_size, jpeg_bytes = entry
            if cached_mtime_ns != mtime_ns or cached_size != size:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return jpeg_bytes

    def put(self, key: str, *, mtime_ns: int, size: int, jpeg_bytes: bytes) -> None:
        with self._lock:
            self._entries[key] = (mtime_ns, size, jpeg_bytes)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
