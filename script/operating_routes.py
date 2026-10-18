"""Small gevent-compatible HTTP clients for the private native worker."""

from flask import Blueprint, Response, current_app, jsonify, request

from script.auth import check_admin_token
from script.operating_rpc import call, call_json

operating = Blueprint("operating", __name__)


def enabled(camera):
    return bool(current_app.config.get("OPERATING_SOCKET")) and camera == current_app.config["OPERATING_CAMERA_ID"]


def status(camera):
    try:
        result = call_json(current_app.config["OPERATING_SOCKET"], {"camera_id": camera, "op": "status"})
        if result.get("state") not in {"unknown", "pending", "active", "degraded"}:
            raise ValueError("Invalid native status")
        return result
    except (OSError, ValueError, TypeError):
        return {"state": "unknown", "effective_mode": None, "requested_mode": None,
                "actuation_state": "hold", "connected": False, "message": "認識サービスに接続できません"}


@operating.before_request
def _bounds():
    request.max_content_length = 1025  # Sentinel for chunked bodies (see AGENTS.md).
    if not enabled(request.view_args["camera_id"]):
        return jsonify(message="このカメラは運転モードに未対応です"), 404


@operating.after_request
def _no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@operating.route("/api/cameras/<camera_id>/operating", methods=["GET"])
def get_status(camera_id):
    result = status(camera_id)
    return jsonify(result), 503 if "message" in result else 200


@operating.route("/api/cameras/<camera_id>/operating/frame")
def frame(camera_id):
    try:
        data = call(current_app.config["OPERATING_SOCKET"], {"camera_id": camera_id, "op": "frame"})
        if data and not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
            raise ValueError("Invalid native JPEG")
        return Response(data, status=200 if data else 204, mimetype="image/jpeg")
    except (OSError, ValueError, TypeError):
        return jsonify(message="最新画像を取得できません"), 503


@operating.route("/api/cameras/<camera_id>/operating", methods=["PUT"])
@operating.route("/api/cameras/<camera_id>/operating/resume", methods=["POST"])
@operating.route("/api/cameras/<camera_id>/operating/diagnostic", methods=["POST", "DELETE"])
def mutate(camera_id):
    if not current_app.config.get("ADMIN_TOKEN"):
        return jsonify(message="管理者トークンの設定が必要です"), 503
    auth = request.headers.get("Authorization", "")
    if not check_admin_token(auth[7:] if auth.startswith("Bearer ") else None):
        return jsonify(message="Unauthorized"), 401, {"WWW-Authenticate": 'Bearer realm="ESP32CAM Admin"'}
    data = request.get_data()
    if len(data) > 1024:
        return jsonify(message="Request body too large"), 413
    payload = request.get_json(silent=True)
    op = ("resume" if request.path.endswith("/resume") else
          "diagnostic.stop" if request.method == "DELETE" else
          "diagnostic.start" if request.path.endswith("/diagnostic") else "mode")
    fields = {"mode": {"mode", "expected_version"}, "resume": {"expected_version"},
              "diagnostic.start": {"duration_seconds"}, "diagnostic.stop": set()}[op]
    if type(payload) is not dict or set(payload) != fields:
        return jsonify(message="Invalid operation fields"), 400
    try:
        result = call_json(current_app.config["OPERATING_SOCKET"], {**payload, "camera_id": camera_id, "op": op})
        code = result.pop("status_code", 200)
        return jsonify(result), code
    except (OSError, ValueError, TypeError):
        return jsonify(state="unknown", effective_mode=None, actuation_state="hold",
                       message="認識サービスに接続できません"), 503
