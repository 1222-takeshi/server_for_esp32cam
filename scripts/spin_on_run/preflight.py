"""Start-of-experiment checks (plan sections 3, 10.1). Read-only; any failure forbids starting.

`O05` marks an environment that differs from the expected one; `O07` marks
something the experiment depends on that cannot be established at all.
"""

from __future__ import annotations

from collections.abc import Callable
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
    # Re-reads the environment; with it, a lone `pending` mode is rechecked.
    read_snapshot: Callable[[], dict] | None = None
    recheck_sleep: Callable[[float], None] | None = None
    recheck_clock: Callable[[], float] | None = None
    # Units that are this run's own (exact names); anything else under esp32cam-* is unexpected.
    allowed_units: tuple[str, ...] = ()
    linger: str | None = None
    recover_unit_sha: str | None = None
    recover_unit_expected_sha: str | None = None
    recover_enabled: bool = False
    recover_check_exit: int | None = None
    old_guard_conflict: bool | None = None
    # v2: the launch environment must name <stage dir>/frames.jsonl for the R3 frame log.
    frame_log_expected: str | None = None
    frame_log_actual: str | None = None
    launch_env_readable: bool = True
    frame_log_exists: bool = False
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
    try:
        found, recheck = environment.deviations_with_recheck(
            base,
            snap,
            "preflight",
            ctx.read_snapshot or (lambda: snap),
            allowed_units=ctx.allowed_units,
            recheck=ctx.read_snapshot is not None,
            sleep=ctx.recheck_sleep,
            clock=ctx.recheck_clock,
        )
    except Exception as error:  # noqa: BLE001 - an unreadable recheck forbids the start
        found = ["mode_not_restored", f"mode_recheck_error:{type(error).__name__}"]
        recheck = {"rechecked": True, "error": type(error).__name__}
    o05 += found
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
        if ctx.frame_log_expected is not None:
            if not ctx.launch_env_readable:
                o05.append("launch_env_unreadable")
            elif ctx.frame_log_actual != ctx.frame_log_expected:
                o05.append("frame_log_env")
            if ctx.frame_log_exists:
                o05.append("frame_log_exists")
    return {
        "ok": not (o05 or o07),
        "O05": o05,
        "O07": o07,
        "notes": ctx.notes,
        "mode_recheck": recheck,
    }
