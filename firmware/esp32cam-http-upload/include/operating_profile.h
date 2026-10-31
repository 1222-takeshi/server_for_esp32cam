#pragma once

#include "high_rate_frame.h"

#ifndef OPERATING_TRACKING_INTERVAL_MS
#define OPERATING_TRACKING_INTERVAL_MS 190
#endif
static_assert(OPERATING_TRACKING_INTERVAL_MS == 100 || OPERATING_TRACKING_INTERVAL_MS == 190,
              "OPERATING_TRACKING_INTERVAL_MS must be 100 or 190");

namespace operating {
constexpr size_t kHeaderSize = 48;
constexpr uint64_t kMaxGeneration = (1ULL << 53) - 1;  // Exact JSON/UI integer.

struct Profile {
  uint64_t generation = 0;
  uint64_t highWater = 0;  // Retained across disconnect/reset for this boot.
  uint64_t appliedUs = 0;
  bool tracking = false;

  void reset(uint64_t nowUs) {
    generation = 0;
    appliedUs = nowUs;
    tracking = false;
  }

  bool apply(uint64_t requested, bool track, uint64_t nowUs) {
    if (requested == 0 || requested > kMaxGeneration || requested < highWater || nowUs == 0)
      return false;
    if (requested == highWater) return generation == requested && tracking == track;
    highWater = requested;
    generation = requested;
    tracking = track;
    appliedUs = nowUs;
    return true;
  }

  uint32_t intervalMs() const { return tracking ? OPERATING_TRACKING_INTERVAL_MS : 5000; }
  bool accepts(uint64_t capturedUs) const { return generation != 0 && capturedUs > appliedUs; }
};

inline bool encodeHeader(uint8_t *out, uint64_t boot, uint64_t seq,
                         uint64_t captured, uint64_t sent, size_t length,
                         uint64_t generation) {
  if (generation == 0 || generation > kMaxGeneration ||
      !high_rate::encodeHeader(out, boot, seq, captured, sent, length)) return false;
  out[3] = '2';
  high_rate::putBigEndian(out + 36, generation, 8);
  high_rate::putBigEndian(out + 44, length, 4);
  return true;
}
}  // namespace operating
