"""Cross-language wire compatibility and capture-time mode generation."""

import shutil
import subprocess
from pathlib import Path

import pytest

from script.frame_protocol import HEADER, MAX_GENERATION, PROFILE_HEADER, decode_frame

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "esp32cam-http-upload"


@pytest.mark.parametrize("interval", [None, 190, 100])
def test_cpp_profile_state_and_python_wire(tmp_path, interval):
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("host C++ compiler is unavailable")
    source = tmp_path / "profile.cpp"
    source.write_text(r'''
#include <cassert>
#include <cstdio>
#include <cstring>
#include "operating_profile.h"
int main() {
  operating::Profile p;
  assert(!p.tracking && p.intervalMs() == 5000 && !p.accepts(1));
  assert(!p.apply(0, true, 1));
  assert(!p.apply(operating::kMaxGeneration + 1, true, 1));
  assert(!p.apply(1, true, 0));
  assert(p.apply(2, true, 100));
  assert(p.intervalMs() == EXPECTED_INTERVAL && !p.accepts(99) && !p.accepts(100));
  assert(p.accepts(101));
  assert(p.apply(2, true, 999));
  assert(p.appliedUs == 100); // Retry ACK must not re-label the capture boundary.
  assert(!p.apply(2, false, 1000));
  assert(!p.apply(1, false, 1000));
  assert(p.apply(3, false, 1000));
  assert(p.intervalMs() == 5000 && !p.accepts(999) && p.accepts(1001));
  p.reset(1100);
  assert(p.generation == 0 && !p.tracking && p.intervalMs() == 5000 && !p.accepts(1101));
  assert(p.highWater == 3);
  assert(!p.apply(2, true, 1101));
  assert(!p.apply(3, false, 1101)); // Reconnect must not revive the previous command.
  assert(p.apply(4, false, 1101));
  uint8_t header[operating::kHeaderSize] = {};
  assert(operating::kHeaderSize == 48);
  assert(!operating::encodeHeader(header, 1, 2, 3, 4, 1, 0));
  assert(!operating::encodeHeader(header, 1, 2, 3, 4, 1, operating::kMaxGeneration + 1));
  assert(!operating::encodeHeader(header, 0, 2, 3, 4, 1, 1));
  assert(!operating::encodeHeader(header, 1, 2, 4, 3, 1, 1));
  assert(!operating::encodeHeader(nullptr, 1, 2, 3, 4, 1, 1));
  assert(!operating::encodeHeader(header, 1, 2, 3, 4, high_rate::kMaxJpegBytes + 1, 1));
  assert(operating::encodeHeader(header, 0xf102030405060708ULL, 2, 3, 4, 1,
                                 operating::kMaxGeneration));
  assert(std::memcmp(header, "ECF2", 4) == 0);
  std::fwrite(header, 1, sizeof(header), stdout);
  std::fputc('x', stdout);
}
''')
    executable = tmp_path / "profile"
    flags = [f"-DEXPECTED_INTERVAL={interval or 190}"]
    if interval is not None:
        flags.append(f"-DOPERATING_TRACKING_INTERVAL_MS={interval}")
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", *flags, "-I",
                    str(FIRMWARE / "include"), str(source), "-o", str(executable)],
                   check=True, capture_output=True)
    raw = subprocess.check_output([str(executable)])
    frame = decode_frame(raw, allow_profiles=True)
    assert (frame.boot_id, frame.sequence, frame.captured_us, frame.sent_us,
            frame.mode_generation, frame.jpeg) == (0xf102030405060708, 2, 3, 4, MAX_GENERATION, b"x")
    with pytest.raises(ValueError):
        decode_frame(raw)  # Existing experiment/receiver must opt in explicitly.


@pytest.mark.parametrize("generation", [0, MAX_GENERATION + 1, 2**64 - 1])
def test_invalid_generation(generation):
    with pytest.raises(ValueError, match="generation"):
        decode_frame(PROFILE_HEADER.pack(b"ECF2", 1, 1, 2, 3, generation, 1) + b"x",
                     allow_profiles=True)


def test_length_limits_and_legacy_compatibility():
    legacy = HEADER.pack(b"ECF1", 1, 2, 3, 4, 1) + b"x"
    assert decode_frame(legacy).mode_generation is None
    assert decode_frame(legacy, allow_profiles=True) == decode_frame(legacy)
    raw = PROFILE_HEADER.pack(b"ECF2", 1, 2, 3, 4, 1, 128 * 1024) + b"x" * (128 * 1024)
    assert len(decode_frame(raw, allow_profiles=True).jpeg) == 128 * 1024
    for payload in (raw + b"x", raw[:-1], raw[:40], b"ECF2"):
        with pytest.raises(ValueError):
            decode_frame(payload, allow_profiles=True)


def test_profile_target_and_fresh_buffer_gate():
    ini = (FIRMWARE / "platformio.ini").read_text()
    section = ini.split("[env:esp32cam_operating_profiles]", 1)[1].split("[env:", 1)[0]
    assert "extends = env:esp32cam_high_rate_capture" in section
    assert "-D OPERATING_PROFILES=1" in section
    assert "esp32cam_operating_profiles" not in ini.split("default_envs =", 1)[1].split("\n", 1)[0]
    source = (FIRMWARE / "src" / "high_rate.cpp").read_text()
    assert source.index("!profile.accepts(capturedUs)") < source.index("memcpy(payload")
    loop = source.split("void loop() {", 1)[1]
    assert loop.index("ArduinoOTA.handle()") < loop.index("webSocket.loop()")
    assert loop.index("lastServerMs) >= 5000") < loop.index("captureAndSend()")


@pytest.mark.parametrize("interval", [0, 95, 101, 200, -1])
def test_invalid_tracking_interval_fails_compilation(tmp_path, interval):
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("host C++ compiler is unavailable")
    source = tmp_path / "invalid.cpp"
    source.write_text('#include "operating_profile.h"\nint main() {}\n')
    result = subprocess.run(
        [compiler, "-std=c++11", f"-DOPERATING_TRACKING_INTERVAL_MS={interval}", "-I",
         str(FIRMWARE / "include"), "-c", str(source), "-o", str(tmp_path / "invalid.o")],
        capture_output=True,
    )
    assert result.returncode != 0
    assert b"OPERATING_TRACKING_INTERVAL_MS must be 100 or 190" in result.stderr


def test_100ms_profile_target_is_explicit_and_compiled_in_ci():
    ini = (FIRMWARE / "platformio.ini").read_text()
    section = ini.split("[env:esp32cam_operating_profiles_100ms]", 1)[1].split("[env:", 1)[0]
    assert "extends = env:esp32cam_operating_profiles" in section
    assert "${env:esp32cam_operating_profiles.build_flags}" in section
    assert "-D OPERATING_TRACKING_INTERVAL_MS=100" in section
    assert "esp32cam_operating_profiles_100ms" not in ini.split("default_envs =", 1)[1].split("\n", 1)[0]
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "-e esp32cam_operating_profiles -e esp32cam_operating_profiles_100ms" in workflow
