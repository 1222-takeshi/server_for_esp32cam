# Deployment Guide

This guide covers running the server in production with Docker.
For local development setup, see [setup.md](setup.md) and [running.md](running.md).

---

## Docker Quickstart (3 steps)

### 1. Copy and edit the environment file

```bash
cp .env.example .env
$EDITOR .env
```

Set at minimum:

```dotenv
ESP32CAM_DEVICE_API_KEY=<strong-random-key>
ESP32CAM_ADMIN_TOKEN=<strong-random-token>
```

### 2. Build and start

```bash
docker compose up -d --build
```

### 3. Verify

```bash
curl http://localhost:8080/healthz/ready
# Expected: {"status":"ready", ...}
```

The dashboard is available at `http://localhost:8080/`.

---

## Raspberry Pi Migration Runbook

Use this flow when moving the active server from a Mac to `raspi-server.local`.

### 1. Prepare the Raspberry Pi

```bash
ssh ubuntu@raspi-server.local
sudo apt-get update
sudo apt-get install -y git docker.io docker-compose-plugin
sudo usermod -aG docker ubuntu
```

Log out and back in after changing Docker group membership.

Check capacity before choosing retention limits:

```bash
df -h /
docker --version
docker compose version
tailscale status
```

### 2. Deploy the server

```bash
git clone https://github.com/1222-takeshi/server_for_esp32cam.git
cd server_for_esp32cam
cp .env.example .env
$EDITOR .env
```

Minimum production settings:

```dotenv
PORT=8080
ESP32CAM_DEVICE_API_KEY=<strong-random-key>
ESP32CAM_ADMIN_TOKEN=<strong-random-token>
ESP32CAM_STORAGE_RETENTION_MAX_AGE_SECONDS=2592000
ESP32CAM_STORAGE_RETENTION_MAX_BYTES=8589934592
ESP32CAM_VIDEO_RETENTION_MAX_AGE_SECONDS=0
ESP32CAM_VIDEO_RETENTION_MAX_BYTES=1073741824
```

For TFLite person detection on the Raspberry Pi:

```dotenv
ESP32CAM_DETECTOR=tflite_cpu
ESP32CAM_MODEL_PATH=/app/models/detect.tflite
ESP32CAM_LABELS_PATH=/app/models/labels.txt
ESP32CAM_DETECTION_THRESHOLD=0.5
ESP32CAM_INFERENCE_MODE=async
```

Place model files on the host under `./models/`; Docker Compose mounts that
directory read-only at `/app/models`. Do not commit model binaries to Git.

To keep only snapshots with successful detections:

```dotenv
ESP32CAM_KEEP_ONLY_DETECTED_SNAPSHOTS=true
```

This option deletes snapshots only when inference succeeds and returns zero detections.
Snapshots are kept when inference is disabled, skipped, or failed.

Start the service:

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8080/healthz/ready
```

### 2b. Deploy without Docker using user systemd

If Docker is not installed and sudo is not available in the automation session,
run the service with a Python virtual environment under the `ubuntu` user:

```bash
cd ~/server_for_esp32cam
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools
.venv/bin/python -m pip install . gunicorn gevent
mkdir -p uploads data models ~/.config/systemd/user
```

Create `~/.config/systemd/user/esp32cam-server.service`:

```ini
[Unit]
Description=ESP32-CAM snapshot server
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/server_for_esp32cam
EnvironmentFile=/home/ubuntu/server_for_esp32cam/.env
ExecStart=/home/ubuntu/server_for_esp32cam/.venv/bin/gunicorn -k gevent -w 1 -b 0.0.0.0:${PORT} script.server:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Start it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now esp32cam-server.service
systemctl --user status esp32cam-server.service --no-pager
curl http://127.0.0.1:8080/healthz/ready
```

This service runs while the user service manager is active. For boot persistence
without an interactive login, enable lingering with `sudo loginctl enable-linger ubuntu`.

### 3. Verify LAN access

From another device on the same network:

```bash
curl http://raspi-server.local:8080/healthz/ready
```

Open `http://raspi-server.local:8080/` from a PC or smartphone browser.
If mDNS does not resolve on a client, use the Raspberry Pi LAN IP from `hostname -I`.

