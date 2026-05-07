#include <Arduino.h>
#include <cstring>

#ifdef WIFI_SMOKE_TEST
#include <WiFi.h>
#endif

#if __has_include("esp32cam_config.h")
#include "esp32cam_config.h"
#define ESP32CAM_HAS_LOCAL_CONFIG 1
#else
#define ESP32CAM_HAS_LOCAL_CONFIG 0
#define WIFI_SSID ""
#define WIFI_PASSWORD ""
#define WIFI_CONNECT_TIMEOUT_MS 20000
#endif

namespace {
constexpr uint32_t kBaudRate = 115200;
constexpr uint32_t kLogIntervalMs = 5000;
uint32_t lastLogMs = 0;
uint32_t bootCount = 0;

void printHardwareInfo() {
  ++bootCount;
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
}

void printSerialSmokeBootInfo() {
  Serial.println();
  Serial.println("========================================");
  Serial.println("ESP32-CAM PlatformIO serial smoke test");
  printHardwareInfo();
  Serial.println("Serial smoke test is running.");
  Serial.println("Next step after this passes: Wi-Fi connection test.");
  Serial.println("========================================");
}

#ifdef WIFI_SMOKE_TEST
void printMacAddress() {
  uint8_t mac[6] = {0};
  WiFi.macAddress(mac);
  Serial.printf("Wi-Fi MAC: %02X:%02X:%02X:%02X:%02X:%02X\n",
                mac[0],
                mac[1],
                mac[2],
                mac[3],
                mac[4],
                mac[5]);
}

void connectWiFi() {
  Serial.println();
  Serial.println("========================================");
  Serial.println("ESP32-CAM PlatformIO Wi-Fi smoke test");
  printHardwareInfo();
  printMacAddress();

#if ESP32CAM_HAS_LOCAL_CONFIG
  Serial.println("Local config: include/esp32cam_config.h found");
#else
  Serial.println("Local config: include/esp32cam_config.h not found");
  Serial.println("Copy include/esp32cam_config.h.example to include/esp32cam_config.h and set WIFI_SSID/WIFI_PASSWORD.");
#endif

  if (strlen(WIFI_SSID) == 0 || strcmp(WIFI_SSID, "YOUR_WIFI_SSID") == 0) {
    Serial.println("Wi-Fi smoke test skipped: WIFI_SSID is not configured.");
    Serial.println("========================================");
    return;
  }

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.disconnect(true, true);
  delay(500);

  Serial.printf("Connecting to Wi-Fi SSID: %s\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  const uint32_t startedAt = millis();
  uint32_t lastDotAt = 0;
  while (WiFi.status() != WL_CONNECTED && millis() - startedAt < WIFI_CONNECT_TIMEOUT_MS) {
    const uint32_t now = millis();
    if (now - lastDotAt >= 1000) {
      lastDotAt = now;
      Serial.print('.');
    }
    delay(50);
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("WiFi connected");
    Serial.printf("IP address: %s\n", WiFi.localIP().toString().c_str());
    Serial.printf("Gateway: %s\n", WiFi.gatewayIP().toString().c_str());
    Serial.printf("Subnet: %s\n", WiFi.subnetMask().toString().c_str());
    Serial.printf("DNS: %s\n", WiFi.dnsIP().toString().c_str());
    Serial.printf("RSSI: %d dBm\n", WiFi.RSSI());
    Serial.println("Next step after this passes: camera init test.");
  } else {
    Serial.printf("WiFi connection failed: status=%d timeout_ms=%lu\n",
                  static_cast<int>(WiFi.status()),
                  static_cast<unsigned long>(WIFI_CONNECT_TIMEOUT_MS));
    Serial.println("Check SSID/password, 2.4GHz network availability, MAC filtering, and AP client isolation.");
  }
  Serial.println("========================================");
}
#endif
}  // namespace

void setup() {
  Serial.begin(kBaudRate);
  delay(1000);
#ifdef WIFI_SMOKE_TEST
  connectWiFi();
#else
  printSerialSmokeBootInfo();
#endif
}

void loop() {
  const uint32_t now = millis();
  if (now - lastLogMs >= kLogIntervalMs) {
    lastLogMs = now;
#ifdef WIFI_SMOKE_TEST
    Serial.printf("alive: millis=%lu wifi_status=%d ip=%s rssi=%d free_heap=%u\n",
                  static_cast<unsigned long>(now),
                  static_cast<int>(WiFi.status()),
                  WiFi.localIP().toString().c_str(),
                  WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0,
                  ESP.getFreeHeap());
#else
    Serial.printf("alive: millis=%lu free_heap=%u\n",
                  static_cast<unsigned long>(now),
                  ESP.getFreeHeap());
#endif
  }
  delay(10);
}
