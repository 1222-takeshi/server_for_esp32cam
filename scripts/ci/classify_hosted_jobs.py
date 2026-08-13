#!/usr/bin/env python3
"""Classify completed GitHub-hosted jobs without masking code failures."""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence


class ClassificationError(ValueError):
    """Raised when the hosted job response is incomplete or ambiguous."""


_PAYMENT_PATTERN = re.compile(r"account payments? (?:have )?failed", re.IGNORECASE)
_SPENDING_PATTERN = re.compile(
    r"spending limit (?:needs? to be increased|increase)", re.IGNORECASE
)


def _has_billing_annotation(job: dict[str, Any]) -> bool:
    annotations = job.get("annotations")
    if not isinstance(annotations, list):
        return False
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
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

    job_list = list(jobs)
    if any(not isinstance(job, dict) for job in job_list):
        raise ClassificationError("workflow job response contains a non-object")
    names = [job.get("name") for job in job_list]
    unexpected = sorted({str(name) for name in names if name not in expected})
    if unexpected:
        raise ClassificationError(
            f"workflow job response contains unexpected jobs: {unexpected}"
        )

    selected = [job for job in job_list if job.get("name") in expected]
    names = [str(job.get("name")) for job in selected]
    missing = sorted(set(expected) - set(names))
    duplicates = sorted(name for name in set(names) if names.count(name) > 1)
    if missing or duplicates:
        raise ClassificationError(
            f"expected job response is incomplete: missing={missing}, duplicates={duplicates}"
        )
    return selected


def classify_jobs(
    jobs: Iterable[dict[str, Any]], *, expected_names: Sequence[str]
) -> dict[str, Any]:
    """Return a fail-closed decision for the expected hosted jobs.

    Fallback is allowed only when every non-success job has complete evidence
    that GitHub rejected it before a runner started because of billing limits.
    """

    selected = _select_expected_jobs(jobs, expected_names)
    conclusions = {str(job["name"]): str(job.get("conclusion")) for job in selected}

    if all(
        job.get("status") == "completed" and job.get("conclusion") == "success"
        for job in selected
    ):
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