### 4. Verify Tailscale access

On the Raspberry Pi:

```bash
tailscale ip -4
tailscale status
```

From a Tailscale-connected device:

```bash
curl http://<raspi-tailscale-ip>:8080/healthz/ready
```

Keep the server bound to Docker's `0.0.0.0:8080`, and use firewall/router policy to
avoid exposing it to the public internet. Tailscale access should require the same
application tokens as LAN access.

### 5. Switch traffic and stop the Mac server

After the Raspberry Pi passes readiness and browser checks:

1. Update ESP32-CAM upload URL to `http://raspi-server.local:8080/upload` or the
   Raspberry Pi LAN IP.
2. Confirm a new upload appears on the Raspberry Pi dashboard.
3. Stop the Mac container:

```bash
docker compose stop app
```

4. Keep the Mac Docker volumes until the Raspberry Pi has received new snapshots and
   retention behavior has been verified.
5. Remove the Mac snapshot volume only after confirming no rollback is needed:

```bash
docker volume rm server_for_esp32cam_uploads
```

Keep `server_for_esp32cam_data` longer if historical metadata is still needed.

---

## Environment Variables

All variables are optional unless marked **required for production**.

| Variable | Default | Description |
|----------|---------|-------------|
| `ESP32CAM_DEVICE_API_KEY` | _(empty — auth disabled)_ | **Required for production.** API key devices must send in `X-API-Key`. |
| `ESP32CAM_ADMIN_TOKEN` | _(empty — auth disabled)_ | **Required for production.** Bearer token for admin endpoints and `/metrics`. |
| `ESP32CAM_UPLOAD_DIR` | `./uploads` | Directory to store uploaded JPEG files. |
| `ESP32CAM_DB_PATH` | `./esp32cam.sqlite3` | Path to the SQLite database. |
| `PORT` | `8080` | TCP port the server listens on. |
| `ESP32CAM_MAX_CONTENT_LENGTH` | `1048576` (1 MiB) | Maximum JPEG payload size in bytes. |
| `ESP32CAM_MAX_VIDEO_CONTENT_LENGTH` | `10485760` (10 MiB) | Maximum video payload size in bytes. |
| `ESP32CAM_UPLOAD_MAX_IMAGE_DIMENSION` | `4096` | Maximum image dimension in pixels (width or height). |
| `ESP32CAM_UPLOAD_RATE_LIMIT` | `10 per minute` | Upload rate limit per device API key. Flask-Limiter format. |
| `ESP32CAM_VIDEO_UPLOAD_RATE_LIMIT` | `5 per minute` | Video upload rate limit per device API key. Flask-Limiter format. |
| `ESP32CAM_STORAGE_RETENTION_MAX_AGE_SECONDS` | `2592000` (30 days) | Delete images older than this. Set `0` to disable. |
| `ESP32CAM_STORAGE_RETENTION_MAX_BYTES` | `0` (disabled) | Delete oldest images when total exceeds this size. |
| `ESP32CAM_VIDEO_RETENTION_MAX_AGE_SECONDS` | `0` (disabled) | Delete videos older than this many seconds. Set `0` to disable. |
| `ESP32CAM_VIDEO_RETENTION_MAX_BYTES` | `0` (disabled) | Delete oldest videos when total video storage exceeds this size. |
| `ESP32CAM_DETECTOR` | `disabled` | Object detector. Options: `disabled`, `tflite_cpu`. |
| `ESP32CAM_MODEL_PATH` | _(empty)_ | Path to TFLite model file. Required when `DETECTOR=tflite_cpu`. |
| `ESP32CAM_LABELS_PATH` | _(empty)_ | Path to labels text file (optional). |
| `ESP32CAM_DETECTION_THRESHOLD` | `0.5` | Minimum confidence score to record a detection. |
| `ESP32CAM_PERSON_LABEL` | `person` | Label string that identifies a person. |
| `ESP32CAM_PERSON_CLASS_ID` | `0` | Class index for the person class in the model. |
| `ESP32CAM_CAMERA_EXPECTED_INTERVAL_SECONDS` | `300` | Seconds between expected uploads; used to compute camera health. |
| `ESP32CAM_KEEP_ONLY_DETECTED_SNAPSHOTS` | `false` | If true, delete snapshots with successful inference and zero detections. |
| `ESP32CAM_MAX_WS_CONNECTIONS` | `50` | Maximum concurrent WebSocket connections. |
| `ESP32CAM_WEBHOOK_URL` | _(empty — disabled)_ | Webhook URL to POST when a person is detected. |
| `ESP32CAM_WEBHOOK_MIN_SCORE` | `0.5` | Minimum score to trigger the webhook. |
| `ESP32CAM_RATELIMIT_STORAGE_URI` | `memory://` | Flask-Limiter storage. Use `redis://host:6379` for multi-worker deployments. |
| `ESP32CAM_UPLOAD_WS_MIN_FRAME_INTERVAL_SECONDS` | `1.0` | Minimum seconds between WebSocket frames per connection. |

