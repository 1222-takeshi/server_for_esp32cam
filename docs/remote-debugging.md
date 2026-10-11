# Remote Debugging ESP32-CAM Devices (No Serial Cable)

ESP32-CAM firmware bakes the server address and device API key in at build
time (`firmware/esp32cam-http-upload/include/esp32cam_config.h`). When the
server moves to a new host or rotates keys, an un-reflashed device silently
keeps calling the old address with the old key. Without a serial cable the
device cannot tell you this — but the network can. This runbook shows how to
diagnose and recover such a device entirely over the network.

It was written after a real incident: after the server migrated from a Mac
to a Raspberry Pi, the deployed camera kept targeting the old Mac IP with a
rotated-out API key. Every step below was used to find and fix that without
touching the device.

## 1. Triage order: server → network → device

Work outward from what you can observe. Each step isolates one layer.

### 1.1 Is the server healthy?

```bash
curl http://<server>:8080/healthz/ready
# {"status":"ready","db":"ok","storage":"ok",...}
curl http://<server>:8080/api/cameras
# check last_seen_at — if it is stale, uploads are not arriving
```

### UI connection diagnostics

`/cameras/<camera_id>` の「接続診断」は、最終通信からの経過秒数、サーバーの
readiness、送信先IP変更・Wi-Fi・認証キーの確認項目を表示します。
30秒ごとに更新され、取得失敗時は確認不能と表示します。`offline` は未受信の
時間から算出した状態で、電源断やWi-Fi切断を確定するものではありません。

詳細画面のライブ画像・受信時刻は、保存画像とは別です。保存済みの件数と一覧は
`GET /api/cameras/<camera_id>/snapshots?limit=20` で10秒ごとに更新されます。
「最終保存」が古くても、検知対象だけを保存する設定なら正常な場合があります。
一覧の取得に失敗した場合は前回確認時点の表示が残り、確認不能と表示されます。
トップ画面の保存件数とサムネイルはページを開いた時点の値です。

詳細画面の `saved_snapshots.js` は長期の `immutable` キャッシュを使います。
JS を更新するときは、`camera_detail.html` の script URL にある `v` も
新しい JS ファイルの SHA-256 先頭12桁へ更新してください。同じ URL のままでは
既存ブラウザに旧版が残ります。HTML の回帰テストが版とファイル内容の一致を確認します。

カメラに設定すべきLAN側の送信先を表示するには、サーバーの `.env` に設定して
再起動してください（表示専用で、実機の設定変更や自動照合は行いません）。

```dotenv
ESP32CAM_DEVICE_SERVER_BASE_URL=http://raspi-server.local:8080
```

この値には認証情報、path、query、fragmentを含めないでください。未指定・不正な
値は表示しません。Tailscaleの閲覧用IPをESP32の送信先と混同しないでください。
PiのDHCPアドレス変更に備え、mDNSで解決できる `.local` 名かDHCP予約を使います。
送信先の変更後は新しいsnapshotの到着まで確認してください。

### 1.2 Is the device on the network?

```bash
ping esp32cam-01.local          # mDNS answers ⇒ Wi-Fi stack is alive
dscacheutil -q host -a name esp32cam-01.local   # its current IP (macOS)
```

A device that answers ping but never appears in `last_seen_at` is running,
connected to Wi-Fi, and failing at the application layer — almost always a
stale target address or key.

### 1.3 Is the device reaching the server at all?

On the server, watch for TCP connections from the device IP:

```bash
ssh <server> "for i in \$(seq 1 30); do
  ss -tn state all '( sport = :8080 )' | grep <device-ip>; sleep 0.5; done"
```

No hits while the device is up ⇒ the device is connecting **somewhere
else** — proceed to 1.4.

The server's Prometheus metrics narrow down failures that do reach it. A
WebSocket upload session that lasts only a few milliseconds means the server
accepted the handshake and immediately closed it, which is the signature of
an **API-key rejection** (`Unauthorized` is sent and the handler returns):

```bash
curl -s -H "Authorization: Bearer $ESP32CAM_ADMIN_TOKEN" \
  http://<server>:8080/metrics | grep camera_upload
```

### 1.4 What is the device actually sending, and to whom?

If the device is not reaching the current server, it is targeting a stale
address — usually a previous server machine. On the machine that owns that
old address, listen on the port and capture one request:

