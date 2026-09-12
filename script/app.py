from __future__ import annotations

import atexit
import gzip
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, g, jsonify, request
from gevent.threadpool import ThreadPool
from werkzeug.exceptions import RequestEntityTooLarge

from script.auth import validate_auth_config
from script.config import Config, gunicorn_worker_count
from script.detector import DisabledDetector, TFLiteCpuDetector
from script.events import SnapshotEventPublisher
from script.inference import InferenceService
from script.logging_config import configure_logging
from script.metadata import SQLiteMetadataStore
from script.prometheus_metrics import AppMetrics
from script.retention import RetentionScanGate, RetentionScheduler
from script.routes import limiter, server
from script.storage import LocalFilesystemStorage, StorageRetentionPolicy
from script.thumbnail_cache import ThumbnailCache
from script.tracking import ObjectTracker
from script.upload_metrics import UploadMetricsRegistry
from script.webhook import DISABLED_NOTIFIER, WebhookNotifier
from script.websocket_routes import register_websocket_routes

JST = ZoneInfo("Asia/Tokyo")

# JPEG/MP4 responses are already compressed and served via send_file with
# long-lived Cache-Control (see storage/routes), so gzip only ever applies
# to the text-ish payloads below (dashboard HTML, JSON APIs, vendored
# CSS/JS). Small bodies are skipped since gzip overhead outweighs the
# bandwidth saved below this size.
_COMPRESSIBLE_MIMETYPES = frozenset(
    {
        "text/html",
        "text/css",
        "text/plain",
        "text/javascript",
        "application/javascript",
        "application/json",
        "image/svg+xml",
        "application/yaml",
        "text/csv",
    }
)
_MIN_COMPRESS_BYTES = 500

# Vendored/static assets (e.g. the ~1.5MB swagger-ui-bundle.js) never change
# at runtime, so re-running gzip.compress() on every cache-miss request (a
# fresh client, or one that dropped its immutable browser cache) is wasted
# CPU on the single-worker request path. Cache the compressed bytes per
# request path, keyed by the file's ETag (set by send_file from its mtime +
# size), so a changed file automatically invalidates its entry. Bounded by
# the number of distinct files under static/, not by request volume.
_compressed_static_cache: dict[str, tuple[str, bytes]] = {}
_compressed_static_cache_lock = threading.Lock()


def _maybe_compress_response(response):
    """Gzip-compress compressible, non-streamed responses when the client
    supports it. Reduces bandwidth for the dashboard/API without touching
    already-compressed binary responses (images/video) or streaming
    responses (MJPEG, WebSocket upgrades, Range/conditional send_file)."""

    if response.headers.get("Content-Encoding"):
        return response
    if "gzip" not in request.accept_encodings:
        return response
    if (response.mimetype or "") not in _COMPRESSIBLE_MIMETYPES:
        return response

    if response.direct_passthrough or response.is_streamed:
        # Flask's `static` endpoint (base.css, vendored swagger-ui JS/CSS,
        # ...) is served via send_file with direct_passthrough=True (and a
        # file-wrapper iterable, so also is_streamed) for efficient
        # wsgi.file_wrapper delivery, which otherwise bypasses gzip
        # entirely -- even though these are exactly the compressible,
        # long-lived-cached assets this function targets (e.g. the ~1.5MB
        # vendored swagger-ui-bundle.js served uncompressed on every
        # uncached /api/docs load). Buffering the file into memory here is
        # safe for this endpoint specifically: static assets are small
        # (the largest vendored file is a few MB), and Range requests are
        # left untouched below since compression would break byte-offset
        # semantics.
        if (
            response.status_code != 200
            or request.endpoint != "static"
            or "Range" in request.headers
        ):
            return response
        response.direct_passthrough = False

    is_static = request.endpoint == "static"
    etag = response.headers.get("ETag") if is_static else None
    if is_static and etag:
        with _compressed_static_cache_lock:
            cached = _compressed_static_cache.get(request.path)
        if cached is not None and cached[0] == etag:
            response.set_data(cached[1])
            response.headers["Content-Encoding"] = "gzip"
            vary = response.headers.get("Vary")
            if not vary:
                response.headers["Vary"] = "Accept-Encoding"
            elif "Accept-Encoding" not in vary:
                response.headers["Vary"] = f"{vary}, Accept-Encoding"
            return response

    data = response.get_data()
    if len(data) < _MIN_COMPRESS_BYTES:
        return response

    compressed = gzip.compress(data, compresslevel=6)
    if is_static and etag:
        with _compressed_static_cache_lock:
            _compressed_static_cache[request.path] = (etag, compressed)

    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    vary = response.headers.get("Vary")
    if not vary:
        response.headers["Vary"] = "Accept-Encoding"
    elif "Accept-Encoding" not in vary:
        response.headers["Vary"] = f"{vary}, Accept-Encoding"
    return response


