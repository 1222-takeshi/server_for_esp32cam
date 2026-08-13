"""Supply-chain and isolation checks for the self-hosted runner setup."""

from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
RUNNER_ROOT = REPO_ROOT / "infra" / "github-actions-runner"
MANAGER = REPO_ROOT / "scripts" / "github-runner.sh"


def test_runner_download_is_versioned_and_checksum_verified():
    content = (RUNNER_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG RUNNER_VERSION=" in content
    assert "ARG RUNNER_SHA256=" in content
    assert "sha256sum --check" in content
    assert "actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" in content


def test_runner_process_is_non_root_and_has_no_docker_client():
    dockerfile = (RUNNER_ROOT / "Dockerfile").read_text(encoding="utf-8")
    manager = MANAGER.read_text(encoding="utf-8")

    assert "USER runner" in dockerfile
    assert "docker-ce" not in dockerfile
    assert "docker.sock" not in manager


def test_entrypoint_accepts_only_project_labels():
    entrypoint = (RUNNER_ROOT / "entrypoint.sh").read_text(encoding="utf-8")

    assert "esp32cam-ci-gate|esp32cam-ci-fallback" in entrypoint
    assert "--disableupdate" in entrypoint
    assert "RUNNER_REGISTRATION_TOKEN" not in entrypoint


def test_manager_uses_short_lived_registration_token_without_persisting_it():
    manager = MANAGER.read_text(encoding="utf-8")

    assert "registration-token" in manager
    assert "--tmpfs /run/runner-secrets" in manager
    assert "--restart=unless-stopped" in manager
    assert "--cap-drop=ALL" in manager
    assert "--security-opt=no-new-privileges" in manager
    assert 'RUNNER_LABELS=${label}' in manager
    assert "unset registration_token" in manager


def test_manager_keeps_gate_and_candidate_labels_separate():
    manager = MANAGER.read_text(encoding="utf-8")

    assert "runner_label" in manager
    assert "esp32cam-ci-%s" in manager
    assert "docker run" in manager
