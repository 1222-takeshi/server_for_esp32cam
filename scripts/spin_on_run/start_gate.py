"""The start gate every offline and live run passes before it may touch anything.

It joins the read-only environment checks (`preflight`) with the frozen-file check
(`manifest.verify`). Any failure forbids the start: nothing is launched and the
refusal is written down. The gate only reads; it never changes the environment.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from . import common, environment, live_checker, manifest, metrics, preflight, recover_guard, units

BINARY_LABELS = ("frame-worker", "frame-live")
FIXED_FILES = ("model", "ort", "input", "golden")
LOGIND_USER = "ubuntu"
REFUSED_EXIT = 2


def binaries(cfg: dict) -> dict[str, Path]:
    """Label -> path of the executables. These are the very files that get run."""
    found = cfg.get("binaries") or {}
    missing = [label for label in BINARY_LABELS if label not in found]
    if missing:
        raise KeyError(f"config.binaries lacks {missing}")
    return {label: Path(found[label]) for label in BINARY_LABELS}


def load_manifest(cfg: dict) -> dict:
    return json.loads(Path(cfg["manifest"]).read_text())


def frozen_changes(cfg: dict, package_dir: Path | None = None) -> list[str]:
    """Everything that differs from the prepared manifest; empty means unchanged."""
    package = package_dir or Path(__file__).parent
    try:
        frozen = load_manifest(cfg)
        changed = manifest.verify(frozen, package, binaries(cfg))
    except (OSError, ValueError, KeyError) as error:
        return [f"manifest_unreadable:{type(error).__name__}"]
    if frozen.get("plan_sha256") != manifest.PLAN:
        changed.append("plan_sha256")
    if frozen.get("field_map") != live_checker.FIELD_MAP:
        changed.append("field_map")
    for key, current in manifest.frozen_environment().items():
        if frozen.get(key) != current:
            changed.append(key)
    return changed


def _sha(path: str | None) -> str:
    try:
        return common.sha256_file(path)
    except (OSError, TypeError):
        return "unreadable"


def _unit_state(run: Callable[[list[str]], str], name: str, verb: str) -> str | None:
    try:
        return run(["systemctl", "--user", verb, name]).strip()
    except Exception:  # noqa: BLE001 - `is-enabled`/`is-active` exit non-zero when off
        return None


def _old_guard_conflict(run, names: list[str] | None) -> bool | None:
    """True when a named standing guard is enabled or active; None when not checkable."""
    if names is None:
        return None
    for name in names:
        if _unit_state(run, name, "is-active") == "active":
            return True
        if _unit_state(run, name, "is-enabled") in {"enabled", "enabled-runtime"}:
            return True
    return False


def evaluate(
    cfg: dict,
    *,
    live: bool,
    thread: bool = False,
    collector: environment.Collector,
    baseline: dict,
    out_dir: Path,
    free_bytes: Callable[[Path], int],
    temp_millic: Callable[[], int],
    run: Callable[[list[str]], str] = environment.run_read_only,
    ops=None,
    package_dir: Path | None = None,
) -> dict:
    """Gather the facts, run `preflight.preflight` and add the frozen-file check."""
    notes: list[str] = []
    try:
        snapshot = collector.snapshot()
    except Exception as error:  # noqa: BLE001 - an unreadable environment forbids the start
        snapshot = {"last_firmware_version": environment.UNVERIFIED}
        notes.append(f"snapshot_unavailable:{type(error).__name__}")
    hashes = {label: _sha(cfg.get(label)) for label in FIXED_FILES}
    expected = {}
    if thread:  # candidate M runs the registered unmodified worker
        hashes["pr616_worker"] = _sha(cfg.get("pr616_worker"))
        expected["pr616_worker"] = common.HASHES["pr616_worker"]
    ctx = preflight.Context(
        snapshot=snapshot,
        file_hashes=hashes,
        expected_hashes=expected,
        free_bytes=free_bytes(out_dir if out_dir.exists() else out_dir.parent),
        temp_millic=temp_millic(),
        live=live,
        notes=notes,
    )
    if live:
        unit_path = Path(cfg["recover_unit_path"])
        try:
            ctx.recover_unit_sha = common.sha256_file(unit_path)
        except OSError:
            ctx.recover_unit_sha = None
        ctx.recover_unit_expected_sha = hashlib.sha256(
            units.recover_unit_text(
                cfg["trial_unit"], cfg["root"], cfg["config_path"], str(out_dir)
            ).encode()
        ).hexdigest()
        ctx.recover_enabled = _unit_state(run, units.RECOVER_UNIT, "is-enabled") == "enabled"
        try:
            linger = run(["loginctl", "show-user", LOGIND_USER, "-p", "Linger"]).strip()
        except Exception:  # noqa: BLE001
            linger = ""
        ctx.linger = linger.partition("=")[2] or None
        problems = ["no_ops"] if ops is None else recover_guard.check_only(ops, baseline)
        ctx.recover_check_exit = 0 if not problems else 1
        ctx.old_guard_conflict = _old_guard_conflict(run, cfg.get("old_guard_units"))
        if "launch_env" in cfg:
            ctx.frame_log_expected = units.frame_log_path(str(out_dir))
            try:
                launch_env = json.loads(Path(cfg["launch_env"]).read_text())
                ctx.frame_log_actual = launch_env.get(units.FRAME_LOG_KEY)
            except (OSError, ValueError, AttributeError):
                ctx.launch_env_readable = False
            ctx.frame_log_exists = Path(ctx.frame_log_expected).exists()
    result = preflight.preflight(ctx, baseline)
    changed = frozen_changes(cfg, package_dir)
    result["O05"] += [f"frozen:{name}" for name in changed]
    result["ok"] = result["ok"] and not changed
    result["live"] = live
    return result


def refuse_offline(out_dir: Path, result: dict) -> dict:
    """Records the refusal in the shape the follow-up and the final check read."""
    out_dir.mkdir(parents=True, exist_ok=True)
    codes = [code for code in ("O05", "O07") if result.get(code)]
    verdict = metrics.final_verdict(codes, None, None, quality_ok=False, environment_ok=False)
    refusal = {"marker": "abort " + ",".join(codes), "codes": codes, "verdict": verdict}
    common.write_json(out_dir / "preflight.json", result)
    common.write_new(out_dir / "marker", refusal["marker"] + "\n")
    common.write_json(out_dir / "result.json", {**refusal, "details": [], "runs_completed": 0})
    return refusal


def refuse_live(run_dir: Path, result: dict) -> None:
    """The trial never starts: no child, no heartbeat; only the reasons are kept."""
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    common.write_json(run_dir / "start-refused.json", result)
