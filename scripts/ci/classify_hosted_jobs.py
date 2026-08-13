#!/usr/bin/env python3
"""Classify GitHub-hosted CI jobs without masking code failures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence


class ClassificationError(ValueError):
    """Raised when the job response is incomplete or ambiguous."""


_PAYMENT_PATTERN = re.compile(r"account payments? (?:have )?failed", re.IGNORECASE)
_SPENDING_PATTERN = re.compile(
    r"spending limit (?:needs? to be increased|increase)", re.IGNORECASE
)


def _has_billing_annotation(job: dict[str, Any]) -> bool:
    for annotation in job.get("annotations") or []:
        if annotation.get("annotation_level") != "failure":
            continue
        message = str(annotation.get("message") or "")
        if _PAYMENT_PATTERN.search(message) and _SPENDING_PATTERN.search(message):
            return True
    return False


def _is_billing_failure(job: dict[str, Any]) -> bool:
    return (
        job.get("status") == "completed"
        and job.get("conclusion") == "failure"
        and job.get("steps") == []
        and job.get("runner_id") == 0
        and _has_billing_annotation(job)
    )


def _select_expected_jobs(
    jobs: Iterable[dict[str, Any]], expected_names: Sequence[str]
) -> list[dict[str, Any]]:
    expected = tuple(expected_names)
    if not expected or len(set(expected)) != len(expected):
        raise ClassificationError("expected job names must be unique and non-empty")

    all_jobs = list(jobs)
    selected = [job for job in all_jobs if job.get("name") in expected]
    names = [str(job.get("name")) for job in selected]
    missing = sorted(set(expected) - set(names))
    duplicates = sorted(name for name in set(names) if names.count(name) > 1)
    unexpected = sorted(
        {str(job.get("name")) for job in all_jobs if job.get("name") not in expected}
    )
    if missing or duplicates or unexpected:
        raise ClassificationError(
            "expected job response is incomplete: "
            f"missing={missing}, duplicates={duplicates}, unexpected={unexpected}"
        )
    return selected


def classify_jobs(
    jobs: Iterable[dict[str, Any]], *, expected_names: Sequence[str]
) -> dict[str, Any]:
    """Return a machine-readable, fail-closed decision for expected hosted jobs."""

    selected = _select_expected_jobs(jobs, expected_names)
    conclusions = {str(job["name"]): str(job.get("conclusion")) for job in selected}

    if all(job.get("status") == "completed" and job.get("conclusion") == "success" for job in selected):
        return {
            "decision": "hosted_success",
            "fallback": False,
            "reason": "all expected GitHub-hosted jobs succeeded",
            "jobs": conclusions,
        }

    failures = [job for job in selected if job.get("conclusion") != "success"]
    if failures and all(_is_billing_failure(job) for job in failures):
        return {
            "decision": "billing_fallback",
            "fallback": True,
            "reason": "GitHub-hosted jobs were not started because of billing limits",
            "jobs": conclusions,
        }

    return {
        "decision": "hard_failure",
        "fallback": False,
        "reason": "one or more GitHub-hosted jobs failed without complete billing evidence",
        "jobs": conclusions,
    }


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Enriched jobs JSON file")
    parser.add_argument(
        "--expected",
        action="append",
        required=True,
        help="Expected GitHub-hosted job name; repeat for each job",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        jobs = payload["jobs"] if isinstance(payload, dict) else payload
        if not isinstance(jobs, list):
            raise ClassificationError("input must contain a jobs list")
        result = classify_jobs(jobs, expected_names=args.expected)
    except (OSError, json.JSONDecodeError, KeyError, ClassificationError) as exc:
        print(json.dumps({"decision": "error", "fallback": False, "reason": str(exc)}))
        return 2

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
