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
    "plan.md": "abce502d64486e5e360c2a0004af2a82d65200d2410994ceb33d694d84dd5460",
    "plan.json": "fb776d7d39ff7b2f6a8ee534d72265c87b71684eddfddc261aa0083143b28f0d",
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
        "live_env_keys": sorted(units.LIVE_ENV_KEYS),
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
