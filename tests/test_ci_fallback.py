import sys
from pathlib import Path
import unittest


CI_DIR = Path(__file__).resolve().parents[1] / "scripts" / "ci"
sys.path.insert(0, str(CI_DIR))

from classify_hosted_jobs import classify_jobs  # noqa: E402


EXPECTED = ("test", "dependency-audit", "compile-esp32-example")
HOSTED_EXPECTED = ("test (3.11)", "test (3.12)", "dependency-audit", "compile-esp32-example")


def _job(name, *, conclusion="success", billing=False):
    annotation = {
        "annotation_level": "failure",
        "message": "Account payments have failed, and the spending limit needs to be increased.",
    }
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "steps": [] if billing else [{"name": "run", "conclusion": conclusion}],
        "runner_id": 0 if billing else 123,
        "annotations": [annotation] if billing else [],
    }


class HostedJobClassificationTests(unittest.TestCase):
    def test_all_billing_failures_are_eligible_for_fallback(self):
        jobs = [_job(name, conclusion="failure", billing=True) for name in EXPECTED]

        result = classify_jobs(jobs, expected_names=EXPECTED)

        self.assertEqual(result["decision"], "billing_fallback")
        self.assertTrue(result["fallback"])

    def test_code_failure_never_falls_back(self):
        jobs = [_job(name) for name in EXPECTED]
        jobs[0] = _job("test", conclusion="failure")

        result = classify_jobs(jobs, expected_names=EXPECTED)

        self.assertEqual(result["decision"], "hard_failure")
        self.assertFalse(result["fallback"])


class WorkflowDefinitionTests(unittest.TestCase):
    def test_fallback_tracks_all_matrix_and_compile_jobs(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci-fallback.yml").read_text()

        for job_name in HOSTED_EXPECTED:
            self.assertIn(f'--expected "{job_name}"', workflow)

        self.assertIn("github.event.workflow_run.conclusion != 'cancelled'", workflow)
        self.assertIn("server-for-esp32cam-gate", workflow)
        self.assertIn("server-for-esp32cam-fallback", workflow)

    def test_hosted_workflow_does_not_run_on_feature_branch_pushes(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()

        self.assertNotIn('"feat/**"', workflow)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", workflow)
        self.assertIn("persist-credentials: false", workflow)

    def test_fallback_does_not_assign_gate_runner_to_untrusted_runs(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci-fallback.yml").read_text()

        self.assertIn("github.event.workflow_run.head_repository.full_name", workflow)
        self.assertIn("github.event.workflow_run.triggering_actor.login", workflow)
        self.assertIn("github.repository_owner", workflow)

    def test_pr_agent_issue_comments_require_an_owner_pr(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/pr-agent.yml").read_text()

        self.assertIn("types: [created]", workflow)
        self.assertIn("github.event.issue.pull_request", workflow)
        self.assertIn("github.event.issue.user.login", workflow)
        self.assertIn("github.event.sender.login == github.repository_owner", workflow)


if __name__ == "__main__":
    unittest.main()
