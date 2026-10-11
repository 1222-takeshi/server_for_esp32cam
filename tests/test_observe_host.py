import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from scripts import observe_host


def test_observe_records_readiness_camera_and_host_without_extra_fields(monkeypatch, tmp_path):
    def fetch(url):
        if url.endswith("/healthz/ready"):
            return {"status": "ready", "db": "ok", "storage": "ok", "secret": "do-not-log"}
        return {"status": "healthy", "seconds_since_contact": 4, "secret": "do-not-log"}

    monkeypatch.setattr(observe_host, "fetch_json", fetch)
    sample = observe_host.observe("http://localhost:8080", ["camera-a"], tmp_path)

    assert sample["readiness"]["status"] == "ready"
    assert sample["cameras"]["camera-a"]["seconds_since_contact"] == 4
    assert sample["host"]["disk_free_mb"] >= 0
    assert "do-not-log" not in json.dumps(sample)


def test_degraded_readiness_preserved_and_camera_failure_separate(monkeypatch, tmp_path):
    def fetch(url):
        if url.endswith("/healthz/ready"):
            return {"status": "degraded", "db": "error", "storage": "ok"}
        return {"error": "unreachable"}

    monkeypatch.setattr(observe_host, "fetch_json", fetch)
    sample = observe_host.observe("http://localhost:8080", ["camera-a"], tmp_path)
    assert sample["readiness"]["status"] == "degraded"
    assert sample["readiness"]["db"] == "error"
    assert sample["cameras"]["camera-a"]["error"] == "unreachable"


def test_fetch_preserves_503_degraded_body(monkeypatch):
    def urlopen(url, timeout):
        raise HTTPError(url, 503, "degraded", {}, BytesIO(b'{"status":"degraded","db":"error"}'))

    monkeypatch.setattr(observe_host, "urlopen", urlopen)
    assert observe_host.fetch_json("http://localhost:8080/healthz/ready") == {
        "status": "degraded", "db": "error"
    }


def test_append_rotates_and_keeps_one_prior_file(tmp_path):
    path = tmp_path / "observations.jsonl"
    sample = {"status": "ok", "padding": "x" * 100}
    size = len((json.dumps(sample, separators=(",", ":"), sort_keys=True) + "\n").encode())
    for _ in range(4):
        observe_host.append_bounded(path, sample, size * 2)
    prior = path.with_suffix(".jsonl.1")
    assert path.stat().st_size == size * 2
    assert prior.stat().st_size == size * 2
    assert len(path.read_text().splitlines()) == 2
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("argv", [
    ["--output", "/tmp/unused"],
    ["--output", "/tmp/unused", "--camera-id", "bad/name"],
    ["--output", "/tmp/unused", "--camera-id", "a", "--base-url", "http://token@localhost:8080"],
    ["--output", "/tmp/unused", "--camera-id", "a", "--base-url", "http://localhost:bad"],
    ["--output", "/tmp/unused", "--camera-id", "a", "--max-bytes", "10"],
])
def test_invalid_arguments_fail_before_probing(argv, monkeypatch):
    monkeypatch.setattr(observe_host, "observe", lambda *args: pytest.fail("unexpected probe"))
    with pytest.raises(SystemExit) as exc:
        observe_host.main(argv)
    assert exc.value.code == 2
