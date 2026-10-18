from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from script.app import create_app
from script.metadata import SQLiteMetadataStore
from script.retention import apply_device_log_retention


def rows(store):
    return store._get_connection().execute("SELECT id, message FROM device_logs ORDER BY id").fetchall()


def test_row_budget_is_global_and_keeps_latest_receipts_despite_clock_skew(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "db", device_log_max_rows=3)
    now = datetime.now(timezone.utc)
    for i in range(8):
        store.record_device_log(f"camera-{i % 2}", level="error", message=str(i),
                                logged_at=now - timedelta(seconds=i))
        assert len(rows(store)) <= 3
    assert [r["message"] for r in rows(store)] == ["5", "6", "7"]


def test_age_and_row_budgets_both_apply_at_insert(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "db", device_log_max_rows=2,
                                device_log_max_age_seconds=60)
    now = datetime.now(timezone.utc)
    for i, age in enumerate([180, 2, 1, 120, 0]):
        store.record_device_log("camera", level="info", message=str(i),
                                logged_at=now - timedelta(seconds=age))
    assert [r["message"] for r in rows(store)] == ["2", "4"]


def test_global_age_cutoff_uses_index_instead_of_scanning_all_cameras(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "db", device_log_max_age_seconds=60)
    plan = store._get_connection().execute(
        "EXPLAIN QUERY PLAN DELETE FROM device_logs WHERE logged_at < ?",
        (datetime.now(timezone.utc).isoformat(),),
    ).fetchall()
    assert any("SEARCH device_logs" in row[3] for row in plan)
    assert not any("SCAN device_logs" in row[3] for row in plan)


@pytest.mark.parametrize("setting", ["device_log_max_rows", "device_log_max_age_seconds"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "3", 2**63])
def test_invalid_policies_fail_before_creating_database(tmp_path, setting, value):
    path = tmp_path / "db"
    with pytest.raises(ValueError):
        SQLiteMetadataStore(path, **{setting: value})
    assert not path.exists()


def test_unrepresentable_age_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        SQLiteMetadataStore(tmp_path / "db", device_log_max_age_seconds=2**60)


def test_prune_failure_rolls_back_insert_and_camera_update(tmp_path, monkeypatch):
    store = SQLiteMetadataStore(tmp_path / "db", device_log_max_rows=1)
    store.record_device_log("existing", level="info", message="preserved")
    def fail(_conn):
        raise OSError("simulated failure")
    monkeypatch.setattr(store, "_prune_device_logs", fail)
    with pytest.raises(OSError):
        store.record_device_log("new", level="info", message="uncommitted")
    assert [r["message"] for r in rows(store)] == ["preserved"]
    assert store.get_camera("new") is None


def test_disabled_budget_preserves_legacy_history(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "db")
    for i in range(4):
        store.record_device_log("camera", level="warning", message=str(i),
                                logged_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert store.prune_device_logs() == 0
    assert len(rows(store)) == 4


def test_concurrent_insert_and_prune_never_commit_above_global_cap(tmp_path):
    store = SQLiteMetadataStore(tmp_path / "db", device_log_max_rows=3)
    def write(camera):
        for i in range(20):
            store.record_device_log(camera, level="error", message=str(i))
            assert len(rows(store)) <= 3
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(write, ["one", "two", "three"]))
    assert len(rows(store)) == 3


def test_scheduler_prunes_existing_history_without_a_new_upload(tmp_path):
    database = tmp_path / "db"
    legacy = SQLiteMetadataStore(database)
    for i in range(5):
        legacy.record_device_log("camera", level="info", message=str(i))
    app = create_app({"TESTING": True, "INFERENCE_MODE": "sync", "RATELIMIT_ENABLED": False,
                      "DB_PATH": database, "UPLOAD_DIR": tmp_path / "uploads",
                      "DEVICE_LOG_RETENTION_MAX_ROWS": 2})
    with app.app_context():
        assert apply_device_log_retention() == 3
        assert apply_device_log_retention() == 0
    assert [r["message"] for r in rows(legacy)] == ["3", "4"]


def test_api_keeps_response_and_enforces_budget_across_cameras(tmp_path):
    app = create_app({"TESTING": True, "INFERENCE_MODE": "sync", "RATELIMIT_ENABLED": False,
                      "DB_PATH": tmp_path / "db", "UPLOAD_DIR": tmp_path / "uploads",
                      "DEVICE_LOG_RETENTION_MAX_ROWS": 1})
    client = app.test_client()
    for camera in ("one", "two"):
        result = client.post("/api/log_error", json={"device_id": camera, "message": "猫" * 2000})
        assert result.status_code == 201
        assert result.get_json()["message"] == "猫" * 2000
    assert len(rows(app.extensions["metadata_store"])) == 1
