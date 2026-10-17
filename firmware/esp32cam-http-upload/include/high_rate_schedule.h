#pragma once

#include <stdint.h>

namespace high_rate {

constexpr bool supportedCaptureInterval(uint32_t period) {
  return period == 95 || period == 100 || period == 190 || period == 200;
}

// Keep the deadline phase through loop jitter and uint32_t millis() rollover.
// Missed deadlines are skipped: at most one fresh capture is attempted per call.
inline bool captureDue(uint32_t now, uint32_t period, uint32_t &lastDeadline) {
  const uint32_t elapsed = now - lastDeadline;
  if (period == 0 || elapsed < period) return false;
  lastDeadline += (elapsed / period) * period;
  return true;
}

}  // namespace high_rate
