#!/usr/bin/env python3
"""Inspect a completed hosted workflow and manage its fallback check run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from classify_hosted_jobs import ClassificationError, classify_jobs


class AuthorizationError(ValueError):
    """Raised when a workflow run is not trusted for local execution."""


class GitHubResponseError(RuntimeError):
    """Raised when GitHub returns an incomplete or invalid response."""


_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_EVENTS = {"pull_request", "push"}
_ALLOWED_CONCLUSIONS = {"success", "failure"}


class GitHubClient:
    """Minimal GitHub API client with no credential persistence."""

    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        if not token:
            raise ValueError("GitHub token is required")
        self._token = token
        self._api_url = api_url.rstrip("/")

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._api_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "server-for-esp32cam-ci-fallback",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            raise GitHubResponseError(f"GitHub API request failed: {method} {path}: {exc}") from exc
        if not response_body:
            return {}
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise GitHubResponseError(
                f"GitHub API returned invalid JSON: {method} {path}"
            ) from exc


def authorize_workflow_run(
    event: dict[str, Any], *, trusted_actors: set[str]
) -> dict[str, Any]:
    """Validate immutable workflow_run metadata before touching PR code."""

    try:
        repository = str(event["repository"]["full_name"])
        run = event["workflow_run"]
        head_repository = str(run["head_repository"]["full_name"])
        actor = str(run["triggering_actor"]["login"])
        event_name = str(run["event"])
        head_sha = str(run["head_sha"])
        run_id = int(run["id"])
        run_attempt = int(run.get("run_attempt", 1))
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationError("workflow_run event is incomplete") from exc

    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise AuthorizationError("repository name is invalid")
    if head_repository != repository:
        raise AuthorizationError("fork workflow runs are not allowed")
    if actor not in trusted_actors:
        raise AuthorizationError(f"workflow actor is not trusted: {actor}")
    if event_name not in _ALLOWED_EVENTS:
        raise AuthorizationError(f"workflow event is not allowed: {event_name}")
    if not _SHA_PATTERN.fullmatch(head_sha):
        raise AuthorizationError("workflow head SHA is invalid")
    if run_id <= 0 or run_attempt <= 0:
        raise AuthorizationError("workflow run identity is invalid")

    return {
        "repository": repository,
        "head_sha": head_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "actor": actor,
        "event": event_name,
    }


def _require_dict(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubResponseError(f"GitHub response must be an object: {context}")
    return value


def _fetch_annotations(client: Any, repository: str, check_run_id: int) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    for page in range(1, 11):
        response = client.request(
            "GET",
            f"/repos/{repository}/check-runs/{check_run_id}/annotations"
            f"?per_page=100&page={page}",
        )
        if not isinstance(response, list):
            raise GitHubResponseError("check annotations response must be a list")
        annotations.extend(
            _require_dict(annotation, "check annotation") for annotation in response
        )
        if len(response) < 100:
            return annotations
    raise GitHubResponseError("check annotations exceeded the safe pagination limit")


def fetch_run_jobs(client: Any, repository: str, run_id: int) -> list[dict[str, Any]]:
    """Fetch one complete jobs page and enrich failed jobs with annotations."""

    response = _require_dict(
        client.request(
            "GET",
            f"/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100&page=1",
        ),
        "workflow jobs",
    )
    jobs_value = response.get("jobs")
    total_count = response.get("total_count")
    if not isinstance(jobs_value, list) or not isinstance(total_count, int):
        raise GitHubResponseError("workflow jobs response is incomplete")
    if total_count != len(jobs_value):
        raise GitHubResponseError(
            "workflow jobs exceeded one page or returned an incomplete page"
        )

    jobs = [_require_dict(job, "workflow job") for job in jobs_value]
    job_ids = [job.get("id") for job in jobs]
    if any(not isinstance(job_id, int) or job_id <= 0 for job_id in job_ids):
        raise GitHubResponseError("workflow job ID is invalid")
    if len(job_ids) != len(set(job_ids)):
        raise GitHubResponseError("workflow job IDs are duplicated")

    for job in jobs:
        job["annotations"] = []
        if job.get("conclusion") != "success":
            job["annotations"] = _fetch_annotations(client, repository, int(job["id"]))
    return jobs


def create_check_run(
    client: Any,
    *,
    repository: str,
    head_sha: str,
    source_run_id: int,
    source_attempt: int,
    check_name: str = "CI Fallback Gate",
) -> int:
    response = _require_dict(
        client.request(
            "POST",
            f"/repos/{repository}/check-runs",
            {
                "name": check_name,
                "head_sha": head_sha,
                "external_id": f"ci-fallback:{source_run_id}:{source_attempt}",
                "status": "in_progress",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "output": {
                    "title": "Inspecting GitHub-hosted CI",
                    "summary": f"Source workflow run: {source_run_id}",
                },
            },
        ),
        "create check run",
    )
    check_id = response.get("id")
    if not isinstance(check_id, int) or check_id <= 0:
        raise GitHubResponseError("create check run response has no valid ID")
    return check_id


def complete_check_run(
    client: Any,
    *,
    repository: str,
    check_id: int,
    conclusion: str,
    title: str,
    summary: str,
) -> None:
    if conclusion not in _ALLOWED_CONCLUSIONS:
        raise ValueError(f"unsupported check conclusion: {conclusion}")
    client.request(
        "PATCH",
        f"/repos/{repository}/check-runs/{check_id}",
        {
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "output": {"title": title[:255], "summary": summary[:65535]},
        },
    )


def inspect_run(
    *,
    event: dict[str, Any],
    trusted_actors: set[str],
    expected_names: Sequence[str],
    client: Any,
    check_name: str = "CI Fallback Gate",
) -> dict[str, Any]:
    authorized = authorize_workflow_run(event, trusted_actors=trusted_actors)
    jobs = fetch_run_jobs(client, authorized["repository"], authorized["run_id"])
    check_id = create_check_run(
        client,
        repository=authorized["repository"],
        head_sha=authorized["head_sha"],
        source_run_id=authorized["run_id"],
        source_attempt=authorized["run_attempt"],
        check_name=check_name,
    )
    try:
        classification = classify_jobs(jobs, expected_names=expected_names)
    except ClassificationError as exc:
        classification = {
            "decision": "hard_failure",
            "fallback": False,
            "reason": f"hosted job classification failed: {exc}",
            "jobs": {},
        }
    return {**classification, **authorized, "check_id": check_id}


def _write_github_output(path: Path, values: dict[str, Any]) -> None:
    allowed = (
        "decision",
        "fallback",
        "reason",
        "repository",
        "head_sha",
        "run_id",
        "run_attempt",
        "check_id",
    )
    lines = []
    for key in allowed:
        value = str(values[key]).replace("\r", " ").replace("\n", " ")
        lines.append(f"{key}={value}")
    with path.open("a", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")


def _client_from_environment(api_url: str | None) -> GitHubClient:
    token = os.environ.get("GITHUB_TOKEN", "")
    return GitHubClient(token, api_url or os.environ.get("GITHUB_API_URL", "https://api.github.com"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--event", type=Path, required=True)
    inspect_parser.add_argument("--trusted-actor", action="append", required=True)
    inspect_parser.add_argument("--expected", action="append", required=True)
    inspect_parser.add_argument("--github-output", type=Path, required=True)
    inspect_parser.add_argument("--check-name", default="CI Fallback Gate")
    inspect_parser.add_argument("--api-url")

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--repository", required=True)
    complete_parser.add_argument("--check-id", type=int, required=True)
    complete_parser.add_argument("--conclusion", choices=sorted(_ALLOWED_CONCLUSIONS), required=True)
    complete_parser.add_argument("--title", required=True)
    complete_parser.add_argument("--summary", required=True)
    complete_parser.add_argument("--api-url")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv or sys.argv[1:])
    try:
        client = _client_from_environment(args.api_url)
        if args.command == "inspect":
            event = json.loads(args.event.read_text(encoding="utf-8"))
            result = inspect_run(
                event=event,
                trusted_actors=set(args.trusted_actor),
                expected_names=args.expected,
                check_name=args.check_name,
                client=client,
            )
            _write_github_output(args.github_output, result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0

        complete_check_run(
            client,
            repository=args.repository,
            check_id=args.check_id,
            conclusion=args.conclusion,
            title=args.title,
            summary=args.summary,
        )
        return 0
    except (
        AuthorizationError,
        ClassificationError,
        GitHubResponseError,
        OSError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
    ) as exc:
        print(f"ci-fallback: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
