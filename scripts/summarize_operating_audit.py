"""Summarize frozen v3 operating-audit evidence; never certify operating acceptance."""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile

MAX_BYTES = 8 * 1024**2
DURATION = 86400
PERIOD = 60
JITTER = 60  # Summary coverage only; does not change the observer or recognition gate.


def read_bounded(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Evidence exceeds its byte limit")
    return data


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Expected a finite nonnegative number")
    return value


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Expected a nonnegative integer")
    return value


def summarize(run: Path):
    names = {"manifest.json": 65536, "samples.jsonl": MAX_BYTES, "result.json": 65536}
    raw = {name: read_bounded(run / name, limit) for name, limit in names.items()
           if name != "result.json" or (run / name).exists()}
    manifest = json.loads(raw["manifest.json"], parse_constant=number)
    if (type(manifest["duration_seconds"]) is not int
            or type(manifest["period_seconds"]) is not int
            or manifest["duration_seconds"] != DURATION or manifest["period_seconds"] != PERIOD
            or manifest.get("wlan_power_save_recorded_per_sample") is not True):
        raise ValueError("Expected a 24h v3 audit with 60-second sampling")
    rows = [json.loads(line, parse_constant=number) for line in raw["samples.jsonl"].splitlines()]
    if not rows or len(rows) > DURATION // PERIOD + 1:
        raise ValueError("Invalid audit row count")
    times = [number(row["elapsed_seconds"]) for row in rows]
    if any(b <= a for a, b in zip(times[:-1], times[1:], strict=True)):
        raise ValueError("Audit samples must be strictly ordered")
    max_gap = max((b - a for a, b in zip(times[:-1], times[1:], strict=True)), default=0)
    millis = [Decimal(str(elapsed)) * 1000 for elapsed in times]
    coverage = (len(rows) == DURATION // PERIOD + 1 and millis[-1] >= DURATION * 1000 - 1
                and all(-1 <= elapsed - index * PERIOD * 1000 <= JITTER * 1000
                        for index, elapsed in enumerate(millis))
                and all(b - a <= (PERIOD + JITTER) * 1000
                        for a, b in zip(millis[:-1], millis[1:], strict=True)))
    errors = [row for row in rows if "error" in row]
    good = [row for row in rows if "error" not in row]
    if any(type(row["error"]) is not str or not row["error"] for row in errors):
        raise ValueError("Invalid error sample")
    states, reasons, wifi, commits, boots = (Counter() for _ in range(5))
    modes, models = Counter(), Counter()
    maximums = {"db": [], "wal": [], "managed_log": [], "temperature_c": []}
    free = []
    changed = missing_resources = paused = missing_temperature = 0
    for row in good:
        if (row["state"] not in ("active", "degraded", "pending", "unknown")
                or row["wlan_power_save"] not in ("on", "off")
                or type(row["deployment_changed_during_sample"]) is not bool
                or row["deployment_changed_during_sample"]
                != (row["deployment"] != row["deployment_after"])):
            raise ValueError("Invalid state or deployment fence")
        states[row["state"]] += 1
        reasons[str(row["reason"])] += 1
        wifi[row["wlan_power_save"]] += 1
        commits[row["deployment"]["commit"]] += 1
        boots[str(row["camera_boot_id"])] += 1
        modes[row["requested_mode"] + "/" + row["effective_mode"]] += 1
        models[row["model_sha256"]] += 1
        if type(row["resources"]["paused"]) is not bool:
            raise ValueError("Invalid resource pause state")
        paused += row["resources"]["paused"]
        changed += row["deployment_changed_during_sample"]
        maximums["managed_log"].append(count(row["managed_log_bytes"]))
        temperature = row["vitals"]["temperature_c"]
        if temperature is not None:
            maximums["temperature_c"].append(number(temperature))
        else:
            missing_temperature += 1
        sample = row["resources"]["sample"]
        if sample is None:
            missing_resources += 1
        else:
            for field in ("db", "wal"):
                maximums[field].append(count(sample[field]))
            free.append(count(sample["free"]))
        for field in ("service_process_write_bytes_delta", "observed_exited_processes",
                      "host_write_bytes_delta"):
            count(row[field])
    for field in ("service_process_write_bytes_delta", "observed_exited_processes",
                  "host_write_bytes_delta"):
        if any(b[field] < a[field] for a, b in zip(good[:-1], good[1:], strict=True)):
            raise ValueError("Cumulative counters decreased")
    result = json.loads(raw["result.json"], parse_constant=number) if "result.json" in raw else None
    if "result.json" in raw and type(result) is not dict:
        raise ValueError("Result must be an object")
    completed = False
    if result is not None:
        if (type(result["completed"]) is not bool
                or count(result["samples"]) != len(good)
                or count(result["errors"]) != len(errors)):
            raise ValueError("Result counters disagree with raw samples")
        if number(result["elapsed_seconds"]) < times[-1]:
            raise ValueError("Result predates the final sample")
        completed = result["completed"] and result["elapsed_seconds"] >= DURATION and coverage
    last = good[-1] if good else None
    output = {
        "schema_version": 1, "observation_completed": completed,
        "observer_result_completed": result["completed"] if result else False,
        "sampling_coverage_complete": coverage, "sample_jitter_allowance_seconds": JITTER,
        "normal_load_24h_accepted": False, "tracking_24h_accepted": False,
        "watts_measured": False, "rows": len(rows), "success_rows": len(good),
        "error_rows": len(errors), "last_sample_elapsed_seconds": times[-1],
        "max_sample_gap_seconds": max_gap,
        "state_counts": dict(states), "reason_counts": dict(reasons),
        "wlan_power_save_counts": dict(wifi), "wifi_unknown_error_rows": len(errors),
        "source_commit_counts": dict(commits), "camera_boot_counts": dict(boots),
        "requested_effective_mode_counts": dict(modes), "model_sha256_counts": dict(models),
        "resource_paused_rows": paused, "temperature_missing_rows": missing_temperature,
        "deployment_changed_rows": changed, "resource_sample_missing_rows": missing_resources,
        "observed_maximums": {k: max(v, default=None) for k, v in maximums.items()},
        "minimum_free_bytes": min(free, default=None),
        "service_write_bytes_lower_bound": last["service_process_write_bytes_delta"] if last else None,
        "observed_exited_processes": last["observed_exited_processes"] if last else None,
        "whole_host_write_bytes_at_last_sample": last["host_write_bytes_delta"] if last else None,
        "evidence_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in raw.items()},
        "interpretation": "Row counts are not time uptime. Process exits can hide service writes. "
                          "Host writes include other services. Observed manual modes and protection "
                          "are not a worst-case cat/tracking load or a field accuracy/power test.",
    }
    # Reject a growing/changing source rather than publishing a mixed snapshot.
    if any(read_bounded(run / name, names[name]) != data for name, data in raw.items()):
        raise ValueError("Evidence changed during summary")
    if ((run / "result.json").exists()) != ("result.json" in raw):
        raise ValueError("Completion changed during summary")
    return output


def write_output(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".audit-summary-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic publication; fails if any output already exists.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() in {args.run.joinpath(n).resolve() for n in
                                     ("manifest.json", "samples.jsonl", "result.json")}:
            raise ValueError("Output must not replace an evidence path")
        output = summarize(args.run)
        if args.require_complete and not output["observation_completed"]:
            print("Audit is not complete; no output written", file=sys.stderr)
            return 3
        write_output(args.output, output)
    except (OSError, ValueError, KeyError, TypeError):
        print("Operating audit summary could not be produced", file=sys.stderr)
        return 2
    print("completed_observation" if output["observation_completed"] else "partial_observation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
