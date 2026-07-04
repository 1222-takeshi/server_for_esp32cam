from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "esp32cam_bridge_proxy.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("esp32cam_bridge_proxy", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_exists() -> None:
    assert SCRIPT_PATH.exists()


def test_rewrites_api_key_header_preserving_rest() -> None:
    module = _load_module()
    request = (
        b"GET /ws/cameras/esp32cam-01/upload HTTP/1.1\r\n"
        b"Host: 192.168.1.233:8080\r\n"
        b"Upgrade: websocket\r\n"
        b"X-API-Key: stale-old-key-32chars-aaaaaaaaaa\r\n"
        b"User-Agent: arduino-WebSocket-Client\r\n"
        b"\r\n"
    )
    rewritten, count = module.rewrite_api_key_header(request, b"new-key-64chars")

    assert count == 1
    assert b"X-API-Key: new-key-64chars\r\n" in rewritten
    assert b"stale-old-key" not in rewritten
    assert rewritten.startswith(b"GET /ws/cameras/esp32cam-01/upload HTTP/1.1\r\n")
    assert rewritten.endswith(b"User-Agent: arduino-WebSocket-Client\r\n\r\n")


def test_rewrite_is_case_insensitive() -> None:
    module = _load_module()
    request = b"GET / HTTP/1.1\r\nx-api-key:old\r\n\r\n"
    rewritten, count = module.rewrite_api_key_header(request, b"new")

    assert count == 1
    assert b"x-api-key:new\r\n" in rewritten


def test_no_header_returns_unchanged() -> None:
    module = _load_module()
    request = b"GET /healthz HTTP/1.1\r\nHost: example\r\n\r\n"
    rewritten, count = module.rewrite_api_key_header(request, b"new")

    assert count == 0
    assert rewritten == request


def test_binary_payload_after_headers_untouched() -> None:
    module = _load_module()
    body = bytes(range(256))
    request = b"POST /upload HTTP/1.1\r\nX-API-Key: old\r\n\r\n" + body
    rewritten, count = module.rewrite_api_key_header(request, b"new")

    assert count == 1
    assert rewritten.endswith(body)
