from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_ROOT = REPO_ROOT / "firmware" / "esp32cam-http-upload"
MAIN_CPP = FIRMWARE_ROOT / "src" / "main.cpp"
CONFIG_EXAMPLE = FIRMWARE_ROOT / "include" / "esp32cam_config.h.example"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_config_example_documents_flash_settings() -> None:
    text = read(CONFIG_EXAMPLE)

    assert "#define FLASH_LED_ENABLED" in text
    assert "#define FLASH_LED_BRIGHTNESS_PCT" in text
    assert "#define FLASH_NIGHT_START_HOUR" in text
    assert "#define FLASH_NIGHT_END_HOUR" in text


def test_main_has_flash_defaults_and_night_window() -> None:
    text = read(MAIN_CPP)

    assert "#ifndef FLASH_LED_ENABLED" in text
    assert "#ifndef FLASH_LED_PIN" in text
    assert "isFlashWindowNow" in text
    # Window must wrap midnight (e.g. 18:00-06:00): both comparisons present.
    assert "hour >= FLASH_NIGHT_START_HOUR || hour < FLASH_NIGHT_END_HOUR" in text


def test_flash_capture_discards_pre_flash_frame() -> None:
    """fb_count=1 + GRAB_WHEN_EMPTY queues a frame captured before the LED
    went on; the firmware must discard one frame after enabling the flash."""
    text = read(MAIN_CPP)

    assert "discard" in text.lower()
    idx_flash_on = text.find("ledcWrite(kFlashLedcChannel")
    assert idx_flash_on != -1
    # A stale-frame return must happen between flash-on and the real capture.
    segment = text[idx_flash_on:]
    assert segment.find("esp_camera_fb_return") < segment.find(
        "camera_fb_t *fb = esp_camera_fb_get()"
    )


def test_flash_turns_off_before_error_paths() -> None:
    text = read(MAIN_CPP)
    # LED off must directly follow the real capture inside the WebSocket
    # upload function so no early return leaves the LED lit.
    fn_idx = text.find("void captureAndUploadWebSocketFrame()")
    assert fn_idx != -1
    capture_idx = text.find("camera_fb_t *fb = esp_camera_fb_get();", fn_idx)
    assert capture_idx != -1
    after = text[capture_idx : capture_idx + 400]
    assert "ledcWrite(kFlashLedcChannel, 0)" in after
