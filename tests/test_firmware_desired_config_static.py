from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_ROOT = REPO_ROOT / "firmware" / "esp32cam-http-upload"
PLATFORMIO_INI = FIRMWARE_ROOT / "platformio.ini"
MAIN_CPP = FIRMWARE_ROOT / "src" / "main.cpp"
CONFIG_EXAMPLE = FIRMWARE_ROOT / "include" / "esp32cam_config.h.example"
README = FIRMWARE_ROOT / "README.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_firmware_declares_desired_config_runtime_settings() -> None:
    text = read(CONFIG_EXAMPLE)

    assert '#define DEVICE_API_BASE_URL "http://YOUR_MAC_LAN_IP:8080"' in text
    assert '#define DEVICE_API_KEY ""' in text
    assert "DESIRED_CONFIG_POLL_TIMEOUT_MS" in text
    assert "DESIRED_CONFIG_FIRMWARE_VERSION" in text


def test_http_upload_env_builds_with_json_parser_for_desired_config() -> None:
    text = read(PLATFORMIO_INI)

    assert "bblanchon/ArduinoJson" in text
    assert "-D HTTP_UPLOAD_SMOKE_TEST" in text


def test_firmware_polls_applies_and_acks_desired_config() -> None:
    text = read(MAIN_CPP)

    assert "pollDesiredConfig()" in text
    assert "applyDesiredConfig" in text
    assert "sendDesiredConfigAck" in text
    assert '"X-API-Key", DEVICE_API_KEY' in text
    assert '"/api/devices/"' in text
    assert '"/desired-config/ack"' in text
    assert "capture_interval_seconds" in text
    assert "frame_size" in text
    assert "jpeg_quality" in text
    assert "vflip" in text
    assert "hmirror" in text
    assert "restart_requested" in text
    assert "ota_update_requested" in text
    assert "ota_window_seconds" in text


def test_desired_config_affects_camera_and_ota_flow_documented() -> None:
    source = read(MAIN_CPP)
    docs = read(README)

    assert "desiredConfig.frameSize" in source
    assert "desiredConfig.jpegQuality" in source
    assert "sensor->set_vflip" in source
    assert "sensor->set_hmirror" in source
    assert "runRequestedOtaWindow" in source
    assert "ESP.restart();" in source
    assert "desired-config" in docs
    assert "DEVICE_API_BASE_URL" in docs
    assert "DEVICE_API_KEY" in docs
