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
print(conn.recv(4096).decode(errors="replace"))
EOF
```

Within one retry interval (5 s for the WebSocket firmware) you get the full
request: target host header, path, device ID, and the `X-API-Key` it was
built with. Compare that key against the server's current
`ESP32CAM_DEVICE_API_KEY` by hash, never by printing either value:

```bash
python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" '<key>'
```

## 2. Recovery without reflashing: the bridge proxy

Once you know the device targets a stale address/key, restore service
immediately by bridging it to the real server. On the machine whose IP the
device targets:

```bash
export ESP32CAM_DEVICE_API_KEY=<current-key>
python3 scripts/esp32cam_bridge_proxy.py --upstream raspi-server.local:8080
```

The proxy forwards all traffic (HTTP and WebSocket) to the real server and
rewrites the stale `X-API-Key` header in flight. Snapshots resume within one
retry interval. Confirm with `GET /api/cameras` (`last_seen_at` becomes
current).

This is a stopgap: the proxy machine must stay on, and the stale key keeps
crossing the LAN. Schedule the permanent fix below.

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
   `last_seen_at`, and OTA answers on port 3232
   (`nc -z esp32cam-01.local 3232`).

## 4. Notes for multi-device benches

Multiple ESP32 boards on one machine are easy to confuse. Identify serial
ports by USB bridge chip before touching anything (`system_profiler
SPUSBDataType` on macOS): each adapter model (CP2102, CH340, FTDI) maps to a
distinct `/dev/cu.*` name, and reading a few seconds of boot output confirms
which firmware is on which port. Never flash a port you have not positively
identified.
