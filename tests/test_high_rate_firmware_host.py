"""Verify the experimental firmware's wire header with a host C++ compiler."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "esp32cam-http-upload"


def test_frame_header_and_freshness(tmp_path: Path) -> None:
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("host C++ compiler is unavailable")
    source = tmp_path / "high_rate_frame.cpp"
    source.write_text(
        r"""
#include <cassert>
#include <cstdint>
#include <cstring>
#include "high_rate_frame.h"

int main() {
  uint8_t header[high_rate::kHeaderSize] = {};
  assert(high_rate::encodeHeader(header, 0x0102030405060708ULL, 1,
                                 0x1122334455667788ULL, 0x1122334455667799ULL, 0x1fffe));
  const uint8_t expected[] = {
      'E','C','F','1', 1,2,3,4,5,6,7,8, 0,0,0,0,0,0,0,1,
      0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88,
      0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x99, 0,1,0xff,0xfe};
  assert(sizeof(expected) == high_rate::kHeaderSize);
  assert(std::memcmp(header, expected, sizeof(expected)) == 0);
  assert(high_rate::encodeHeader(header, 1, 2, 3, 4, high_rate::kMaxJpegBytes));
  assert(high_rate::encodeHeader(header, 1, 2, 3, 3, 1));
  assert(!high_rate::encodeHeader(header, 1, 2, 4, 3, 1));
  assert(!high_rate::encodeHeader(nullptr, 1, 2, 3, 4, 1));
  assert(!high_rate::encodeHeader(header, 0, 2, 3, 4, 1));
  assert(!high_rate::encodeHeader(header, 1, 0, 3, 4, 1));
  assert(!high_rate::encodeHeader(header, 1, 2, 3, 4, 0));
  assert(!high_rate::encodeHeader(header, 1, 2, 3, 4, high_rate::kMaxJpegBytes + 1));
  assert(high_rate::isFresh(1, 0));
  assert(high_rate::isFresh(101, 100));
  assert(!high_rate::isFresh(100, 100));
  assert(!high_rate::isFresh(99, 100));
  assert(!high_rate::isFresh(0, 0));
}
""",
        encoding="utf-8",
    )
    executable = tmp_path / "high_rate_frame"
    subprocess.run(
        [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-I", str(FIRMWARE / "include"),
         str(source), "-o", str(executable)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)


def test_experimental_target_is_isolated() -> None:
    ini = (FIRMWARE / "platformio.ini").read_text(encoding="utf-8")
    section = ini.split("[env:esp32cam_high_rate_capture]", 1)[1].split("[env:", 1)[0]
    assert "+<high_rate.cpp>" in section
    assert "-<main.cpp>" in section
    assert "[env:esp32cam_high_rate_capture]" not in ini.split("default_envs =", 1)[1].split("\n", 1)[0]


def test_authenticated_ota_stays_available_after_camera_failure() -> None:
    source = (FIRMWARE / "src" / "high_rate.cpp").read_text(encoding="utf-8")
    setup = source.split("void setup() {", 1)[1].split("void loop() {", 1)[0]
    loop = source.split("void loop() {", 1)[1]
    assert setup.index("WiFi.begin(WIFI_SSID, WIFI_PASSWORD)") < setup.index("if (!psramFound())")
    assert setup.index("ready = true") < setup.index("if (!psramFound())")
    assert "ArduinoOTA.setPassword(OTA_PASSWORD)" in loop
    assert "ArduinoOTA.begin()" in loop
    assert loop.index("ArduinoOTA.handle()") < loop.index("webSocket.loop()")
