"""One-camera, RAM-only acknowledged profiles; never authorizes motor movement."""

from __future__ import annotations

import secrets
import time
from collections import deque
from threading import RLock

MAX_VERSION = 2**53 - 1
ECO_GAP_LIMIT = 17  # Missing t=5,10 is tolerated if the next fresh result arrives at t=15.


def _uint(value, *, zero=False, maximum=MAX_VERSION):
    return type(value) is int and (0 if zero else 1) <= value <= maximum


class ModeError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class OperatingMode:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = RLock()
        self.version = secrets.randbits(40) + 1
        self.boot = None
        self.connected = False
        self.requested = "eco"
        self.state = "unknown"
        self.reason = "disconnected"
        self.ack = None
        self.ack_at = None
        self.cadence_at = None
        self.cadence_count = 0
        self.cadence_rate = None
        self.requested_at = self.clock()
        self.last_command = -float("inf")
        self.frames = deque(maxlen=128)
        self.rejected_ack = 0
        self.rejected_result = 0
        self.transitions = 0
        self.eco_recoverable = self.eco_recovery_ready = self.eco_confirmed = False
        self.automatic_recoveries = 0

    def _change(self, mode, *, reason=None):
        if self.version >= MAX_VERSION:
            self.connected, self.state, self.reason = False, "degraded", "version_exhausted"
            self.ack = None
            self.ack_at = None
            self.frames.clear()
            raise ModeError("Mode version exhausted; device restart required", 503)
        self.version += 1
        self.transitions += 1
        self.requested = mode
        self.state = "degraded" if reason else "pending"
        self.reason = reason
        self.ack = None
        self.ack_at = None
        self.cadence_at = None
        self.cadence_count = 0
        self.cadence_rate = None
        self.frames.clear()
        self.requested_at = self.clock()
        self.last_command = -float("inf")
        self.eco_recoverable = self.eco_confirmed = False

    def hello(self, boot_id, generation_floor):
        with self.lock:
            if (self.connected or not _uint(boot_id, maximum=2**64 - 1)
                    or not _uint(generation_floor, zero=True)):
                raise ModeError("Invalid profile hello")
            self.version = max(self.version, generation_floor)
            self.boot, self.connected = boot_id, True
            self._change("eco")  # Reconnection never restores tracking automatically.

    def disconnect(self):
        with self.lock:
            if not self.connected and self.requested == "eco" and self.ack is None:
                self.state = "unknown"
                return
            self.connected = False
            if self.version < MAX_VERSION:
                self._change("eco", reason="disconnected")
            else:
                self.requested, self.reason, self.ack = "eco", "version_exhausted", None
                self.ack_at = None
                self.frames.clear()
            self.state = "unknown"

    def request(self, mode, expected_version, *, ready):
        if type(mode) is not str or mode not in {"eco", "tracking"} or not _uint(expected_version):
            raise ModeError("Invalid mode or version")
        with self.lock:
            if expected_version != self.version:
                raise ModeError("Mode version conflict", 409)
            if mode == "tracking" and (not self.connected or not ready):
                raise ModeError("Compatible camera, model and resources must be ready", 409)
            if mode != self.requested or self.reason is not None:
                self._change(mode, reason=None if ready else "resources_unavailable")
            return self.status()

    def protect(self, reason):
        """Call on real-time faults, independently of logs and device ACK."""
        with self.lock:
            if self.requested == "tracking":
                self._change("eco", reason=reason)
            else:
                transient = reason in {"frames_expired", "cadence_low"}
                if transient and self.reason is not None and not self.eco_recoverable:
                    return  # Cadence must never replace a manual/resource latch.
                self.eco_recoverable = transient and (self.reason is None or self.eco_recoverable)
                self.state, self.reason = "degraded", reason

    def acknowledge(self, boot_id, generation, mode, applied_us):
        with self.lock:
            valid = (self.connected and boot_id == self.boot and type(boot_id) is int
                     and _uint(generation) and generation == self.version
                     and type(mode) is str and mode == self.requested
                     and _uint(applied_us, maximum=2**64 - 1))
            if not valid or self.ack is not None and self.ack != applied_us:
                self.rejected_ack += 1
                return False
            if self.ack is None and self.clock() - self.requested_at >= 5:
                self.protect("ack_timeout")
                self.rejected_ack += 1
                return False
            if self.ack is None:
                self.ack_at = self.clock()
            self.ack = applied_us
            return True  # Duplicate ACK does not erase frame/cadence evidence.

    def allows(self, frame):
        with self.lock:
            return (self.connected and self.ack is not None and frame.boot_id == self.boot
                    and frame.mode_generation == self.version and frame.captured_us > self.ack)

    def publish_result(self, frame, publish):
        """Fence both result publication and mode changes with the same lock.

        publish must be bounded, nonblocking, and must not call external IO.
        An already-published old result is never re-labelled after a change.
        """
        with self.lock:
            # A newly arriving result must not hide a gap that already expired
            # before the watchdog thread ran (including at the 500ms boundary).
            self.tick()
            if (not self.allows(frame) or self.frames and
                    (frame.sequence <= self.frames[-1][2] or frame.captured_us <= self.frames[-1][1])):
                self.rejected_result += 1
                return False
            now = self.clock()
            try:
                publish()
            except Exception:
                self.protect("publication_failed")
                raise
            self.frames.append((now, frame.captured_us, frame.sequence))
            if self.cadence_at is None:
                self.cadence_at = now
            if self.requested == "tracking":
                self.cadence_count += 1
            self._trim(now)
            self._reconcile()
            return True

    def _trim(self, now):
        window = 10 if self.requested == "tracking" else 18
        while self.frames and now - self.frames[0][0] >= window:
            self.frames.popleft()

    def _cadence(self):
        if self.requested == "tracking":
            # Completed consecutive [start, start+10) windows, including their
            # first result, avoid a false 49-count just before the next result.
            return self.cadence_rate is not None and self.cadence_rate >= 5
        recent = list(self.frames)[-3:]
        return (len(recent) == 3 and self.clock() - recent[-1][0] <= 7
                and all(4 <= b[0] - a[0] <= 6
                        for a, b in zip(recent, recent[1:], strict=False)))

    def _reconcile(self):
        if (self.connected and self.eco_recoverable and self.eco_recovery_ready
                and self.ack is not None and self._cadence()):
            self.reason, self.eco_recoverable = None, False
            self.automatic_recoveries += 1
        if self.connected and self.reason is None:
            self.state = "active" if self.ack is not None and self._cadence() else "pending"
            if self.requested == "eco" and self.state == "active":
                self.eco_confirmed = True

    def tick(self):
        with self.lock:
            if not self.connected:
                return
            now = self.clock()
            self._trim(now)
            if self.ack is None:
                if now - self.requested_at >= 5:
                    self.protect("ack_timeout")
                return
            limit = .5 if self.requested == "tracking" else ECO_GAP_LIMIT
            if self.frames:
                expired = now - self.frames[-1][0] > limit
            else:
                # Allow the first 190ms capture + inference, or first 5s eco frame.
                expired = now - self.ack_at > (1 if self.requested == "tracking" else ECO_GAP_LIMIT)
            if expired:
                self.protect("frames_expired")
            else:
                if (self.requested == "tracking" and self.cadence_at is not None
                        and now - self.cadence_at >= 10):
                    if now - self.cadence_at >= 20 or self.cadence_count < 50:
                        self.protect("cadence_low")
                        return
                    self.cadence_rate = self.cadence_count / 10
                    self.cadence_at += 10
                    self.cadence_count = 0
                self._reconcile()

    def command(self):
        with self.lock:
            self.tick()
            now = self.clock()
            if not self.connected or self.ack is not None or now - self.last_command < 1:
                return None
            self.last_command = now
            return {"type": "profile.set", "boot_id": self.boot,
                    "generation": self.version, "mode": self.requested}

    def status(self):
        with self.lock:
            self.tick()
            now = self.clock()
            span = self.frames[-1][0] - self.frames[0][0] if len(self.frames) >= 2 else 0
            rate = (len(self.frames) - 1) / span if span else 0
            if self.requested == "tracking" and self.cadence_rate is not None:
                rate = self.cadence_rate
            warning = (self.requested == "eco" and self.ack is not None and self.reason is None
                       and ((self.eco_confirmed and not self._cadence())
                            or now - (self.frames[-1][0] if self.frames else self.ack_at) > 7))
            return {"schema": 2, "version": self.version, "boot_id": self.boot,
                    "requested_mode": self.requested,
                    "effective_mode": self.requested if self.ack is not None and self.frames else None,
                    "state": self.state, "reason": self.reason, "connected": self.connected,
                    "eco_warning": warning, "eco_auto_recovery_pending": self.eco_recoverable,
                    "automatic_recoveries": self.automatic_recoveries,
                    "actuation_state": "observe_only" if self.state == "active" and self.requested == "tracking" else "hold",
                    "acknowledged": self.ack is not None,
                    "last_result_age_seconds": now - self.frames[-1][0] if self.frames else None,
                    "recognition_hz": rate,
                    "cadence_samples": len(self.frames), "transitions": self.transitions,
                    "rejected_ack": self.rejected_ack, "rejected_result": self.rejected_result}