```bash
python3 - <<'EOF'
import socket
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 8080)); srv.listen(1); srv.settimeout(30)
conn, addr = srv.accept()
print("connection from", addr)
request = conn.recv(4096).decode(errors="replace")
for line in request.splitlines():
    header, separator, _ = line.partition(":")
    if separator and header.strip().lower() == "x-api-key":
        print(f"{header}: [REDACTED]")
    else:
        print(line)
EOF
```

Within one retry interval (5 s for the WebSocket firmware) you get the request
metadata: target host header, path, device ID, and a redacted `X-API-Key`
header. Compare the device's configured key against the server's current
`ESP32CAM_DEVICE_API_KEY` by hash, never by printing either value:

```bash
python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" '<key>'
```

## 2. Recovery without reflashing

### 2.1 Stale key only → accept the old key server-side

If the device targets the **correct address** but a **rotated-out key**, no
proxy is needed: the server already supports per-device keys. Add the stale
key to the server's `.env` and restart it:

```dotenv
ESP32CAM_DEVICE_API_KEYS={"esp32cam-01": "<stale-key>"}
```

The mapping is checked against the request's `device_id`, so the device
recovers immediately without allowing the key to impersonate another device.
Remove the entry once the device is re-flashed.

For a single-device deployment that still uses the legacy global key, set both
the key and its fixed identity instead:

```dotenv
ESP32CAM_DEVICE_API_KEY=<current-key>
ESP32CAM_SHARED_KEY_DEVICE_ID=esp32cam-01
```

Never use one global key for multiple device IDs; configure
`ESP32CAM_DEVICE_API_KEYS` for that case.

### 2.2 Stale address (± stale key) → the bridge proxy

If the device targets an **old server address**, run the bridge proxy on the
machine that owns that address. It forwards all traffic (HTTP and WebSocket)
to the real server and rewrites the stale `X-API-Key` header in flight:

```bash
export ESP32CAM_DEVICE_API_KEY=<current-key>
python3 scripts/esp32cam_bridge_proxy.py \
  --upstream raspi-server.local:8080 --allow-source <device-ip>
```

Snapshots resume within one retry interval. Confirm with `GET /api/cameras`
(`last_seen_at` becomes current).

**Always pass `--allow-source`.** The proxy replaces the `X-API-Key` of any
matching request with a valid key, so an unrestricted proxy lets every host
that can reach its port authenticate as a device. Restrict it to the stranded
device's IP.

This is a stopgap: the proxy machine must stay on, and the stale key keeps
crossing the LAN. Only the first request of each TCP connection is rewritten
(fine for the retrying firmwares, which reconnect per attempt). Schedule the
permanent fix below.

## 3. Permanent fix and prevention

1. **Re-flash with an OTA-enabled environment** so the next stale-config
   incident can be fixed over the network:

   ```bash
   cd firmware/esp32cam-http-upload
   pio run -e esp32cam_websocket_upload_ota_usb -t upload   # one-time USB flash
   ```

   After that, updates go over the air with no cable:

   ```bash
   export ESP32CAM_OTA_HOST=esp32cam-01.local ESP32CAM_OTA_PORT=3232 ESP32CAM_OTA_PASSWORD=<ota-pass>
   pio run -e esp32cam_websocket_upload_ota -t upload
   ```

2. **Before every flash, verify the config matches the server.** The values
   that must agree with the server's `.env`:
   - `WS_UPLOAD_HOST` / `UPLOAD_URL` / `DEVICE_API_BASE_URL` → the server's
     current address
   - `DEVICE_API_KEY` → `ESP32CAM_DEVICE_API_KEY` (compare SHA-256 hashes)

3. **Pin the server's IP** with a DHCP reservation in the router so a lease
   change never strands deployed devices, or use an mDNS-resolvable name.

4. **After flashing, verify end to end**: `GET /api/cameras` shows a fresh
   `last_seen_at`, and an actual over-the-air update succeeds
   (`pio run -e esp32cam_websocket_upload_ota -t upload` with the
   `ESP32CAM_OTA_*` variables set). Note that ArduinoOTA listens on
   **UDP** port 3232, so a TCP probe such as `nc -z <host> 3232` reports
   "connection refused" even when OTA is working.

## 4. Notes for multi-device benches

Multiple ESP32 boards on one machine are easy to confuse. Identify serial
ports by USB bridge chip before touching anything (`system_profiler
SPUSBDataType` on macOS): each adapter model (CP2102, CH340, FTDI) maps to a
distinct `/dev/cu.*` name, and reading a few seconds of boot output confirms
which firmware is on which port. Never flash a port you have not positively
identified.
