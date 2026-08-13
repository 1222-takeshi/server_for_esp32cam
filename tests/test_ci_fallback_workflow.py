"""Static safety checks for the self-hosted CI fallback workflow."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).parents[1]
FALLBACK_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-fallback.yml"
HOSTED_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RUN_CHECK = REPO_ROOT / "scripts" / "ci" / "run-check.sh"


def load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_fallback_is_triggered_only_from_ci_workflow_run():
    content = FALLBACK_WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_run:" in content
    assert 'workflows: ["CI"]' in content
    assert "pull_request_target:" not in content
    assert "pull_request:" not in content


def test_candidate_jobs_are_read_only_and_do_not_persist_checkout_credentials():
    workflow = load_workflow(FALLBACK_WORKFLOW)

    for job_name in ("fallback_test", "fallback_audit", "fallback_compile"):
        job = workflow["jobs"][job_name]
        assert job["permissions"] == {"contents": "read"}
        checkout_steps = [
            step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
        ]
        assert checkout_steps
        assert all(step["with"]["persist-credentials"] is False for step in checkout_steps)
        assert "checks" not in job["permissions"]


def test_gate_and_candidate_jobs_use_separate_labels():
    workflow = load_workflow(FALLBACK_WORKFLOW)

    gate = ["self-hosted", "linux", "x64", "esp32cam-ci-gate"]
    candidate = ["self-hosted", "linux", "x64", "esp32cam-ci-fallback"]
    assert workflow["jobs"]["inspect"]["runs-on"] == gate
    assert workflow["jobs"]["finalize"]["runs-on"] == gate
    for job_name in ("fallback_test", "fallback_audit", "fallback_compile"):
        assert workflow["jobs"][job_name]["runs-on"] == candidate


def test_expected_hosted_jobs_are_checked():
    content = FALLBACK_WORKFLOW.read_text(encoding="utf-8")

    for name in ("test (3.11)", "test (3.12)", "dependency-audit", "compile-esp32-example"):
        assert f'--expected "{name}"' in content
    assert "github.repository_owner" in content
    assert "checks: write" in content


def test_fallback_workflow_actions_are_pinned_to_commit_shas():
    workflow = load_workflow(FALLBACK_WORKFLOW)

    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            action = step.get("uses")
            if action:
                assert len(action.rsplit("@", 1)[-1]) == 40, action


def test_hosted_and_fallback_share_check_commands():
    hosted = HOSTED_WORKFLOW.read_text(encoding="utf-8")
    fallback = FALLBACK_WORKFLOW.read_text(encoding="utf-8")

    assert "scripts/ci/run-check.sh test" in hosted
    assert "scripts/ci/run-check.sh audit" in hosted
    assert "scripts/ci/run-check.sh test" in fallback
    assert "scripts/ci/run-check.sh audit" in fallback
    assert RUN_CHECK.exists()
