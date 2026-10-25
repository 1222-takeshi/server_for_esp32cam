"""Exercise the actual capture/send function with a host C++ camera/socket seam."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "esp32cam-http-upload"


@pytest.mark.parametrize("profiles", [0, 1])
def test_capture_packet_headroom_and_payload(tmp_path, profiles):
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("host C++ compiler is unavailable")
    firmware = (FIRMWARE / "src/high_rate.cpp").read_text()
    function = "void captureAndSend() {" + firmware.split("void captureAndSend() {", 1)[1].split(
        "}  // namespace", 1
    )[0]
    source = tmp_path / "packet.cpp"
    source.write_text(
        r"""
#include <algorithm>
#include <cassert>
#include <cstring>
#include <vector>
#include "operating_profile.h"
constexpr size_t WEBSOCKETS_MAX_HEADER_SIZE = 14;
constexpr bool profilesEnabled = PROFILES;
constexpr size_t headerSize = profilesEnabled ? operating::kHeaderSize : high_rate::kHeaderSize;
constexpr size_t capacity = WEBSOCKETS_MAX_HEADER_SIZE + headerSize + high_rate::kMaxJpegBytes;
constexpr int PIXFORMAT_JPEG = 1;
struct Frame { struct { long tv_sec; long tv_usec; } timestamp; uint8_t *buf; size_t len; int format; };
using camera_fb_t = Frame;
Frame frame;
std::vector<uint8_t> storage, jpeg, expected;
uint8_t *packet;
uint64_t bootId, frameSeq, lastAcquiredUs, nowUs;
uint32_t attempts, captured, captureFailures, stale, sent, sendFailures, returned;
bool available;
operating::Profile profile;
Frame *esp_camera_fb_get() { return available ? &frame : nullptr; }
void esp_camera_fb_return(Frame *fb) {
  assert(fb == &frame); ++returned;
  std::fill(jpeg.begin(), jpeg.end(), 0); // Frame storage is no longer usable.
}
uint64_t esp_timer_get_time() { return nowUs; }
struct Socket {
  size_t calls = 0;
  bool success = true;
  std::vector<uint8_t> payload;
  bool sendBIN(uint8_t *data, size_t length, bool headroom = false) {
    ++calls;
    assert(headroom == (length >= 1400));
    assert(data == (headroom ? packet : packet + WEBSOCKETS_MAX_HEADER_SIZE));
    // ArduinoWebSockets' headerToPayload contract: payload begins at +14,
    // and its 6/8/14-byte client WS header is written immediately before it.
    if (headroom) {
      const size_t ws = length < 65535 ? 8 : 14;
      std::fill(data + WEBSOCKETS_MAX_HEADER_SIZE - ws, data + WEBSOCKETS_MAX_HEADER_SIZE, 0);
      data += WEBSOCKETS_MAX_HEADER_SIZE;
    }
    payload.assign(data, data + length);
    return success;
  }
} webSocket;
void reset(size_t length) {
  storage.assign(16 + capacity + 16, 0xa5); packet = storage.data() + 16;
  jpeg.resize(length);
  for (size_t i = 0; i < length; ++i) jpeg[i] = static_cast<uint8_t>(i * 31);
  expected = jpeg;
  frame = {{0, 10}, jpeg.data(), length, PIXFORMAT_JPEG};
  bootId = 7; frameSeq = lastAcquiredUs = 0; nowUs = 20;
  attempts = captured = captureFailures = stale = sent = sendFailures = returned = 0;
  available = true; webSocket = Socket(); profile = operating::Profile();
  assert(profile.apply(3, true, 1));
}
void guards() {
  assert(std::all_of(storage.begin(), storage.begin() + 16, [](uint8_t v) { return v == 0xa5; }));
  assert(std::all_of(storage.end() - 16, storage.end(), [](uint8_t v) { return v == 0xa5; }));
}
"""
        + function
        + r"""
int main() {
  // Both ECF formats, all client WS header-size boundaries, and maximum JPEG.
  for (size_t length : {size_t(1), 125 - headerSize, 126 - headerSize, 1399 - headerSize, 1400 - headerSize,
                        65534 - headerSize, 65535 - headerSize, high_rate::kMaxJpegBytes}) {
    reset(length); captureAndSend(); guards();
    assert(attempts == 1 && captured == 1 && returned == 1 && sent == 1);
    assert(sendFailures == 0 && captureFailures == 0 && webSocket.calls == 1);
    std::vector<uint8_t> oldPayload(headerSize + length);
    const bool encoded = profilesEnabled ?
      operating::encodeHeader(oldPayload.data(), 7, 1, 10, 20, length, 3) :
      high_rate::encodeHeader(oldPayload.data(), 7, 1, 10, 20, length);
    assert(encoded);
    std::copy(expected.begin(), expected.end(), oldPayload.begin() + headerSize);
    assert(webSocket.payload == oldPayload); // Unmasked application bytes, not WS mask bytes.
  }
  for (size_t length : {size_t(0), high_rate::kMaxJpegBytes + 1}) {
    reset(length); captureAndSend(); guards();
    assert(captureFailures == 1 && returned == 1 && webSocket.calls == 0);
  }
  reset(100); frame.format = 0; captureAndSend(); guards();
  assert(captureFailures == 1 && returned == 1 && webSocket.calls == 0);
  reset(100); available = false; captureAndSend(); guards();
  assert(captureFailures == 1 && returned == 0 && webSocket.calls == 0);
  reset(100); lastAcquiredUs = 10; captureAndSend(); guards();
  assert(stale == 1 && returned == 1 && webSocket.calls == 0);
  reset(100); bootId = 0; captureAndSend(); guards();
  assert(sendFailures == 1 && returned == 1 && webSocket.calls == 0);
  reset(100); nowUs = 9; captureAndSend(); guards();
  assert(sendFailures == 1 && returned == 1 && webSocket.calls == 0);
  reset(100); webSocket.success = false; captureAndSend(); guards();
  assert(sendFailures == 1 && sent == 0 && returned == 1 && webSocket.calls == 1);
}
"""
    )
    executable = tmp_path / "packet"
    subprocess.run(
        [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", f"-DPROFILES={profiles}",
         "-I", str(FIRMWARE / "include"), str(source), "-o", str(executable)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)


def test_packet_allocation_reserves_library_headroom():
    source = (FIRMWARE / "src/high_rate.cpp").read_text()
    assert "ps_malloc(WEBSOCKETS_MAX_HEADER_SIZE + headerSize + high_rate::kMaxJpegBytes)" in source
    ini = (FIRMWARE / "platformio.ini").read_text()
    section = ini.split("[env:esp32cam_high_rate_capture]", 1)[1].split("[env:", 1)[0]
    assert "platform = espressif32@6.5.0" in section
    assert "links2004/WebSockets@2.7.3" in section
