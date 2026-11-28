"""Opt-in JSONL records for investigating clock_expired drops.

Enabled only when ESP32CAM_CLOCK_DIAGNOSTICS is "1". Nothing here may raise or
influence admission, window or drop decisions.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock

from script.frame_protocol import CameraFrame

FILENAME = "clock-diagnostics.jsonl"


def _live(window, boot_id, now_us):
    """Mirror the sample filter in ClockWindow.age_bounds_us, newest first."""
    return [sample for sample in reversed(window.samples)
            if sample.boot_id == boot_id and 0 <= now_us - sample.received_us <= 10_000_000]


class ClockDiagnostics:
    def __init__(self):
        self._lock = Lock()
        self._handle = None

    @classmethod
    def from_env(cls):
        return cls() if os.environ.get("ESP32CAM_CLOCK_DIAGNOSTICS") == "1" else None

    def exchange(self, *, window, sample, rtt_us, received_us, admitted):
        try:
            ts = time.time_ns() // 1000
            # No frame belongs to a pong: judge a hypothetical one captured at
            # the device time it reports.
            probe = CameraFrame(sample.boot_id, 1, sample.device_us, sample.device_us, b"")
            bounds = window.age_bounds_us(probe, received_us)
            self._write({
                "type": "clock_exchange", "ts": ts, "rtt_us": rtt_us,
                "device_us": sample.device_us,
                "offset_low_us": sample.offset_low_us,
                "offset_high_us": sample.offset_high_us,
                "admitted": admitted,
                "reject_reason": None if admitted else "rtt_over_limit",
                "live_samples": len(_live(window, sample.boot_id, received_us)),
                "bounds_low_us": None if bounds is None else bounds[0],
                "bounds_high_us": None if bounds is None else bounds[1],
            })
        except Exception:  # Diagnostics must never affect the receiver.
            pass

    def expired(self, *, window, frame, received_us):
        try:
            ts = time.time_ns() // 1000
            samples = []
            for sample in _live(window, frame.boot_id, received_us):
                bounds = sample.age_bounds_us(frame, received_us)
                samples.append({"age_us": received_us - sample.received_us,
                                "low_us": None if bounds is None else bounds[0],
                                "high_us": None if bounds is None else bounds[1]})
            self._write({
                "type": "clock_expired_state", "ts": ts,
                "captured_us": frame.captured_us, "host_received_us": received_us,
                "samples": samples,
                "newest_is_none": bool(samples) and samples[0]["low_us"] is None,
            })
        except Exception:  # Diagnostics must never affect the receiver.
            pass

    def _write(self, record):
        line = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        with self._lock:
            if self._handle is None:
                directory = Path.home() / ".local/state/esp32cam/logs"
                # Private, as the log collector sharing this directory requires.
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                self._handle = os.fdopen(os.open(directory / FILENAME, flags, 0o600), "ab")
            self._handle.write(line)
            self._handle.flush()
