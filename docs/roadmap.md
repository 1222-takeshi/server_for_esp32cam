# Development roadmap

Updated 2026-10-11. Implementation and real-device validation are tracked separately. The orchestrator owns milestones, integration, and merge decisions; domain PMs own independent Issues and worktrees. Tracking Issue: [#451](https://github.com/1222-takeshi/server_for_esp32cam/issues/451). The detailed acceptance plan is in the [Japanese roadmap](ja/roadmap.md).

| Milestone | Current capability | Work and acceptance |
|---|---|---|
| [M1: Reliable viewing](https://github.com/1222-takeshi/server_for_esp32cam/milestone/1) | JPEG upload/storage, browsing, newly received MJPEG frames on one real camera | [#444](https://github.com/1222-takeshi/server_for_esp32cam/issues/444): refresh retained snapshots, distinguish saved images from live frames, handle empty/error states, validate on the Pi |
| [M2: Operational reliability](https://github.com/1222-takeshi/server_for_esp32cam/milestone/2) | Pi service, Tailscale SSH, Netdata | [#445](https://github.com/1222-takeshi/server_for_esp32cam/issues/445), [#448](https://github.com/1222-takeshi/server_for_esp32cam/issues/448): bounded observations, isolated backup restoration, seven actual days of observation and recovery results |
| [M3: Remote device configuration](https://github.com/1222-takeshi/server_for_esp32cam/milestone/3) | Desired-config/ACK API and a tested OTA update path | [#446](https://github.com/1222-takeshi/server_for_esp32cam/issues/446): polling/apply/ACK in the active WebSocket firmware, preserve its five-second interval explicitly, test one-shot actions and reconnection |
| [M4: Pet monitoring evaluation](https://github.com/1222-takeshi/server_for_esp32cam/milestone/4) | Person detection and detected-only snapshot retention | [#447](https://github.com/1222-takeshi/server_for_esp32cam/issues/447): immutable labeled images/model provenance, separate image/object precision and recall, latency and real cat evaluation |

M1–M3 implementations and the M4 evaluation tool can proceed in parallel with separate file ownership. Device deployment is coordinated sequentially. Every PR needs meaningful tests, static checks, CI validation, and at least two read-only reviews; unresolved BLOCKING/IMPORTANT findings prevent merge.

Seven-day observation starts when the actual timer runs. Tool completion does not establish continuous uptime. Cat accuracy remains unevaluated until real labeled images are available; synthetic fixtures verify the evaluator only. Record acceptance thresholds before measurement and preserve baselines.

Webhook, video upload, camera-aware management, and tracking have server implementations. Notification delivery, ESP-side short clips, multiple physical cameras, and tracking still need real-device validation. After the foundations pass, plan these as separate Issues. NAS/Drive/S3, Tapo integration, and moving hardware remain future work requiring explicit interface and operational decisions.
