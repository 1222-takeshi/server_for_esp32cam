from __future__ import annotations

from flask import Flask, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

from script.config import Config
from script.detector import DisabledDetector, TFLiteCpuDetector
from script.inference import InferenceService
from script.metadata import SQLiteMetadataStore
from script.routes import server
from script.storage import LocalFilesystemStorage


def _create_detector(app: Flask):
    detector_kind = app.config.get("DETECTOR", "disabled")
    if detector_kind == "disabled":
        return DisabledDetector("Detector is disabled")
    if detector_kind != "tflite_cpu":
        return DisabledDetector(f"Unsupported detector: {detector_kind}")

    model_path = app.config.get("MODEL_PATH", "")
    if not model_path:
        return DisabledDetector("TFLite model path is not configured")

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
        return DisabledDetector(str(exc))


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder=str(Config.BASE_DIR / "templates"))
    app.config.from_object(Config)

    if test_config:
        app.config.update(test_config)

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
    inference_service = InferenceService(
        metadata_store=metadata_store,
        storage=storage,
        detector=detector,
        mode=app.config["INFERENCE_MODE"],
    )
    inference_service.start()

    app.extensions["storage"] = storage
    app.extensions["metadata_store"] = metadata_store
    app.extensions["detector"] = detector
    app.extensions["inference_service"] = inference_service
    app.register_blueprint(server)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_entity_too_large(_error):
        return jsonify({"message": "Payload too large"}), 413

    @app.teardown_appcontext
    def teardown_inference(_exception):
        inference = app.extensions.get("inference_service")
        if inference is not None and app.config.get("INFERENCE_MODE") != "async":
            inference.stop()

    return app
