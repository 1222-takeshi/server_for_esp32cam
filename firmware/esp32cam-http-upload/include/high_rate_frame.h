#pragma once

#include <stddef.h>
#include <stdint.h>

namespace high_rate {

constexpr size_t kHeaderSize = 40;
constexpr size_t kMaxJpegBytes = 128 * 1024;

inline bool isFresh(uint64_t capturedUs, uint64_t lastAcquiredUs) {
  return capturedUs != 0 && capturedUs > lastAcquiredUs;
}

inline void putBigEndian(uint8_t *out, uint64_t value, size_t bytes) {
  for (size_t i = 0; i < bytes; ++i) {
    out[bytes - i - 1] = static_cast<uint8_t>(value);
    value >>= 8;
  }
}

inline bool encodeHeader(uint8_t *out, uint64_t bootId, uint64_t frameSeq,
                         uint64_t capturedUs, uint64_t sentUs, size_t jpegLength) {
  if (out == nullptr || bootId == 0 || frameSeq == 0 || capturedUs == 0 ||
      sentUs < capturedUs ||
      jpegLength == 0 || jpegLength > kMaxJpegBytes) {
    return false;
  }
  out[0] = 'E'; out[1] = 'C'; out[2] = 'F'; out[3] = '1';
  putBigEndian(out + 4, bootId, 8);
  putBigEndian(out + 12, frameSeq, 8);
  putBigEndian(out + 20, capturedUs, 8);
  putBigEndian(out + 28, sentUs, 8);
  putBigEndian(out + 36, jpegLength, 4);
  return true;
}

}  // namespace high_rate
