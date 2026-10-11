#!/usr/bin/env python3
"""Assess an isolated SQLite/upload backup without modifying either source."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


def assess(db_path: Path, uploads: Path, max_rows: int) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "failed", "db_integrity": None, "references_checked": 0,
        "missing_files": 0, "size_mismatches": 0, "invalid_paths": 0,
        "incomplete": False,
    }
    if (not db_path.is_file() or not uploads.is_dir() or db_path.is_symlink()
            or Path(f"{db_path}-wal").exists()):
        result["error"] = "backup_input_missing_or_unsafe"
        return result

    # The copied DB is private to this invocation. SQLite cannot create a WAL
    # beside or migrate the source; the source remains byte-for-byte immutable.
    with tempfile.TemporaryDirectory(prefix="esp32cam-restore-check-") as temporary:
        isolated_db = Path(temporary) / "restore.sqlite3"
        try:
            shutil.copyfile(db_path, isolated_db)
            conn = sqlite3.connect(f"{isolated_db.as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                integrity = conn.execute("PRAGMA quick_check").fetchone()
                result["db_integrity"] = integrity[0] if integrity else "unknown"
                if result["db_integrity"] != "ok":
                    return result

                upload_root = uploads.resolve()
                for table in ("snapshots", "videos"):
                    cursor = conn.execute(f"SELECT relative_path, size FROM {table} ORDER BY id LIMIT ?", (max_rows + 1,))
                    for relative_path, expected_size in cursor:
                        if result["references_checked"] >= max_rows:
                            result["incomplete"] = True
                            break
                        result["references_checked"] += 1
                        if (not isinstance(relative_path, str) or not relative_path
                                or Path(relative_path).is_absolute()
                                or ".." in Path(relative_path).parts):
                            result["invalid_paths"] += 1
                            continue
                        path = uploads / relative_path
                        try:
                            resolved = path.resolve()
                            resolved.relative_to(upload_root)
                            actual_size = resolved.stat().st_size
                            if not resolved.is_file():
                                result["missing_files"] += 1
                            elif actual_size != expected_size:
                                result["size_mismatches"] += 1
                        except (ValueError, RuntimeError):
                            result["invalid_paths"] += 1
                        except OSError:
                            result["missing_files"] += 1
                    if result["incomplete"]:
                        break
            finally:
                conn.close()
        except (OSError, sqlite3.DatabaseError):
            result["error"] = "backup_read_failed"
            return result

    if (result["db_integrity"] == "ok" and not result["incomplete"]
            and not result["missing_files"] and not result["size_mismatches"]
            and not result["invalid_paths"]):
        result["status"] = "passed"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="SQLite file from an isolated backup")
    parser.add_argument("--uploads", type=Path, required=True, help="Uploads directory from the same backup")
    parser.add_argument("--max-rows", type=int, default=100000)
    args = parser.parse_args(argv)
    if not 1 <= args.max_rows <= 1000000:
        parser.error("--max-rows must be between 1 and 1000000")
    result = assess(args.db, args.uploads, args.max_rows)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
