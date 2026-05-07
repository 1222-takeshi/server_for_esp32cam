#include <Arduino.h>

namespace {
constexpr uint32_t kBaudRate = 115200;
constexpr uint32_t kLogIntervalMs = 5000;
uint32_t lastLogMs = 0;
uint32_t bootCount = 0;

void printBootInfo() {
  ++bootCount;
  Serial.println();
  Serial.println("========================================");
  Serial.println("ESP32-CAM PlatformIO serial smoke test");
  Serial.printf("Boot count: %lu\n", static_cast<unsigned long>(bootCount));
  Serial.printf("Chip revision: %u\n", ESP.getChipRevision());
  Serial.printf("CPU frequency: %u MHz\n", ESP.getCpuFreqMHz());
  Serial.printf("Flash size: %u bytes\n", ESP.getFlashChipSize());
  Serial.printf("Free heap: %u bytes\n", ESP.getFreeHeap());
  Serial.printf("PSRAM: %s\n", psramFound() ? "found" : "not found");
  if (psramFound()) {
    Serial.printf("PSRAM size: %u bytes\n", ESP.getPsramSize());
    Serial.printf("Free PSRAM: %u bytes\n", ESP.getFreePsram());
  }
  Serial.println("Serial smoke test is running.");
  Serial.println("Next step after this passes: Wi-Fi connection test.");
  Serial.println("========================================");
}
}  // namespace

void setup() {
  Serial.begin(kBaudRate);
  delay(1000);
  printBootInfo();
}

void loop() {
  const uint32_t now = millis();
  if (now - lastLogMs >= kLogIntervalMs) {
    lastLogMs = now;
    Serial.printf("alive: millis=%lu free_heap=%u\n",
                  static_cast<unsigned long>(now),
                  ESP.getFreeHeap());
  }
  delay(10);
}
