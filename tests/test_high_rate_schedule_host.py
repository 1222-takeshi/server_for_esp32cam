"""Compile real scheduling code to check cadence, stalls, and millis wrap."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_schedule_preserves_phase_without_catchup_bursts(tmp_path):
    compiler = shutil.which('c++')
    if compiler is None:
        pytest.skip('host C++ compiler is unavailable')
    source = tmp_path / 'schedule.cpp'
    source.write_text(r'''
#include <cassert>
#include <cstdint>
#include <initializer_list>
#include "high_rate_schedule.h"
int main() {
  static_assert(high_rate::supportedCaptureInterval(190), "5Hz headroom candidate");
  static_assert(!high_rate::supportedCaptureInterval(0), "zero is invalid");
  static_assert(!high_rate::supportedCaptureInterval(191), "unsupported interval");
  uint32_t last = 0;
  assert(!high_rate::captureDue(99, 100, last) && last == 0);
  assert(high_rate::captureDue(100, 100, last) && last == 100);
  assert(high_rate::captureDue(203, 100, last) && last == 200);
  assert(!high_rate::captureDue(299, 100, last));
  assert(high_rate::captureDue(300, 100, last) && last == 300);
  assert(high_rate::captureDue(895, 100, last) && last == 800);
  assert(!high_rate::captureDue(895, 100, last)); // never burst missed frames
  assert(high_rate::captureDue(900, 100, last));
  assert(!high_rate::captureDue(1000, 0, last) && last == 900);
  last = UINT32_MAX - 49;
  assert(!high_rate::captureDue(49, 100, last));
  assert(high_rate::captureDue(50, 100, last) && last == 50);
  assert(high_rate::captureDue(151, 100, last) && last == 150);
  for (uint32_t period : {95u, 100u, 190u, 200u}) {
    assert(high_rate::supportedCaptureInterval(period));
    last = 0;
    for (uint32_t n = 1; n <= 1000; ++n) {
      assert(high_rate::captureDue(n * period + 1, period, last));
      assert(last == n * period);
    }
  }
}
''')
    binary = tmp_path / 'schedule'
    subprocess.run([compiler, '-std=c++11', '-Wall', '-Wextra', '-Werror', '-I',
                    str(ROOT / 'firmware/esp32cam-http-upload/include'),
                    str(source), '-o', str(binary)], check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, capture_output=True)
