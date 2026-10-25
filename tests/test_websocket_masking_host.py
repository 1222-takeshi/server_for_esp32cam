"""Compile actual pinned/patched library functions; no device or secrets required."""

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware/esp32cam-http-upload"


def test_actual_websockets_wire(tmp_path):
    source_path = os.environ.get("ESP32CAM_WEBSOCKETS_SOURCE")
    if not source_path:
        pytest.skip("run in firmware CI after PlatformIO installed the pinned library")
    spec = importlib.util.spec_from_file_location("ws_patch", FIRMWARE / "scripts/patch_websockets.py")
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)
    raw = Path(source_path).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == patch.PATCHED
    copy = tmp_path / "WebSockets.cpp"
    copy.write_bytes(raw)
    patch.patch_library(copy)
    assert copy.read_bytes() == raw  # Idempotent; an unknown version fails closed.
    copy.write_bytes(raw + b"\n")
    with pytest.raises(ValueError):
        patch.patch_library(copy)
    text = raw.decode()
    functions = []
    for name in ["uint8_t WebSockets::createHeader(", "bool WebSockets::sendFrameHeader(",
                 "bool WebSockets::sendFrame(", "size_t WebSockets::write(WSclient_t * client, uint8_t *"]:
        begin = text.index(name)
        functions.append(text[begin:text.index("\n}\n", begin) + 3])
    harness = (ROOT / "tests/firmware/websocket_masking.cpp").read_text()
    cpp = tmp_path / "wire.cpp"
    cpp.write_text(harness.replace("// ACTUAL_FUNCTIONS", "\n".join(functions)))
    compiler = shutil.which("c++")
    assert compiler, "firmware CI requires a host C++ compiler"
    executable = tmp_path / "wire"
    subprocess.run([compiler, "-std=c++14", "-Wall", "-Wextra", "-Werror",
                    "-fsanitize=address,undefined", str(cpp), "-o", str(executable)],
                   check=True, capture_output=True, text=True, timeout=60)
    subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=30)
