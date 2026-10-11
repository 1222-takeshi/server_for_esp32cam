"""Execute the firmware's restart action gate with a host C++ compiler."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


INCLUDE = (
    Path(__file__).resolve().parents[1] / "firmware" / "esp32cam-http-upload" / "include"
)


def test_restart_gate_ack_failure_ota_deferral_and_replay(tmp_path: Path) -> None:
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("host C++ compiler is unavailable")
    source = tmp_path / "restart_gate.cpp"
    source.write_text(
        """
#include <cassert>
#include "desired_config_restart_gate.h"

int main() {
  DesiredConfigRestartGate gate;
  uint32_t version = 0;

  gate.recordAck(false, 2, true, 0);
  assert(!gate.consumeIfReady(false, &version));  // failed ACK never restarts

  gate.recordAck(true, 2, true, 0);
  assert(!gate.consumeIfReady(true, &version));   // active OTA defers restart
  assert(!gate.consumeIfReady(false, nullptr));   // invalid output is harmless
  assert(gate.consumeIfReady(false, &version));
  assert(version == 2);
  assert(!gate.consumeIfReady(false, &version));  // no replay in this boot

  gate.recordAck(true, 2, true, 2);
  assert(!gate.consumeIfReady(false, &version));  // RTC handled version
  gate.recordAck(true, 3, false, 2);
  assert(!gate.consumeIfReady(false, &version));  // unrelated config
  gate.recordAck(true, 4, true, 2);
  assert(gate.consumeIfReady(false, &version));
  assert(version == 4);
}
""",
        encoding="utf-8",
    )
    executable = tmp_path / "restart_gate"
    subprocess.run(
        [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-I", str(INCLUDE), str(source), "-o", str(executable)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)
