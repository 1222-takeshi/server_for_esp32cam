"""Frozen experiment constants and small file helpers shared by the harness."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

# Offline S: A = spinning off, B = run-only spinning. Four ABBA/BAAB blocks.
ORDER = "ABBABAABABBABAAB"
BLOCKS = (ORDER[0:4], ORDER[4:8], ORDER[8:12], ORDER[12:16])
ROWS_PER_RUN = 85
WARMUP_ROWS = 5
MEASURED_ROWS = 80
FRAME_GAP_US = 40_000
GAP_COUNT = MEASURED_ROWS  # gaps between row 4 -> 5, ..., 83 -> 84
START_TEMP_MAX_MILLIC = 70_000
START_TEMP_WAIT_S = 60
TEMP_STOP_MILLIC = 80_000
CHILD_LIMIT_S = 60
IDLE_BETWEEN_RUNS_S = 15
SIGTERM_GRACE_S = 5
MONITOR_MAX_AGE_S = 3
RUN_OUTPUT_LIMIT = 2 * 1024 * 1024
STDERR_LIMIT = 64 * 1024
JSON_LIMIT = 256 * 1024
FILE_LIMIT = 4 * 1024 * 1024
STAGE_LIMIT = 32 * 1024 * 1024
FREE_MIN = 1024**3
QUALITY_FIELDS = (
    "input_sha256",
    "raw_sha256",
    "detections",
    "runtime_build",
    "sequence",
    "boot_id",
    "captured_us",
    "sent_us",
    "mode_generation",
    "replay_only",
    "freshness_checked",
    "actuation_state",
)
HASHES = {
    "model": "ff1013da8734fe2b8ede5f34a2124855d447fbefd248d148e9f29e7f8e28c9c4",
    "ort": "ab8c4363e06ac80b3d1279ea55ebea44e906c5b131ba783ff684a067540c0e94",
    "input": "d9bda20070c002a3eb466571b0044d81029b41f2cbcf1635dc02385c76f28885",
    "golden": "00baa4577c85d91944721d2e95127cbc5d9f26f038cf6aa5b7dae651c9bf909c",
    "original_unit": "b900b7dbfbb1b4dc1da66f9c27f71bdd6ab881cc91ecb864182c6a01d68c3dea",
    # Candidate M runs this unmodified worker (plan section 6).
    "pr616_worker": "91a7e70992ec023aaf08a2e9eee3d05b8be57b204b50c6a5e8a27e77cd536c75",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_new(path: str | os.PathLike, data: bytes | str, mode: int = 0o400) -> Path:
    """Create a read-only file that must not exist yet (the `xb` rule)."""
    raw = data.encode() if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
    except BaseException:
        os.unlink(path)
        raise
    os.chmod(path, mode)
    return Path(path)


def write_json(path: str | os.PathLike, value: object) -> Path:
    return write_new(path, json.dumps(value, sort_keys=True, indent=1) + "\n")
