"""GitHub-hosted CI billing-failure classifier tests."""

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "ci" / "classify_hosted_jobs.py"
SPEC = importlib.util.spec_from_file_location("classify_hosted_jobs", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
CLASSIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLASSIFIER)
ClassificationError = CLASSIFIER.ClassificationError
classify_jobs = CLASSIFIER.classify_jobs


EXPECTED_JOBS = ("test (3.11)", "test (3.12)", "dependency-audit", "compile-esp32-example")
BILLING_MESSAGE = (
    "The job was not started because recent account payments have failed "
    "or your spending limit needs to be increased."
)


def job(
    name: str,
    conclusion: str,
    *,
    steps: list[dict] | None = None,
    runner_id: int = 42,
    annotations: list[dict] | None = None,
) -> dict:
    return {
        "id": sum(ord(char) for char in name),
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "steps": [{"name": "run", "conclusion": conclusion}] if steps is None else steps,
        "runner_id": runner_id,
        "annotations": annotations or [],
    }


def successful_jobs() -> list[dict]:
    return [job(name, "success") for name in EXPECTED_JOBS]


def billing_job(name: str) -> dict:
    return job(
        name,
        "failure",
        steps=[],
        runner_id=0,
        annotations=[{"annotation_level": "failure", "message": BILLING_MESSAGE}],
    )


def test_all_expected_jobs_succeed_without_fallback():
    result = classify_jobs(successful_jobs(), expected_names=EXPECTED_JOBS)

    assert result["decision"] == "hosted_success"
    assert result["fallback"] is False


def test_all_unstarted_billing_jobs_request_fallback():
    result = classify_jobs(
        [billing_job(name) for name in EXPECTED_JOBS],
        expected_names=EXPECTED_JOBS,
    )

    assert result["decision"] == "billing_fallback"
    assert result["fallback"] is True


def test_success_and_billing_failure_mix_requests_fallback():
    jobs = successful_jobs()
    jobs[1] = billing_job("test (3.12)")

    result = classify_jobs(jobs, expected_names=EXPECTED_JOBS)

    assert result["decision"] == "billing_fallback"


@pytest.mark.parametrize(
    "mutation",
    [
        {"annotations": []},
        {"runner_id": 99},
        {"steps": [{"name": "Set up job", "conclusion": "failure"}]},
        {
            "annotations": [
                {
                    "annotation_level": "failure",
                    "message": "The runner pool is currently unavailable.",
                }
            ]
        },
    ],
)
def test_zero_step_failure_requires_complete_billing_evidence(mutation):
    candidate = billing_job(EXPECTED_JOBS[0])
    candidate.update(mutation)
    jobs = successful_jobs()
    jobs[0] = candidate

    result = classify_jobs(jobs, expected_names=EXPECTED_JOBS)

    assert result["decision"] == "hard_failure"
    assert result["fallback"] is False


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "timed_out", "action_required"])
def test_non_success_terminal_conclusions_fail_closed(conclusion):
    jobs = successful_jobs()
    jobs[2] = job(EXPECTED_JOBS[2], conclusion)

    result = classify_jobs(jobs, expected_names=EXPECTED_JOBS)

    assert result["decision"] == "hard_failure"


@pytest.mark.parametrize(
    "bad_jobs",
    [
        successful_jobs()[:-1],
        successful_jobs() + [job(EXPECTED_JOBS[0], "success")],
        successful_jobs() + [job("unreviewed-job", "success")],
    ],
)
def test_missing_or_duplicate_expected_jobs_raise(bad_jobs):
    with pytest.raises(ClassificationError):
        classify_jobs(deepcopy(bad_jobs), expected_names=EXPECTED_JOBS)
