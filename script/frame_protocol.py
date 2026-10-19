"""Bounded ECF1 camera frames and monotonic-clock uncertainty for live capture."""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass


HEADER = struct.Struct(">4sQQQQI")
PROFILE_HEADER = struct.Struct(">4sQQQQQI")
MAX_GENERATION = 2**53 - 1
MAX_JPEG_BYTES = 128 * 1024
UINT64_MAX = 2**64 - 1


def _positive_int(value: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= UINT64_MAX


@dataclass(frozen=True, slots=True)
class CameraFrame:
    boot_id: int
    sequence: int
    captured_us: int
    sent_us: int
    jpeg: bytes
    mode_generation: int | None = None


def decode_frame(payload: bytes, *, max_jpeg_bytes: int = MAX_JPEG_BYTES,
                 allow_profiles: bool = False) -> CameraFrame:
    """Validate the envelope before copying its bounded JPEG payload.

    JPEG format/dimensions and authentication belong to the receiver. Timestamps
    are device monotonic microseconds, not UTC or server receive timestamps.
    """
    if not _positive_int(max_jpeg_bytes) or max_jpeg_bytes > 2**32 - 1:
        raise ValueError("Invalid JPEG size limit")
    if not isinstance(payload, bytes):
        raise ValueError("Invalid frame size")
    profile = payload[:4] == b"ECF2" and allow_profiles
    header = PROFILE_HEADER if profile else HEADER
    if not header.size < len(payload) <= header.size + max_jpeg_bytes:
        raise ValueError("Invalid frame size")
    if profile:
        magic, boot_id, sequence, captured_us, sent_us, generation, length = header.unpack_from(payload)
        if not 0 < generation <= MAX_GENERATION:
            raise ValueError("Invalid mode generation")
    else:
        magic, boot_id, sequence, captured_us, sent_us, length = header.unpack_from(payload)
        generation = None
    if magic != (b"ECF2" if profile else b"ECF1") or not boot_id or not sequence or not captured_us:
        raise ValueError("Invalid frame identity or timestamp")
    if length != len(payload) - header.size or sent_us < captured_us:
        raise ValueError("Invalid frame length or timestamp order")
    return CameraFrame(boot_id, sequence, captured_us, sent_us, payload[header.size:], generation)


class FrameOrder:
    """Single-camera ordering state; one owner must serialize observe() calls.

    Remember the last 32 retired boots. Older identities cannot be authenticated
    by this cache: the receiver must additionally bind frames to its connection's
    boot and reject expired/replayed clock exchanges.
    """

    def __init__(self) -> None:
        self.boot_id: int | None = None
        self.sequence = 0
        self.captured_us = 0
        self._retired: deque[int] = deque(maxlen=32)

    def observe(self, frame: CameraFrame) -> tuple[str, int]:
        """Return (classification, missing sequence count); reject without updates."""
        changed = frame.boot_id != self.boot_id
        if changed and frame.boot_id in self._retired:
            return "retired_boot", 0
        if not changed:
            if frame.sequence == self.sequence:
                return "duplicate", 0
            if frame.sequence < self.sequence or frame.captured_us <= self.captured_us:
                return "stale", 0
        previous = self.boot_id
        gap = 0 if changed else frame.sequence - self.sequence - 1
        if changed and previous is not None:
            self._retired.append(previous)
        self.boot_id = frame.boot_id
        self.sequence = frame.sequence
        self.captured_us = frame.captured_us
        return ("boot_changed" if changed and previous is not None else "new"), gap


@dataclass(frozen=True, slots=True)
class ClockSample:
    """Host-minus-device clock offset interval from one ping/pong exchange."""

    boot_id: int
    offset_low_us: int
    offset_high_us: int
    received_us: int
    device_us: int

    @classmethod
    def from_exchange(
        cls, boot_id: int, device_us: int, sent_us: int, received_us: int
    ) -> ClockSample:
        if not all(_positive_int(v) for v in (boot_id, device_us, sent_us, received_us)):
            raise ValueError("Invalid clock exchange")
        if not 0 <= received_us - sent_us <= 1_000_000:
            raise ValueError("Invalid clock round trip")
        return cls(boot_id, sent_us - device_us, received_us - device_us, received_us, device_us)

    def age_bounds_us(
        self, frame: CameraFrame, received_us: int, *, max_age_us: int = 10_000_000,
        drift_ppm: int = 200,
    ) -> tuple[int, int] | None:
        """Return a conservative frame age interval, or None if not trustworthy.

        drift_ppm bounds the host/device clock-rate ratio error, an assumption
        to validate on hardware;
        it is not a device guarantee. Expire samples instead of hiding uncertainty.
        """
        if not _positive_int(received_us) or not _positive_int(max_age_us):
            raise ValueError("Invalid clock age limit")
        if (isinstance(drift_ppm, bool) or not isinstance(drift_ppm, int)
                or not 0 <= drift_ppm < 1_000_000):
            raise ValueError("Invalid clock drift bound")
        elapsed = abs(received_us - self.received_us)
        if frame.boot_id != self.boot_id or elapsed > max_age_us:
            return None
        # Convert the capture's device timestamp using the exchange's offset.
        # Drift can accumulate *before* the exchange as well as after it.
        delta = abs(frame.captured_us - self.device_us)
        margin = (delta * drift_ppm + 999_999) // 1_000_000
        low = received_us - frame.captured_us - self.offset_high_us - margin
        high = received_us - frame.captured_us - self.offset_low_us + margin
        if high < 0:
            return None
        return max(0, low), high


class ClockWindow:
    """Intersect up to eight independently drift-bounded clock observations.

    A delayed exchange cannot discard still-valid tighter evidence. Each sample
    keeps its original expiry; new exchanges never renew an older sample.
    Empty intersections and definitely future captures fail closed.
    """

    def __init__(self) -> None:
        self.samples: deque[ClockSample] = deque(maxlen=8)

    def add(self, sample: ClockSample) -> None:
        if self.samples and sample.received_us < self.samples[-1].received_us:
            raise ValueError("Out-of-order clock exchange")
        if self.samples and sample.boot_id != self.samples[-1].boot_id:
            self.samples.clear()
        self.samples.append(sample)

    def age_bounds_us(self, frame: CameraFrame, received_us: int) -> tuple[int, int] | None:
        low, high = 0, None
        for sample in self.samples:
            if (sample.boot_id != frame.boot_id or sample.received_us > received_us
                    or received_us - sample.received_us > 10_000_000):
                continue
            bounds = sample.age_bounds_us(frame, received_us)
            if bounds is None:
                return None
            low = max(low, bounds[0])
            high = bounds[1] if high is None else min(high, bounds[1])
        return (low, high) if high is not None and low <= high else None
