from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

from script.config import Config
from script.detector import DisabledDetector, TFLiteCpuDetector
from script.events import SnapshotEventPublisher
from script.inference import InferenceService
from script.metadata import SQLiteMetadataStore
from script.auth import validate_auth_config
from script.routes import server
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

    validate_auth_config(app)

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
    )
    inference_service.start()
    snapshot_event_publisher = SnapshotEventPublisher()
    storage_retention_policy = StorageRetentionPolicy(
        max_age_seconds=app.config["STORAGE_RETENTION_MAX_AGE_SECONDS"] or None,
        max_bytes=app.config["STORAGE_RETENTION_MAX_BYTES"] or None,
    )
    upload_metrics = UploadMetricsRegistry()

    app.extensions["storage"] = storage
    app.extensions["metadata_store"] = metadata_store
    app.extensions["detector"] = detector
    app.extensions["inference_service"] = inference_service
    app.extensions["snapshot_event_publisher"] = snapshot_event_publisher
    app.extensions["storage_retention_policy"] = storage_retention_policy
    app.extensions["upload_metrics"] = upload_metrics
    app.register_blueprint(server)
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
