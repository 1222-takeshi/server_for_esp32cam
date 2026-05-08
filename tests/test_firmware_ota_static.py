from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_ROOT = REPO_ROOT / "firmware" / "esp32cam-http-upload"
PLATFORMIO_INI = FIRMWARE_ROOT / "platformio.ini"
MAIN_CPP = FIRMWARE_ROOT / "src" / "main.cpp"
CONFIG_EXAMPLE = FIRMWARE_ROOT / "include" / "esp32cam_config.h.example"
README = FIRMWARE_ROOT / "README.md"
RUNNING_DOC = REPO_ROOT / "docs" / "ja" / "running.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_platformio_has_ota_upload_environment() -> None:
    text = read(PLATFORMIO_INI)

    assert "[env:esp32cam_http_upload_ota]" in text
    assert "upload_protocol = espota" in text
    assert "upload_port = ${sysenv.ESP32CAM_OTA_HOST}" in text
    assert "--port=${sysenv.ESP32CAM_OTA_PORT}" in text
    assert "--auth=${sysenv.ESP32CAM_OTA_PASSWORD}" in text
    assert "board_build.partitions = min_spiffs.csv" in text
    assert "-D HTTP_UPLOAD_SMOKE_TEST" in text
    assert "-D ENABLE_ARDUINO_OTA=1" in text


def test_firmware_config_example_documents_ota_without_secrets() -> None:
    text = read(CONFIG_EXAMPLE)

    assert "#define OTA_HOSTNAME \"esp32cam-01\"" in text
    assert "#define OTA_PORT 3232" in text
    assert "#define OTA_PASSWORD \"\"" in text
    assert "#define OTA_WAIT_WINDOW_MS 15000" in text
    assert "YOUR_OTA_PASSWORD" not in text
    assert "[REDACTED]" not in text


def test_http_upload_firmware_initializes_and_services_arduino_ota() -> None:
    text = read(MAIN_CPP)

    assert "#include <ArduinoOTA.h>" in text
    assert "void setupArduinoOta()" in text
    assert "ArduinoOTA.setHostname(OTA_HOSTNAME);" in text
    assert "ArduinoOTA.setPort(OTA_PORT);" in text
    assert "ArduinoOTA.setPassword(OTA_PASSWORD);" in text
    assert "ArduinoOTA.begin();" in text
    assert "ArduinoOTA.handle();" in text
    assert "OTA ready" in text
    assert "OTA update started" in text
    assert "OTA update finished" in text


def test_docs_describe_usb_first_then_platformio_ota_and_post_upload_regression() -> None:
    combined = read(README) + "\n" + read(RUNNING_DOC)

    assert "esp32cam_http_upload_ota" in combined
    assert "ESP32CAM_OTA_HOST" in combined
    assert "ESP32CAM_OTA_PASSWORD" in combined
    assert "upload_protocol = espota" in combined
    assert "POST /upload" in combined
    assert "HTTP upload smoke test" in combined
    assert "USB" in combined
