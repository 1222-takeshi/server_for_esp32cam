from __future__ import annotations

from flask import Flask, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

from script.config import Config
from script.routes import server
from script.storage import LocalFilesystemStorage


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder=str(Config.BASE_DIR / "templates"))
    app.config.from_object(Config)

    if test_config:
        app.config.update(test_config)

    storage_backend = app.config.get("STORAGE_BACKEND", "local")
    if storage_backend != "local":
        raise ValueError(f"Unsupported storage backend: {storage_backend}")

    app.extensions["storage"] = LocalFilesystemStorage(app.config["UPLOAD_DIR"])
    app.register_blueprint(server)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_entity_too_large(_error):
        return jsonify({"message": "Payload too large"}), 413

    return app
