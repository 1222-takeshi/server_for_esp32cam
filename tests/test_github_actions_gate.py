"""Security tests for the workflow_run fallback gate."""

import importlib.util
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "ci" / "github_actions_gate.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("github_actions_gate", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {path}")
        return self.responses.pop(0)


def workflow_run_event(*, repository="1222-takeshi/server_for_esp32cam", actor="1222-takeshi", event="pull_request"):
    return {
        "repository": {"full_name": repository},
        "workflow_run": {
            "id": 1234,
            "run_attempt": 1,
            "event": event,
            "head_sha": "a" * 40,
            "head_repository": {"full_name": repository},
            "triggering_actor": {"login": actor},
        },
    }


def test_authorize_same_repository_trusted_actor():
    result = GATE.authorize_workflow_run(
        workflow_run_event(), trusted_actors={"1222-takeshi"}
    )

    assert result["repository"] == "1222-takeshi/server_for_esp32cam"
    assert result["head_sha"] == "a" * 40


@pytest.mark.parametrize(
    "event, trusted",
    [
        (workflow_run_event(actor="someone-else"), {"1222-takeshi"}),
        (
            {
                **workflow_run_event(),
                "workflow_run": {
                    **workflow_run_event()["workflow_run"],
                    "head_repository": {"full_name": "fork/server_for_esp32cam"},
                },
            },
            {"1222-takeshi"},
        ),
    ],
)
def test_authorize_rejects_untrusted_actor_or_fork(event, trusted):
    with pytest.raises(GATE.AuthorizationError):
        GATE.authorize_workflow_run(event, trusted_actors=trusted)


def test_authorize_rejects_invalid_sha_and_event_type():
    event = workflow_run_event(event="schedule")
    event["workflow_run"]["head_sha"] = "main"

    with pytest.raises(GATE.AuthorizationError):
        GATE.authorize_workflow_run(event, trusted_actors={"1222-takeshi"})


def test_fetch_jobs_enriches_failure_annotations():
    client = FakeClient(
        [
            {
                "total_count": 2,
                "jobs": [
                    {"id": 1, "name": "test (3.11)", "conclusion": "success"},
                    {"id": 2, "name": "dependency-audit", "conclusion": "failure"},
                ],
            },
            [{"annotation_level": "failure", "message": "billing"}],
        ]
    )

    jobs = GATE.fetch_run_jobs(
        client, "1222-takeshi/server_for_esp32cam", 1234
    )

    assert jobs[1]["annotations"] == [
        {"annotation_level": "failure", "message": "billing"}
    ]
    assert client.requests[1][0] == "GET"


def test_fetch_jobs_rejects_incomplete_pagination():
    client = FakeClient([{"total_count": 2, "jobs": [{"id": 1, "name": "test (3.11)"}]}])

    with pytest.raises(GATE.GitHubResponseError):
        GATE.fetch_run_jobs(client, "1222-takeshi/server_for_esp32cam", 1234)


def test_create_check_run_uses_target_sha_and_stable_external_id():
    client = FakeClient([{"id": 999}])

    check_id = GATE.create_check_run(
        client,
        repository="1222-takeshi/server_for_esp32cam",
        head_sha="a" * 40,
        source_run_id=1234,
        source_attempt=2,
    )

    assert check_id == 999
    payload = client.requests[0][2]
    assert payload["head_sha"] == "a" * 40
    assert payload["external_id"] == "ci-fallback:1234:2"


def test_complete_check_run_restricts_conclusion():
    client = FakeClient([{}])

    GATE.complete_check_run(
        client,
        repository="1222-takeshi/server_for_esp32cam",
        check_id=999,
        conclusion="success",
        title="Local fallback CI passed",
        summary="ok",
    )

    assert client.requests[0][2]["conclusion"] == "success"
    with pytest.raises(ValueError):
        GATE.complete_check_run(
            client,
            repository="1222-takeshi/server_for_esp32cam",
            check_id=999,
            conclusion="neutral",
            title="invalid",
            summary="invalid",
        )
