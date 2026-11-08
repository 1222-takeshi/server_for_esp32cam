"""Prepared manifest: SHA256 of every frozen file plus the fixed field mapping (plan section 4)."""

from __future__ import annotations

import json
from pathlib import Path

from . import common, live_checker, units

FROZEN_FILES = (
    "__init__.py",
    "common.py",
    "metrics.py",
    "quality.py",
    "environment.py",
    "offline_runner.py",
    "post_runner.py",
    "thread_cpu.py",
    "live_checker.py",
    "interval_gate.py",
    "interval_classify.py",
    "r2_sampler.py",
    "r2_remote.sh",
    "live_guardian.py",
    "recover_guard.py",
    "units.py",
    "preflight.py",
    "start_gate.py",
    "system_ops.py",
    "cli.py",
    "manifest.py",
)
PLAN = {
    "plan.md": "d0a25cbc8009135a6e1186740f105cc1ebd5ae6e5e9d35697490bafcf55c8955",
    "plan.json": "5da016646d2fb619926e9c6f07edb49fea7f30cbcaa95395ae11c3f09f13f87d",
}


def frozen_environment() -> dict:
    """What the live launch environment may contain, frozen with the files (spec D7)."""
    return {
        "live_env_keys": sorted(units.LIVE_ENV_KEYS),
        "frame_log": {"env": units.FRAME_LOG_KEY, "file": units.FRAME_LOG_NAME},
    }


def build(
    package_dir: Path, binaries: dict[str, Path], base_commit: str, issue: int | None
) -> dict:
    """`binaries`: label -> path of frame-worker / frame-live built from the merge commit."""
    return {
        "schema": 1,
        "plan_sha256": PLAN,
        "base_commit": base_commit,
        "implementation_issue": issue,
        "files": {name: common.sha256_file(package_dir / name) for name in FROZEN_FILES},
        "binaries": {label: common.sha256_file(path) for label, path in binaries.items()},
        **frozen_environment(),
        "live_env_credential_keys": sorted(units.CREDENTIAL_KEYS),
        "field_map": live_checker.FIELD_MAP,
        "ort_api": "SessionBuilder::with_config_entry (ort 2.0.0-rc.11, session/builder/mod.rs:141)",
        "per_session_pool_log": "not recorded here: Pi-side INFO log, outside the decision",
    }


def verify(manifest: dict, package_dir: Path, binaries: dict[str, Path]) -> list[str]:
    """Names whose current SHA256 differs from the frozen one."""
    changed = [
        name
        for name, sha in manifest["files"].items()
        if common.sha256_file(package_dir / name) != sha
    ]
    changed += [
        f"binary:{label}"
        for label, sha in manifest["binaries"].items()
        if label not in binaries or common.sha256_file(binaries[label]) != sha
    ]
    return changed


def dump(manifest: dict) -> str:
    return json.dumps(manifest, sort_keys=True, indent=1) + "\n"
