from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN_DOC = ROOT / "docs" / "design" / "video-upload.md"
ROADMAP_DOC = ROOT / "docs" / "ja" / "roadmap.md"


def test_video_upload_design_doc_exists_and_covers_issue_35_acceptance_criteria() -> None:
    text = DESIGN_DOC.read_text(encoding="utf-8")

    required_terms = [
        "POST /api/cameras/<camera_id>/videos",
        "uploads/<camera_id>/videos/",
        "GET /api/cameras/<camera_id>/videos",
        "GET /api/cameras/<camera_id>/videos/<video_id>/content",
        "ESP32CAM_MAX_VIDEO_CONTENT_LENGTH",
        "ESP32CAM_VIDEO_UPLOAD_RATE_LIMIT",
        "413 Payload Too Large",
        "415 Unsupported Media Type",
        "videos (",
        "実装分割計画",
    ]

    missing = [term for term in required_terms if term not in text]
    assert not missing, missing


def test_video_upload_design_is_fail_closed_and_avoids_path_based_serving() -> None:
    text = DESIGN_DOC.read_text(encoding="utf-8")

    required_security_terms = [
        "fail-closed",
        "該当 `camera_id` の key が設定されていない場合は `403 Forbidden`",
        "`X-API-Key` がない場合は `401 Unauthorized`",
        "^[A-Za-z0-9_-]{1,64}$",
        "ユーザー入力をそのまま `Path` に連結しない",
        "hmac.compare_digest",
        "サーバーログ・エラーレスポンス・テスト出力へ key を出さない",
        "TLS/HTTPS 終端を必須",
        "HTTP request からファイルパスを直接受け取らない",
        "配信 API は `video_id` ベースに統一",
        "`Content-Type` やファイル拡張子だけを信頼しない",
        "マジックバイト",
        "一時ファイル",
        "atomic rename",
    ]

    missing = [term for term in required_security_terms if term not in text]
    assert not missing, missing


def test_roadmap_links_video_upload_design_doc() -> None:
    text = ROADMAP_DOC.read_text(encoding="utf-8")

    assert "docs/design/video-upload.md" in text
    assert "#35" in text