def _create_detector(app: Flask):
    detector_kind = app.config.get("DETECTOR", "disabled")
    if detector_kind == "disabled":
        return DisabledDetector("Detector is disabled")
    if detector_kind != "tflite_cpu":
        return DisabledDetector(f"Unsupported detector: {detector_kind}", skip=False)

    model_path = app.config.get("MODEL_PATH", "")
    if not model_path:
        return DisabledDetector("TFLite model path is not configured", skip=False)

    labels_path = app.config.get("LABELS_PATH", "")
    try:
        return TFLiteCpuDetector(
            model_path=model_path,
            threshold=app.config["DETECTION_THRESHOLD"],
            labels_path=labels_path or None,
            person_label=app.config["PERSON_LABEL"],
            person_class_id=app.config["PERSON_CLASS_ID"],
            num_threads=app.config.get("DETECTOR_NUM_THREADS"),
        )
    # Intentional catch-all: a missing/broken optional ML dependency or model
    # file should degrade to a disabled detector, not crash startup.
    except Exception as exc:  # pragma: no cover  # noqa: BLE001
        return DisabledDetector(str(exc), skip=False)


def start_retention_scheduler(app: Flask) -> bool:
    """Start retention in the current process when it is not a test app.

    Called from `_before_request` on every request when
    RETENTION_SCHEDULER_AUTO_START is disabled (the `--preload` deployment
    mode), so this checks the lock-free `is_running` property before
    calling `scheduler.start()` — which re-acquires `_state_lock` on every
    call — to avoid taking that lock on every request once the scheduler is
    already up.
    """

    if app.config.get("TESTING", False):
        return False
    scheduler = app.extensions.get("retention_scheduler")
    if scheduler is None or scheduler.is_running:
        return False
    return scheduler.start()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Config.BASE_DIR / "templates"),
        static_folder=str(Config.BASE_DIR / "static"),
    )
    app.config.from_object(Config)
    # Serve static assets (e.g. templates/_base.html's extracted CSS) with a
    # long-lived, cacheable response instead of relying on Flask's default
    # (no max-age, revalidate-on-every-request) behavior.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000

    @app.template_filter("jst")
    def format_jst(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S JST") -> str:
        if value is None:
            return "—"
        if value.tzinfo is None:
            value = value.replace(tzinfo=JST)
        return value.astimezone(JST).strftime(fmt)

    if test_config:
        app.config.update(test_config)

    snapshot_max_content_length = app.config.get(
        "SNAPSHOT_MAX_CONTENT_LENGTH",
        app.config.get("MAX_CONTENT_LENGTH", 1 * 1024 * 1024),
    )
    app.config["SNAPSHOT_MAX_CONTENT_LENGTH"] = snapshot_max_content_length
    app.config["MAX_CONTENT_LENGTH"] = max(
        snapshot_max_content_length,
        app.config.get("MAX_VIDEO_CONTENT_LENGTH", snapshot_max_content_length),
    )

    configure_logging(app)
    validate_auth_config(app)

    worker_count = gunicorn_worker_count()
    if (
        worker_count is not None
        and worker_count > 1
        and str(app.config.get("RATELIMIT_STORAGE_URI", "")).startswith("memory://")
    ):
        app.logger.warning(
            "Flask-Limiter uses in-process memory storage with multiple Gunicorn workers "
            "(%d); configure ESP32CAM_RATELIMIT_STORAGE_URI with a shared backend "
            "such as Redis",
            worker_count,
        )

    if not app.config.get("ADMIN_TOKEN", ""):
        app.logger.warning(
            "ESP32CAM_ADMIN_TOKEN is not set — /metrics and admin API endpoints are "
            "accessible without authentication. Set a strong token in production."
        )

    if not app.config.get("DEVICE_API_KEY", "") and not app.config.get("DEVICE_API_KEYS", {}):
        app.logger.warning(
            "Neither ESP32CAM_DEVICE_API_KEY nor ESP32CAM_DEVICE_API_KEYS is set — "
            "/upload and device endpoints accept requests without authentication. "
            "Set a strong key in production."
        )
    elif (
        app.config.get("DEVICE_API_KEY", "")
        and not app.config.get("DEVICE_API_KEYS", {})
        and not app.config.get("SHARED_KEY_DEVICE_ID", "")
    ):
        app.logger.warning(
            "ESP32CAM_DEVICE_API_KEY is configured without "
            "ESP32CAM_SHARED_KEY_DEVICE_ID — identity-bearing device endpoints "
            "will reject requests. Configure per-device keys for multiple cameras."
        )

    storage_backend = app.config.get("STORAGE_BACKEND", "local")
    if storage_backend != "local":
        raise ValueError(f"Unsupported storage backend: {storage_backend}")

    storage = LocalFilesystemStorage(app.config["UPLOAD_DIR"])
    metadata_store = SQLiteMetadataStore(
        app.config["DB_PATH"],
        default_expected_interval_seconds=app.config["CAMERA_EXPECTED_INTERVAL_SECONDS"],
    )
    # Cheap filename-only walk first, then only stat()/hydrate the files
    # that are genuinely new -- avoids stat()-ing every on-disk file on
    # every boot when most of them are already indexed from a prior run
    # (#383).
    image_names = storage.list_image_names()
    new_filenames = metadata_store.filter_new_filenames([name for name, _ in image_names])
    new_relative_paths = [
        relative_path for name, relative_path in image_names if name in new_filenames
    ]
    metadata_store.sync_storage_index(
        storage.stat_images(new_relative_paths, include_metadata=False),
        hydrate_metadata=storage.read_image_metadata,
    )
    detector = _create_detector(app)
    webhook_url = app.config.get("WEBHOOK_URL", "")
    webhook_min_score = app.config["WEBHOOK_MIN_SCORE"]
    webhook_max_attempts = app.config.get("WEBHOOK_MAX_ATTEMPTS", 3)
    webhook_retry_backoff_seconds = app.config.get("WEBHOOK_RETRY_BACKOFF_SECONDS", 1.0)
    if not (0.0 <= webhook_min_score <= 1.0):
        raise ValueError(
            f"ESP32CAM_WEBHOOK_MIN_SCORE must be between 0.0 and 1.0, got {webhook_min_score}"
        )
    if webhook_max_attempts < 1:
        raise ValueError(
            "ESP32CAM_WEBHOOK_MAX_ATTEMPTS must be at least 1, "
            f"got {webhook_max_attempts}"
        )
    if webhook_retry_backoff_seconds < 0:
        raise ValueError(
            "ESP32CAM_WEBHOOK_RETRY_BACKOFF_SECONDS must not be negative, "
            f"got {webhook_retry_backoff_seconds}"
        )
    notifier = (
        WebhookNotifier(
            webhook_url,
            min_score=webhook_min_score,
            max_attempts=webhook_max_attempts,
            retry_backoff_seconds=webhook_retry_backoff_seconds,
        )
        if webhook_url
        else DISABLED_NOTIFIER
    )
    atexit.register(notifier.close)
    object_tracker = (
        ObjectTracker(
            iou_threshold=app.config["TRACKING_IOU_THRESHOLD"],
            max_missed_frames=app.config["TRACKING_MAX_MISSED_FRAMES"],
            max_tracks_per_camera=app.config["TRACKING_MAX_TRACKS_PER_CAMERA"],
            max_cameras=app.config["TRACKING_MAX_CAMERAS"],
        )
        if app.config["TRACKING_ENABLED"]
        else None
    )
    inference_service = InferenceService(
        metadata_store=metadata_store,
        storage=storage,
        detector=detector,
        mode=app.config["INFERENCE_MODE"],
        notifier=notifier,
        keep_only_detected_snapshots=app.config["KEEP_ONLY_DETECTED_SNAPSHOTS"],
        tracker=object_tracker,
    )
    inference_service.start()
    snapshot_event_publisher = SnapshotEventPublisher()
    storage_retention_policy = StorageRetentionPolicy(
        max_age_seconds=app.config["STORAGE_RETENTION_MAX_AGE_SECONDS"] or None,
        max_bytes=app.config["STORAGE_RETENTION_MAX_BYTES"] or None,
    )
    video_retention_policy = StorageRetentionPolicy(
        max_age_seconds=app.config.get("VIDEO_RETENTION_MAX_AGE_SECONDS") or None,
        max_bytes=app.config.get("VIDEO_RETENTION_MAX_BYTES") or None,
    )
    storage_retention_scan_gate = RetentionScanGate(
        app.config.get("STORAGE_RETENTION_SCAN_INTERVAL_UPLOADS", 1)
    )
    video_retention_scan_gate = RetentionScanGate(
        app.config.get("VIDEO_RETENTION_SCAN_INTERVAL_UPLOADS", 1)
    )
    disk_usage_scan_gate = RetentionScanGate(app.config.get("DISK_USAGE_CHECK_INTERVAL_UPLOADS", 1))
    retention_scheduler = RetentionScheduler(
        app,
        interval_seconds=app.config.get("RETENTION_SCHEDULER_INTERVAL_SECONDS", 3600),
    )
    upload_metrics = UploadMetricsRegistry(
        max_tracked_cameras=app.config.get("MAX_TRACKED_CAMERAS", 500)
    )
    app_metrics = AppMetrics()
    # Thumbnail generation (Pillow decode/resize/encode) is a blocking
    # native call, same class of issue as detector.py's TFLiteCpuDetector
    # -- run it on a small worker pool so it can't stall the gevent hub and
    # freeze every other greenlet (uploads, dashboard, /healthz) for its
    # duration. A handful of workers is enough to keep concurrent thumbnail
    # requests off the request-handling greenlets without oversubscribing
    # the CPU-bound JPEG codec.
    thumbnail_pool = ThreadPool(4)
    atexit.register(thumbnail_pool.kill)
    # In-process cache of generated thumbnail bytes, keyed by source image
    # path + (mtime, size) so a changed/replaced file is never served stale
    # (see script/thumbnail_cache.py, #394). Bounded to avoid unbounded
    # memory growth on deployments with many distinct images.
    thumbnail_cache = ThumbnailCache(
        max_entries=app.config.get("THUMBNAIL_CACHE_MAX_ENTRIES", 256)
    )
    # DELETE /api/cameras/<id>?delete_files=true's shutil.rmtree() of a
    # camera's whole upload directory is the same class of issue as the
    # thumbnail pool above -- a blocking native filesystem call that doesn't
    # yield to gevent, so a camera with years of accumulated snapshots/clips
    # can stall every other greenlet (uploads, dashboard, /healthz) for the
    # whole deletion. A single worker is enough: this is a rare, admin-only
    # action, not a concurrent hot path like thumbnails.
    storage_delete_pool = ThreadPool(1)
    atexit.register(storage_delete_pool.kill)

    app.extensions["storage"] = storage
    app.extensions["metadata_store"] = metadata_store
    app.extensions["detector"] = detector
    app.extensions["inference_service"] = inference_service
    app.extensions["thumbnail_pool"] = thumbnail_pool
    app.extensions["thumbnail_cache"] = thumbnail_cache
    app.extensions["storage_delete_pool"] = storage_delete_pool
    app.extensions["object_tracker"] = object_tracker
    app.extensions["snapshot_event_publisher"] = snapshot_event_publisher
    app.extensions["storage_retention_policy"] = storage_retention_policy
    app.extensions["video_retention_policy"] = video_retention_policy
    app.extensions["storage_retention_scan_gate"] = storage_retention_scan_gate
    app.extensions["video_retention_scan_gate"] = video_retention_scan_gate
    app.extensions["disk_usage_scan_gate"] = disk_usage_scan_gate
    app.extensions["storage_retention_lock"] = threading.RLock()
    app.extensions["retention_scheduler"] = retention_scheduler
    app.extensions["upload_metrics"] = upload_metrics
    app.extensions["app_metrics"] = app_metrics
    limiter.init_app(app)
    app.register_blueprint(server)

    # Flask's test suite creates many application instances; avoid leaking a
    # daemon thread from each test while keeping the scheduler enabled for the
    # production WSGI process.
    if not app.config.get("TESTING", False):
        atexit.register(retention_scheduler.stop)
        if app.config.get("RETENTION_SCHEDULER_AUTO_START", True):
            start_retention_scheduler(app)

    @app.before_request
    def _before_request():
        if not app.config.get("RETENTION_SCHEDULER_AUTO_START", True):
            start_retention_scheduler(app)
        if request.endpoint == "server.upload_file":
            # Read one sentinel byte beyond the configured limit so a chunked
            # request without Content-Length is rejected instead of silently
            # being truncated by Werkzeug's LimitedStream.
            request.max_content_length = app.config["SNAPSHOT_MAX_CONTENT_LENGTH"] + 1
        elif request.endpoint == "server.api_upload_video":
            request.max_content_length = app.config["MAX_VIDEO_CONTENT_LENGTH"] + 1

        metrics = app.extensions.get("app_metrics")
        if metrics is not None:
            g._esp32cam_request_start = metrics.start_request_timer()

    @app.after_request
    def _after_request(response):
        metrics = app.extensions.get("app_metrics")
        start = getattr(g, "_esp32cam_request_start", None)
        endpoint = request.endpoint or "unknown"
        if metrics is not None and start is not None:
            metrics.finish_request(start, request.method, endpoint, response.status_code)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if endpoint == "static":
            # Mirror the long-lived, cacheable Cache-Control used for
            # /files/<filename> — static assets (e.g. base.css) are
            # content-addressed enough (bundled with releases) to be safe
            # to cache aggressively, and this doesn't depend on Flask's
            # SEND_FILE_MAX_AGE_DEFAULT default of "no explicit max-age".
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return _maybe_compress_response(response)

    register_websocket_routes(
        app,
        snapshot_event_publisher,
        max_connections=app.config["MAX_WS_CONNECTIONS"],
        max_pending_events=app.config["MAX_WS_PENDING_EVENTS"],
    )

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_entity_too_large(_error):
        return jsonify({"message": "Payload too large"}), 413

    @app.teardown_appcontext
    def teardown_inference(_exception):
        inference = app.extensions.get("inference_service")
        if inference is not None and app.config.get("INFERENCE_MODE") != "async":
            inference.stop()

    return app
