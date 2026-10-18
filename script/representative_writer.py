"""Native-process optional writes: bounded memory, cadence, and resource guards.

Limits are admission guards, not filesystem quotas. An admitted/in-flight write
may finish after a fault; OS writeback and other processes are not hard-capped.
"""

from __future__ import annotations

import math
import shutil
import time
from collections import deque
from itertools import islice
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

from script.detector import Detection
from script.validation import is_valid_camera_id

MIB = 1024 * 1024
HOUR_BUDGET = 128 * MIB


def sample_resources(upload_dir, db_path):
    """Run off the recognition/request thread; Linux write_bytes includes IO here."""
    usage = shutil.disk_usage(upload_dir)
    db = Path(db_path)
    io = dict(line.split(":", 1) for line in Path("/proc/self/io").read_text().splitlines())
    return {"free": usage.free, "total": usage.total,
            "db": db.stat().st_size if db.exists() else 0,
            "wal": Path(f"{db}-wal").stat().st_size if Path(f"{db}-wal").exists() else 0,
            "write_bytes": int(io["write_bytes"])}


class _Window:
    """At most 61 minute buckets; retain the boundary minute conservatively."""

    def __init__(self):
        self.buckets = deque()

    def add(self, now, size):
        minute = int(now // 60)
        while self.buckets and self.buckets[0][0] < minute - 60:
            self.buckets.popleft()
        if self.buckets and self.buckets[-1][0] == minute:
            self.buckets[-1][1] += size
        else:
            self.buckets.append([minute, size])
        return sum(item[1] for item in self.buckets)


class ResourceGuard:
    """Cached monitor state; submit never stats disk or waits for a checkpoint."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = Lock()
        self.sample = None
        self.sample_at = None
        self.reason = "monitor_unavailable"
        self.paused = True
        self.healthy_since = None
        self._started = False
        self._last_write = None
        self._physical = _Window()
        self._reserved = _Window()
        self.physical_bytes = 0
        self.reserved_bytes = 0

    def _fault(self, reason):
        self.paused, self.reason, self.healthy_since = True, reason, None

    def _fresh(self, now):
        if self.sample_at is None or now - self.sample_at > 3:
            self._fault("monitor_stale")

    def observe(self, sample):
        now = self.clock()
        with self.lock:
            if self.sample_at is not None and now - self.sample_at > 3:
                self._fault("monitor_stale")
            if (type(sample) is not dict or set(sample) != {"free", "total", "db", "wal", "write_bytes"}
                    or any(type(v) is not int or not 0 <= v < 2**64 for v in sample.values())
                    or not sample["total"] or sample["free"] > sample["total"]
                    or (self._last_write is not None and sample["write_bytes"] < self._last_write)):
                self._fault("monitor_error")
                self._started = True
                return
            delta = 0 if self._last_write is None else sample["write_bytes"] - self._last_write
            self._last_write = sample["write_bytes"]
            self.physical_bytes = self._physical.add(now, delta)
            self.reserved_bytes = self._reserved.add(now, 0)
            self.sample, self.sample_at = dict(sample), now
            if sample["free"] <= max(1024 * MIB, sample["total"] // 10):
                reason = "low_disk"
            elif sample["db"] >= 256 * MIB:
                reason = "db_limit"
            elif sample["wal"] >= 8 * MIB:
                reason = "wal_limit"
            elif self.physical_bytes >= HOUR_BUDGET or self.reserved_bytes >= HOUR_BUDGET:
                reason = "write_limit"
            else:
                reason = None
            if reason:
                self._fault(reason)
            elif not self._started:
                self.paused, self.reason = False, None
            elif sample["free"] > max(1536 * MIB, sample["total"] * 15 // 100):
                if self.healthy_since is None:
                    self.healthy_since = now
            else:
                self.healthy_since = None
            self._started = True

    def monitor_failed(self):
        with self.lock:
            self._started = True
            self._fault("monitor_error")

    def resume(self):
        with self.lock:
            now = self.clock()
            self._fresh(now)
            if (self.healthy_since is None or now - self.healthy_since < 60):
                return False
            self.paused, self.reason = False, None
            return True

    def reserve(self, size):
        if type(size) is not int or not 0 < size <= 192 * 1024:
            raise ValueError("Invalid write reservation")
        with self.lock:
            now = self.clock()
            self._fresh(now)
            self.reserved_bytes = self._reserved.add(now, 0)
            if self.paused:
                return False
            if self.reserved_bytes + size > HOUR_BUDGET:
                self._fault("write_limit")
                return False
            self.reserved_bytes = self._reserved.add(now, size)
            return True

    def status(self):
        with self.lock:
            now = self.clock()
            self._fresh(now)
            return {"paused": self.paused, "reason": self.reason,
                    "resume_ready": self.paused and self.healthy_since is not None
                    and now - self.healthy_since >= 60,
                    "reserved_bytes_hour": self.reserved_bytes,
                    "physical_bytes_hour": self.physical_bytes,
                    "sample": dict(self.sample) if self.sample is not None else None}


class RepresentativeWriter:
    """One writer, two admitted images, <=4 cameras, <=1 image/10s/camera.

    save(camera_id, jpeg, immutable_detections, context) owns storage/SQL/notify.
    context is a bounded (boot, generation, sequence) identity. No retry queue.
    """

    def __init__(self, save, guard, *, keep_only_detected=True, clock=time.monotonic):
        from gevent.monkey import is_module_patched
        if is_module_patched("threading"):
            raise RuntimeError("RepresentativeWriter requires a native process")
        self.save, self.guard = save, guard
        self.keep_only_detected, self.clock = keep_only_detected, clock
        self.lock = Lock()
        self.queue = Queue(maxsize=2)
        self.stop = Event()
        self.last = {}
        self.counts = {"pending": 0, "pending_bytes": 0, "saved": 0, "errors": 0,
                       "interval": 0, "full": 0, "undetected": 0, "resource": 0,
                       "invalid": 0, "closed": 0, "cameras": 0, "discarded": 0}
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, camera_id, jpeg, detections, context):
        # Copy at most 301 entries before validation, even if another thread
        # grows the caller's list between len() and iteration.
        frozen = tuple(islice(detections, 301)) if type(detections) in (list, tuple) else None
        if (type(jpeg) is not bytes or not 0 < len(jpeg) <= 128 * 1024
                or type(camera_id) is not str or not is_valid_camera_id(camera_id)
                or frozen is None
                or len(frozen) > 300 or type(context) is not tuple or len(context) != 3
                or any(type(v) is not int or not 0 < v < 2**64 for v in context)
                or any(not self._valid_detection(d) for d in frozen)
                or sum(200 + len(d.label.encode("utf-8")) * 6 for d in frozen) > 64 * 1024):
            with self.lock:
                self.counts["invalid"] += 1
            return False
        size = len(jpeg) + 64 * 1024  # Logical metadata allowance; NOT physical IO upper bound.
        with self.lock:
            if self.stop.is_set():
                reason = "closed"
            elif self.keep_only_detected and not frozen:
                reason = "undetected"
            elif camera_id not in self.last and len(self.last) >= 4:
                reason = "cameras"
            elif self.clock() - self.last.get(camera_id, -math.inf) < 10:
                reason = "interval"
            elif self.counts["pending"] >= 2:
                reason = "full"
            elif not self.guard.reserve(size):
                reason = "resource"
            else:
                self.last[camera_id] = self.clock()
                self.counts["pending"] += 1
                self.counts["pending_bytes"] += size
                try:
                    self.queue.put_nowait((camera_id, jpeg, frozen, context, size))
                except Full:  # Covered by pending reservation; defensive only.
                    self.counts["pending"] -= 1
                    self.counts["pending_bytes"] -= size
                    self.counts["full"] += 1
                    return False
                return True
            self.counts[reason] += 1
            return False

    @staticmethod
    def _valid_detection(d):
        if type(d) is not Detection or type(d.label) is not str or not 0 < len(d.label) <= 64:
            return False
        try:
            d.label.encode("utf-8")
        except UnicodeEncodeError:
            return False
        values = (d.score, d.x1, d.y1, d.x2, d.y2)
        return (all(type(v) in (int, float) and 0 <= v <= 1 and math.isfinite(v) for v in values)
                and d.x1 < d.x2 and d.y1 < d.y2
                and (d.track_id is None or type(d.track_id) is int and 0 < d.track_id < 2**63))

    def _run(self):
        while not self.stop.is_set() or not self.queue.empty():
            try:
                camera, jpeg, detections, context, size = self.queue.get(timeout=0.1)
            except Empty:
                continue
            outcome = "discarded"
            try:
                if not self.stop.is_set() and not self.guard.status()["paused"]:
                    outcome = "saved" if self.save(camera, jpeg, detections, context) is not False else "discarded"
            except Exception:
                outcome = "errors"
                self.guard.monitor_failed()
            finally:
                with self.lock:
                    self.counts[outcome] += 1
                    self.counts["pending"] -= 1
                    self.counts["pending_bytes"] -= size
                self.queue.task_done()

    def status(self):
        with self.lock:
            return dict(self.counts)

    def close(self):
        with self.lock:
            self.stop.set()
        self.thread.join(timeout=1)
