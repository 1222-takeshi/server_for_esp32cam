"""Offline follow-up unit and the final fail-closed check (plan section 10.4).

`run_post` runs after the offline unit stopped: it confirms the unit is gone,
saves `after.json` and compares the environment with the baseline. The unit's
`ExecStopPost` calls `finalize_post`; the independent `final_check` runs once
the follow-up unit is inactive and settles O08 even if neither of them ran.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import common, environment, metrics

V1_CODES = {"O02", "O04", "O05", "O06", "O07", "O08"}


def run_post(
    out_dir: Path,
    collector: environment.Collector,
    baseline: dict,
    *,
    offline_unit: str,
    post_unit: str,
    frozen_changes: list[str] | None = None,
) -> dict:
    """Steps 3 to 6. The only unit allowed to be active here is this one.

    `frozen_changes` lists files that differ from the prepared manifest; any
    entry fails the follow-up (O08), since its own code is then not the frozen code.
    """
    result = {"pass": False, "reasons": [f"frozen:{n}" for n in frozen_changes or []]}
    try:
        snapshot = collector.snapshot()
        common.write_json(out_dir / "after.json", snapshot)
        if offline_unit in snapshot["units_active"]:
            result["reasons"].append("offline_unit_active")
        found, result["mode_recheck"] = environment.deviations_with_recheck(
            baseline, snapshot, "restored", collector.snapshot, allowed_units=(post_unit,)
        )
        result["reasons"] += found
        result["pass"] = not result["reasons"]
    except Exception as error:  # noqa: BLE001 - every failure must be recorded
        result["reasons"].append(f"post_error:{type(error).__name__}")
    common.write_json(out_dir / "post-result.json", result)
    return result


def finalize_post(out_dir: Path, service_result: str) -> str | None:
    """Writes `post-o08.json` when the follow-up unit did not finish cleanly."""
    path = out_dir / "post-result.json"
    reason = None
    if service_result != "success":
        reason = "timeout" if service_result == "timeout" else "exit-code"
    elif not path.exists():
        reason = "no-result"
    elif not json.loads(path.read_text()).get("pass"):
        reason = "failed"
    if reason and not (out_dir / "post-o08.json").exists():
        common.write_json(out_dir / "post-o08.json", {"reason": reason})
    return reason


def final_check(
    out_dir: Path,
    collector: environment.Collector,
    baseline: dict,
    frozen_changes: list[str] | None = None,
) -> dict:
    """Second, fail-closed settlement of O06/O08 and the final verdict."""
    extra, notes = [], []
    if frozen_changes:
        extra.append("O08")
        notes += [f"frozen:{name}" for name in frozen_changes]
    result_path = out_dir / "result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else None
    if result is None:
        extra.append("O06")
        notes.append("result.json missing")
    if not (out_dir / "marker").exists():
        extra.append("O06")
        notes.append("unit ended without a completion marker")
    post_path = out_dir / "post-result.json"
    if (out_dir / "post-o08.json").exists():
        extra.append("O08")
        notes.append("post unit reported failure")
    elif not post_path.exists():
        extra.append("O08")
        notes.append("post-result.json missing")
    elif not json.loads(post_path.read_text()).get("pass"):
        extra.append("O08")
        notes.append("post-result.json did not pass")
    try:
        deviation, recheck = environment.deviations_with_recheck(
            baseline, collector.snapshot(), "restored", collector.snapshot
        )
    except Exception as error:  # noqa: BLE001
        deviation = [f"final_error:{type(error).__name__}"]
        recheck = {"rechecked": False, "error": type(error).__name__}
    if deviation:
        extra.append("O08")
        notes += deviation
    codes = list(result["codes"]) if result else []
    for code in extra:
        if code not in codes:
            codes.append(code)
    original = (result or {}).get("verdict", {"id": "V1", "result": "indeterminate"})
    verdict = original
    if V1_CODES & set(codes) or original["id"] == "V6" and extra:
        verdict = {"id": "V1", "result": "indeterminate"}
    final = {
        "codes": codes,
        "notes": notes,
        "verdict": verdict,
        "stop_codes_known": all(c in metrics.STOP_RESULT_ROW for c in codes),
        "mode_recheck": recheck,
    }
    common.write_json(out_dir / "final.json", final)
    return final
