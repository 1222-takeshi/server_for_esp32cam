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
