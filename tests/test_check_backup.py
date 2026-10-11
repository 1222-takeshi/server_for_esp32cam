import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from scripts import check_backup


def backup_fixture(tmp_path):
    db = tmp_path / "backup.sqlite3"
    uploads = tmp_path / "uploads"
    image = uploads / "cam" / "images" / "one.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"jpeg")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY, relative_path TEXT, size INTEGER)")
        conn.execute("CREATE TABLE videos (id INTEGER PRIMARY KEY, relative_path TEXT, size INTEGER)")
        conn.execute("INSERT INTO snapshots VALUES (1, 'cam/images/one.jpg', 4)")
    return db, uploads, image


def test_assess_valid_backup_preserves_sources(tmp_path):
    db, uploads, image = backup_fixture(tmp_path)
    original_db = db.read_bytes()
    result = check_backup.assess(db, uploads, 100)
    assert result["status"] == "passed"
    assert result["references_checked"] == 1
    assert db.read_bytes() == original_db
    assert image.read_bytes() == b"jpeg"


def test_assess_missing_and_wrong_sized_images(tmp_path):
    db, uploads, image = backup_fixture(tmp_path)
    image.write_bytes(b"shorter")
    assert check_backup.assess(db, uploads, 100)["size_mismatches"] == 1
    image.unlink()
    result = check_backup.assess(db, uploads, 100)
    assert result["status"] == "failed"
    assert result["missing_files"] == 1


def test_assess_rejects_path_escape_and_reports_incomplete(tmp_path):
    db, uploads, _ = backup_fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO videos VALUES (1, '../outside.mp4', 1)")
    assert check_backup.assess(db, uploads, 100)["invalid_paths"] == 1
    truncated = check_backup.assess(db, uploads, 1)
    assert truncated["status"] == "failed"
    assert truncated["incomplete"] is True


def test_assess_bad_database_and_wal_sidecar(tmp_path):
    db, uploads, _ = backup_fixture(tmp_path)
    db.write_bytes(b"not sqlite")
    assert check_backup.assess(db, uploads, 100)["status"] == "failed"
    Path(f"{db}-wal").write_bytes(b"wal")
    assert check_backup.assess(db, uploads, 100)["error"] == "backup_input_missing_or_unsafe"


@pytest.mark.parametrize("max_rows", [0, 1000001])
def test_invalid_row_limit_is_rejected(tmp_path, max_rows):
    with pytest.raises(SystemExit) as exc:
        check_backup.main(["--db", str(tmp_path / "db"), "--uploads", str(tmp_path),
                           "--max-rows", str(max_rows)])
    assert exc.value.code == 2


def test_hot_backup_includes_wal_and_publishes_checked_file(tmp_path):
    source = tmp_path / "live.sqlite3"
    backup_dir = tmp_path / "backups"
    conn = sqlite3.connect(source)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE example (value TEXT)")
    conn.execute("INSERT INTO example VALUES ('in-wal')")
    conn.commit()
    completed = subprocess.run(
        ["bash", "scripts/backup.sh"],
        env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "DB_PATH": str(source),
             "BACKUP_DIR": str(backup_dir)}, capture_output=True, text=True, check=False,
    )
    conn.close()
    assert completed.returncode == 0, completed.stderr
    backups = list(backup_dir.glob("esp32cam_*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as restored:
        assert restored.execute("SELECT value FROM example").fetchone()[0] == "in-wal"
    assert not list(backup_dir.glob(".esp32cam_*"))


def test_failed_backup_preserves_existing_daily_copy(tmp_path):
    source = tmp_path / "invalid.sqlite3"
    source.write_bytes(b"not sqlite")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    # The daily filename is intentionally derived from the same date command as the script.
    existing = backup_dir / f"esp32cam_{datetime.now().strftime('%Y%m%d')}.sqlite3"
    existing.write_bytes(b"known-good-placeholder")
    completed = subprocess.run(
        ["bash", "scripts/backup.sh"],
        env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "DB_PATH": str(source),
             "BACKUP_DIR": str(backup_dir)}, capture_output=True, text=True, check=False,
    )
    assert completed.returncode != 0
    assert existing.read_bytes() == b"known-good-placeholder"
    assert not list(backup_dir.glob(".esp32cam_*"))
