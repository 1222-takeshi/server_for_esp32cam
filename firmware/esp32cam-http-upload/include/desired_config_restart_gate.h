#pragma once

#include <stdint.h>

// Keeps a previously acknowledged restart pending while an OTA window is open.
// Call recordAck only with the actual HTTP ACK result.
class DesiredConfigRestartGate {
 public:
  void recordAck(bool succeeded, uint32_t version, bool requested, uint32_t lastHandledVersion) {
    if (succeeded && requested && version != lastHandledVersion) {
      pending_ = true;
      pendingVersion_ = version;
    }
  }

  bool consumeIfReady(bool otaWindowActive, uint32_t *version) {
    if (!pending_ || otaWindowActive || version == nullptr) {
      return false;
    }
    *version = pendingVersion_;
    pending_ = false;
    return true;
  }

 private:
  bool pending_ = false;
  uint32_t pendingVersion_ = 0;
};
