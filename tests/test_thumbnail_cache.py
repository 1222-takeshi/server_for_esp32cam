from __future__ import annotations

import pytest

from script.thumbnail_cache import ThumbnailCache


def test_rejects_non_positive_max_entries():
    with pytest.raises(ValueError):
        ThumbnailCache(max_entries=0)


def test_miss_on_empty_cache():
    cache = ThumbnailCache(max_entries=4)

    assert cache.get("a.jpg", mtime_ns=1, size=10) is None


def test_put_then_get_with_matching_signature_hits():
    cache = ThumbnailCache(max_entries=4)

    cache.put("a.jpg", mtime_ns=1, size=10, jpeg_bytes=b"thumb-a")

    assert cache.get("a.jpg", mtime_ns=1, size=10) == b"thumb-a"
    assert len(cache) == 1


def test_stale_mtime_is_treated_as_miss_and_evicts_entry():
    cache = ThumbnailCache(max_entries=4)
    cache.put("a.jpg", mtime_ns=1, size=10, jpeg_bytes=b"thumb-a")

    # Source file was replaced/re-uploaded at the same path -- new mtime.
    assert cache.get("a.jpg", mtime_ns=2, size=10) is None
    # The stale entry was dropped, not just ignored.
    assert len(cache) == 0


def test_stale_size_is_treated_as_miss():
    cache = ThumbnailCache(max_entries=4)
    cache.put("a.jpg", mtime_ns=1, size=10, jpeg_bytes=b"thumb-a")

    assert cache.get("a.jpg", mtime_ns=1, size=99) is None


def test_put_overwrites_existing_entry_for_same_key():
    cache = ThumbnailCache(max_entries=4)
    cache.put("a.jpg", mtime_ns=1, size=10, jpeg_bytes=b"thumb-a-old")

    cache.put("a.jpg", mtime_ns=2, size=20, jpeg_bytes=b"thumb-a-new")

    assert cache.get("a.jpg", mtime_ns=2, size=20) == b"thumb-a-new"
    assert len(cache) == 1


def test_evicts_least_recently_used_entry_when_over_capacity():
    cache = ThumbnailCache(max_entries=2)
    cache.put("a.jpg", mtime_ns=1, size=1, jpeg_bytes=b"a")
    cache.put("b.jpg", mtime_ns=1, size=1, jpeg_bytes=b"b")

    # Touch "a" so "b" becomes the least-recently-used entry.
    assert cache.get("a.jpg", mtime_ns=1, size=1) == b"a"

    cache.put("c.jpg", mtime_ns=1, size=1, jpeg_bytes=b"c")

    assert len(cache) == 2
    assert cache.get("b.jpg", mtime_ns=1, size=1) is None
    assert cache.get("a.jpg", mtime_ns=1, size=1) == b"a"
    assert cache.get("c.jpg", mtime_ns=1, size=1) == b"c"


def test_independent_keys_do_not_collide():
    cache = ThumbnailCache(max_entries=4)
    cache.put("a.jpg", mtime_ns=1, size=10, jpeg_bytes=b"thumb-a")
    cache.put("b.jpg", mtime_ns=1, size=10, jpeg_bytes=b"thumb-b")

    assert cache.get("a.jpg", mtime_ns=1, size=10) == b"thumb-a"
    assert cache.get("b.jpg", mtime_ns=1, size=10) == b"thumb-b"
