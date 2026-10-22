"""One native worker's bounded RAM context; the monitor owns all JSON/log IO."""

from collections import deque
import json
from threading import Lock
import time

FIELDS = ("boot_id", "mode_generation", "sequence", "captured_us", "sent_us",
          "host_received_us", "queue_checked_us", "infer_started_us", "infer_finished_us",
          "age_low_us", "age_high_us", "sequence_gap")
REASONS = frozenset({"cancelled", "profile_changed", "expired_queue",
    "insufficient_inference_time", "expired_result", "overwritten", "profile_hello_timeout",
    "clock_timeout", "invalid_message", "invalid_frame", "boot_mismatch", "duplicate",
    "stale", "retired_boot", "invalid_jpeg", "profile_unconfirmed", "clock_unknown",
    "clock_expired", "stale_capture"})
COUNTER_MAX = 2**64 - 1


class FaultContext:
    def __init__(self, *, clock=time.monotonic):
        self.clock, self.lock = clock, Lock()
        self.ring, self.pending = deque(maxlen=8), None
        self.last_emission = -float("inf")
        self.coalesced = self.invalid = self.attempted = self.unknown = self.rejected = 0
        self.busy = deque(maxlen=1)  # Saturating indication, never wait to count contention.

    def _reject(self):
        if not self.lock.acquire(blocking=False):
            self.busy.append(True)
            return
        try:
            self.rejected = min(COUNTER_MAX, self.rejected + 1)
        finally:
            self.lock.release()

    def observe(self, event):
        if type(event) is not dict:
            self._reject()
            return
        status = event.get("status")
        if type(status) is str and len(status) <= 16 and status in {"clock", "device_status"}:
            return  # Normal protocol telemetry is outside the frame diagnostic ring.
        if (type(status) is not str or len(status) > 16
                or status not in {"accepted", "success", "dropped", "failed", "closed"}):
            self._reject()
            return
        record = {"status": status, "observed_host_us": int(self.clock() * 1_000_000)}
        reason = event.get("reason")
        known = type(reason) is str and len(reason) <= 64 and reason in REASONS
        record["reason"] = reason if known else "inference_failed" if status == "failed" else "other"
        invalid = 0
        for key in FIELDS:
            value = event.get(key)
            if type(value) is int and 0 <= value < 2**64:
                record[key] = value
            elif value is not None:
                invalid += 1
        if not self.lock.acquire(blocking=False):
            self.busy.append(True)
            return
        try:
            self.invalid = min(COUNTER_MAX, self.invalid + invalid)
            self.ring.append(record)
            if status in {"dropped", "failed", "closed"}:
                self.unknown = min(COUNTER_MAX, self.unknown + (record["reason"] == "other"))
                if self.pending is None:
                    self.pending = tuple(self.ring)
                else:
                    self.coalesced = min(COUNTER_MAX, self.coalesced + 1)
        finally:
            self.lock.release()

    def take(self):
        """One attempt per 300 seconds. Serialization happens after releasing the lock."""
        if not self.lock.acquire(blocking=False):
            return None
        try:
            now = self.clock()
            if self.pending is None or now - self.last_emission < 300:
                return None
            events, self.pending = list(self.pending), None
            self.last_emission, self.attempted = now, min(COUNTER_MAX, self.attempted + 1)
            coalesced, invalid = self.coalesced, self.invalid
        finally:
            self.lock.release()
        record = {"schema": 1, "clock_units": "us: captured/sent=device; other times=host monotonic",
                  "events": events, "coalesced_total": coalesced, "invalid_fields_total": invalid}
        while True:
            payload = json.dumps(record, separators=(",", ":"), allow_nan=False)
            if len(payload.encode()) <= 4096:
                return payload
            events.pop(0)  # Keep the triggering (last) event, trimming oldest context first.

    def status(self):
        with self.lock:
            return {"ring_records": len(self.ring), "pending": self.pending is not None,
                    "attempted": self.attempted, "coalesced": self.coalesced,
                    "invalid_fields": self.invalid, "unknown_reasons": self.unknown,
                    "rejected_events": self.rejected,
                    "contention_observed": bool(self.busy)}
