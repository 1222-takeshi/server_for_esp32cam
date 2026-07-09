from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

from flask import g, request

from script.config import Config
from script.detector import DisabledDetector, TFLiteCpuDetector
from script.events import SnapshotEventPublisher
from script.inference import InferenceService
from script.logging_config import configure_logging
from script.metadata import SQLiteMetadataStore
from script.auth import validate_auth_config
from script.prometheus_metrics import AppMetrics
from script.retention import RetentionScanGate
from script.routes import limiter, server
from script.storage import LocalFilesystemStorage, StorageRetentionPolicy
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


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder=str(Config.BASE_DIR / "templates"))
    app.config.from_object(Config)

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

    storage_backend = app.config.get("STORAGE_BACKEND", "local")
    if storage_backend != "local":
        raise ValueError(f"Unsupported storage backend: {storage_backend}")

    storage = LocalFilesystemStorage(app.config["UPLOAD_DIR"])
    metadata_store = SQLiteMetadataStore(
        app.config["DB_PATH"],
        default_expected_interval_seconds=app.config["CAMERA_EXPECTED_INTERVAL_SECONDS"],
    )
    metadata_store.sync_storage_index(storage.list_images())
    detector = _create_detector(app)
    webhook_url = app.config.get("WEBHOOK_URL", "")
    webhook_min_score = app.config["WEBHOOK_MIN_SCORE"]
    if not (0.0 <= webhook_min_score <= 1.0):
        raise ValueError(
            f"ESP32CAM_WEBHOOK_MIN_SCORE must be between 0.0 and 1.0, got {webhook_min_score}"
        )
    notifier = (
        WebhookNotifier(webhook_url, min_score=webhook_min_score)
        if webhook_url
        else DISABLED_NOTIFIER
    )
    inference_service = InferenceService(
        metadata_store=metadata_store,
        storage=storage,
        detector=detector,
        mode=app.config["INFERENCE_MODE"],
        notifier=notifier,
        keep_only_detected_snapshots=app.config["KEEP_ONLY_DETECTED_SNAPSHOTS"],
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
    upload_metrics = UploadMetricsRegistry()
    app_metrics = AppMetrics()

    app.extensions["storage"] = storage
    app.extensions["metadata_store"] = metadata_store
    app.extensions["detector"] = detector
    app.extensions["inference_service"] = inference_service
    app.extensions["snapshot_event_publisher"] = snapshot_event_publisher
    app.extensions["storage_retention_policy"] = storage_retention_policy
    app.extensions["video_retention_policy"] = video_retention_policy
    app.extensions["storage_retention_scan_gate"] = storage_retention_scan_gate
    app.extensions["video_retention_scan_gate"] = video_retention_scan_gate
    app.extensions["upload_metrics"] = upload_metrics
    app.extensions["app_metrics"] = app_metrics
    limiter.init_app(app)
    app.register_blueprint(server)

    @app.before_request
    def _before_request():
        metrics = app.extensions.get("app_metrics")
        if metrics is not None:
            g._esp32cam_request_start = metrics.start_request_timer()

    @app.after_request
    def _after_request(response):
        metrics = app.extensions.get("app_metrics")
        start = getattr(g, "_esp32cam_request_start", None)
        if metrics is not None and start is not None:
            endpoint = request.endpoint or "unknown"
            metrics.finish_request(start, request.method, endpoint, response.status_code)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response
    register_websocket_routes(
        app, snapshot_event_publisher, max_connections=app.config["MAX_WS_CONNECTIONS"]
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
