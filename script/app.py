from __future__ import annotations

import atexit
from datetime import datetime
import threading
from zoneinfo import ZoneInfo

from flask import Flask, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

from flask import g, request

from script.config import Config, gunicorn_worker_count
from script.detector import DisabledDetector, TFLiteCpuDetector
from script.events import SnapshotEventPublisher
from script.inference import InferenceService
from script.logging_config import configure_logging
from script.metadata import SQLiteMetadataStore
from script.auth import validate_auth_config
from script.prometheus_metrics import AppMetrics
from script.retention import RetentionScanGate, RetentionScheduler
from script.routes import limiter, server
from script.storage import LocalFilesystemStorage, StorageRetentionPolicy
from script.tracking import ObjectTracker
from script.upload_metrics import UploadMetricsRegistry
from script.webhook import DISABLED_NOTIFIER, WebhookNotifier
from script.websocket_routes import register_websocket_routes


JST = ZoneInfo("Asia/Tokyo")


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
        )
    except Exception as exc:  # pragma: no cover - depends on optional deps and model file
        return DisabledDetector(str(exc), skip=False)


def start_retention_scheduler(app: Flask) -> bool:
    """Start retention in the current process when it is not a test app."""

    if app.config.get("TESTING", False):
        return False
    scheduler = app.extensions.get("retention_scheduler")
    if scheduler is None:
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
    elif app.config.get("DEVICE_API_KEY", "") and not app.config.get("DEVICE_API_KEYS", {}):
        if not app.config.get("SHARED_KEY_DEVICE_ID", ""):
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
    metadata_store.sync_storage_index(
        storage.list_images(include_metadata=False),
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

    app.extensions["storage"] = storage
    app.extensions["metadata_store"] = metadata_store
    app.extensions["detector"] = detector
    app.extensions["inference_service"] = inference_service
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
        return response

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