---

## Production Checklist

- [ ] `ESP32CAM_DEVICE_API_KEY` is set to a strong random value
- [ ] `ESP32CAM_ADMIN_TOKEN` is set to a strong random value
- [ ] `ESP32CAM_UPLOAD_DIR` points to a persistent volume (not ephemeral container storage)
- [ ] `ESP32CAM_DB_PATH` points to a persistent volume
- [ ] Storage retention limits are configured (`MAX_AGE_SECONDS` and/or `MAX_BYTES`)
- [ ] HTTPS termination is handled upstream (nginx/Caddy/cloud load balancer)
- [ ] `/api/docs` is not exposed publicly (Swagger UI loads scripts from CDN)

---

## Data Volumes

The Docker Compose file mounts two named volumes:

| Volume | Container path | Contents |
|--------|---------------|----------|
| `uploads` | `/app/uploads` | JPEG image files organized by `<camera_id>/images/` |
| `data` | `/app/data` | SQLite database (`esp32cam.sqlite3`) |

Ensure these volumes survive container restarts. Docker named volumes persist by default unless explicitly removed.

---

## Backup and Recovery

### Backup

Back up both the database and the image files together so they stay consistent.

```bash
# Stop the server to prevent writes during backup (or use SQLite WAL snapshot)
docker compose stop app

# Copy database
docker run --rm \
  -v server_for_esp32cam_data:/src \
  -v "$(pwd)/backup":/dst \
  busybox cp /src/esp32cam.sqlite3 /dst/esp32cam_$(date +%Y%m%d_%H%M%S).sqlite3

# Copy images
docker run --rm \
  -v server_for_esp32cam_uploads:/src \
  -v "$(pwd)/backup/uploads":/dst \
  busybox sh -c "cp -r /src/. /dst/"

docker compose start app
```

For online backup (without stopping), the SQLite WAL mode is enabled by default.
Use `.backup` SQL command or `sqlite3 /path/to/db .dump > dump.sql` against the live file.

### Recovery

```bash
docker compose stop app

# Restore database
docker run --rm \
  -v server_for_esp32cam_data:/dst \
  -v "$(pwd)/backup":/src \
  busybox cp /src/esp32cam_<timestamp>.sqlite3 /dst/esp32cam.sqlite3

# Restore images
docker run --rm \
  -v server_for_esp32cam_uploads:/dst \
  -v "$(pwd)/backup/uploads":/src \
  busybox sh -c "cp -r /src/. /dst/"

docker compose start app
```

After recovery, visit `/healthz/ready` to confirm the server is ready.

---

## Updating

```bash
git pull
docker compose up -d --build
```

The database schema is managed automatically via SQLite migrations on startup.
No manual migration step is required.

---

## Troubleshooting

See [troubleshooting.md](troubleshooting.md) for common issues.

Key endpoints for diagnostics:

| Endpoint | Purpose |
|----------|---------|
| `GET /healthz` | Liveness check (is the process alive?) |
| `GET /healthz/ready` | Readiness check (DB, storage, disk, inference worker) |
| `GET /metrics` | Prometheus metrics (requires `ADMIN_TOKEN`) |
| `GET /api/docs` | Interactive API documentation (Swagger UI) |
