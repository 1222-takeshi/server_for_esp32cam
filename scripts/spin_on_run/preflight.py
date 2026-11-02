"""Start-of-experiment checks (plan sections 3, 10.1). Read-only; any failure forbids starting.

`O05` marks an environment that differs from the expected one; `O07` marks
something the experiment depends on that cannot be established at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import environment
from .common import FREE_MIN, HASHES, START_TEMP_MAX_MILLIC

CAMERA_BOOT = 18174604300954271633
LIVE_FREE_MIN = 2 * 1024**3


@dataclass
class Context:
    """Facts gathered by the caller; every field is a plain value."""

    snapshot: dict
    file_hashes: dict  # label -> sha256 of the file actually in use
    expected_hashes: dict  # label -> frozen sha256 from the prepared manifest
    free_bytes: int
    temp_millic: int
    live: bool = False
    linger: str | None = None
    recover_unit_sha: str | None = None
    recover_unit_expected_sha: str | None = None
    recover_enabled: bool = False
    recover_check_exit: int | None = None
    old_guard_conflict: bool | None = None
    notes: list[str] = field(default_factory=list)


def preflight(ctx: Context, baseline: dict | None = None) -> dict:
    o05: list[str] = []
    o07: list[str] = []
    snap = ctx.snapshot
    base = baseline or snap
    if snap.get("camera_boot_id") != CAMERA_BOOT:
        ctx.notes.append(
            f"camera boot id differs from the last confirmed: {snap.get('camera_boot_id')}"
        )
    o05 += environment.deviations(base, snap, "preflight")
    if snap.get("last_firmware_version") in (None, environment.UNVERIFIED):
        o07.append("firmware identity (device record read path) unverified")
    for label, expected in ctx.expected_hashes.items():
        if ctx.file_hashes.get(label) != expected:
            o05.append(f"hash:{label}")
    for label in ("model", "ort", "input", "golden"):
        if label in ctx.file_hashes and ctx.file_hashes[label] != HASHES[label]:
            o05.append(f"fixed_hash:{label}")
    if ctx.free_bytes < (LIVE_FREE_MIN if ctx.live else FREE_MIN):
        o05.append("free_space")
    if ctx.temp_millic > START_TEMP_MAX_MILLIC and not ctx.live:
        o05.append("start_temperature")
    if ctx.live:
        if ctx.linger != "yes":
            o05.append("linger_not_enabled")
        if not ctx.recover_enabled:
            o05.append("recover_unit_not_enabled")
        if ctx.recover_unit_sha != ctx.recover_unit_expected_sha:
            o05.append("recover_unit_sha")
        if ctx.recover_check_exit != 0:
            o05.append("recover_check_failed")
        if ctx.old_guard_conflict is not False:
            o07.append("conflict with the standing guard cannot be excluded")
    return {"ok": not (o05 or o07), "O05": o05, "O07": o07, "notes": ctx.notes}
