#!/usr/bin/env python3
"""Summarize a bounded seven-day observation window from frozen JSONL copies."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 8192
MAX_INPUT_LINES = 50000
MIN_DURATION_SECONDS = 7 * 24 * 60 * 60
JITTER_SECONDS = 60
MIN_SAMPLE_RATIO = 0.99
MIN_COUNT_COVERAGE = 1.0
ERROR_CODES = frozenset({"unreachable", "http_status", "invalid_response", "response_too_large"})


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("timestamp must be ISO 8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be ISO 8601 UTC")
    return parsed.astimezone(timezone.utc)


def _state(value: object, allowed: tuple[str, ...]) -> str | None:
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    error = value.get("error")
    if status in allowed and error is None:
        return status
    if status is None and error in ERROR_CODES:
        return "monitor_failure"
    return None


def _read_sources(paths: list[Path]) -> tuple[list[bytes], list[dict[str, object]]]:
    lines: list[bytes] = []
    sources: list[dict[str, object]] = []
    for index, path in enumerate(paths):
        with path.open("rb") as stream:
            data = stream.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("each input must be at most 8 MiB")
        line_count = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        if line_count > MAX_INPUT_LINES:
            raise ValueError("too many input lines")
        file_lines = data.split(b"\n") if data else []
        if data.endswith(b"\n"):
            file_lines.pop()
        lines.extend(file_lines)
        sources.append({"input_index": index, "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data)})
    return lines, sources


def summarize(
    paths: list[Path], start: datetime, end: datetime, camera_ids: list[str],
    interval_seconds: int = 300, *, now: datetime | None = None,
) -> dict[str, object]:
    if not 1 <= len(paths) <= 2 or not camera_ids or len(camera_ids) > 32:
        raise ValueError("provide 1-2 input files and 1-32 camera IDs")
    if any(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", camera_id) is None for camera_id in camera_ids):
        raise ValueError("invalid camera ID")
    if len(set(camera_ids)) != len(camera_ids):
        raise ValueError("camera IDs must be unique")
    if not 1 <= interval_seconds <= 3600 or end <= start:
        raise ValueError("invalid interval or window")
    if any(value.tzinfo is None or value.utcoffset() != timedelta(0) for value in (start, end)):
        raise ValueError("window bounds must be UTC")

    lines, sources = _read_sources(paths)
    counts: dict[str, object] = {
        "lines_total": len(lines), "outside_before": 0, "outside_after": 0,
        "invalid": 0, "duplicate_timestamps": 0, "valid_samples": 0,
        "readiness": {"ready": 0, "degraded": 0, "monitor_failure": 0},
        "cameras": {camera_id: {"healthy": 0, "stale": 0, "offline": 0,
                                "monitor_failure": 0} for camera_id in camera_ids},
        "gap_count": 0, "max_gap_seconds": 0,
    }
    samples: dict[datetime, tuple[str, dict[str, str]]] = {}
    for line in lines:
        try:
            if len(line) > MAX_LINE_BYTES:
                raise ValueError("oversized line")
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("timestamp_utc"), str):
                raise ValueError("invalid record")
            timestamp = parse_utc(record["timestamp_utc"])
            if timestamp < start:
                counts["outside_before"] += 1
                continue
            if timestamp >= end:
                counts["outside_after"] += 1
                continue
            ready = _state(record.get("readiness"), ("ready", "degraded"))
            cameras = record.get("cameras")
            if ready is None or not isinstance(cameras, dict):
                raise ValueError("invalid state")
            camera_states = {
                camera_id: _state(cameras.get(camera_id), ("healthy", "stale", "offline"))
                for camera_id in camera_ids
            }
            if any(state is None for state in camera_states.values()):
                raise ValueError("invalid camera state")
        except (UnicodeDecodeError, ValueError, TypeError, KeyError):
            counts["invalid"] += 1
            continue
        if timestamp in samples:
            counts["duplicate_timestamps"] += 1
            continue
        samples[timestamp] = (ready, camera_states)

    ordered = sorted(samples)
    counts["valid_samples"] = len(ordered)
    for timestamp in ordered:
        ready, camera_states = samples[timestamp]
        counts["readiness"][ready] += 1
        for camera_id, state in camera_states.items():
            counts["cameras"][camera_id][state] += 1

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ValueError("evaluation time must be UTC")
    measured_end = min(current, end)
    elapsed_ordered = [timestamp for timestamp in ordered if timestamp <= measured_end]
    tolerance = interval_seconds + JITTER_SECONDS
    if measured_end <= start:
        spans = []
    elif elapsed_ordered:
        spans = ([(elapsed_ordered[0] - start).total_seconds()]
                 + [(right - left).total_seconds()
                    for left, right in zip(elapsed_ordered[:-1], elapsed_ordered[1:], strict=True)]
                 + [(measured_end - elapsed_ordered[-1]).total_seconds()])
    else:
        spans = [(measured_end - start).total_seconds()]
    counts["gap_count"] = sum(span > tolerance for span in spans)
    counts["max_gap_seconds"] = round(max(spans, default=0))

    elapsed = max(0, (measured_end - start).total_seconds())
    sample_count = len(ordered)
    expected_samples = math.ceil((end - start).total_seconds() / interval_seconds)
    count_coverage = min(1.0, sample_count / expected_samples)
    progress_expected = math.ceil(elapsed / interval_seconds)
    progress_coverage = min(1.0, len(elapsed_ordered) / progress_expected) if progress_expected else 0
    readiness_ratio = counts["readiness"]["ready"] / sample_count if sample_count else 0
    camera_ratios = {camera_id: states["healthy"] / sample_count if sample_count else 0
                     for camera_id, states in counts["cameras"].items()}
    ratios = {
        "readiness_ready": round(readiness_ratio, 6),
        "camera_healthy": {camera_id: round(ratio, 6)
                           for camera_id, ratio in camera_ratios.items()},
    }
    reasons = []
    if (end - start).total_seconds() < MIN_DURATION_SECONDS:
        reasons.append("window_shorter_than_seven_days")
    if current < end:
        reasons.append("window_not_elapsed")
    reasons.extend(field for field in ("invalid", "duplicate_timestamps", "gap_count") if counts[field])
    if readiness_ratio < MIN_SAMPLE_RATIO:
        reasons.append("readiness_sample_ratio_below_threshold")
    if any(ratio < MIN_SAMPLE_RATIO for ratio in camera_ratios.values()):
        reasons.append("camera_sample_ratio_below_threshold")
    if count_coverage < MIN_COUNT_COVERAGE:
        reasons.append("sample_count_below_expected")
    status = "pending" if current < end else "passed" if not reasons else "failed"
    return {
        "schema_version": 1,
        "evaluated_at_utc": current.isoformat(),
        "configuration": {
            "start_utc": start.isoformat(), "end_utc": end.isoformat(),
            "window": "[start,end)", "interval_seconds": interval_seconds,
            "jitter_seconds": JITTER_SECONDS, "minimum_duration_seconds": MIN_DURATION_SECONDS,
            "minimum_sample_ratio": MIN_SAMPLE_RATIO,
            "minimum_count_coverage": MIN_COUNT_COVERAGE, "camera_ids": camera_ids,
        },
        "sources": sources, "counts": counts, "sample_ratios": ratios,
        "sampling_coverage": {"expected_samples": expected_samples,
                              "observed_unique_samples": sample_count,
                              "count_coverage_ratio": round(count_coverage, 6),
                              "elapsed_expected_samples": progress_expected,
                              "elapsed_observed_unique_samples": len(elapsed_ordered),
                              "progress_count_coverage_ratio": round(progress_coverage, 6)},
        "elapsed_seconds": round(elapsed), "status": status, "reasons": reasons,
        "pending_future_tail_seconds": round(max(0, (end - current).total_seconds())),
        "interpretation": "Sample ratios are not time uptime.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--camera-id", action="append", required=True)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = summarize(args.input, parse_utc(args.start), parse_utc(args.end),
                           args.camera_id, args.interval_seconds)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, ValueError):
        print("observation summary could not be produced", file=sys.stderr)
        return 2
    print(result["status"])
    return {"passed": 0, "failed": 1, "pending": 3}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
