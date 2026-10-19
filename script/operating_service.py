"""Launch native inference beside Gunicorn under one bounded log collector."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread


def _supervise(command):
    if os.getpgrp() != os.getpid():
        raise RuntimeError("Run supervise through script.log_runner (dedicated process group required)")
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ValueError("Gunicorn command is required")
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    native_env = dict(os.environ)
    extra = native_env.get("ESP32CAM_OPERATING_PYTHONPATH", "")
    if extra:
        native_env["PYTHONPATH"] = extra + os.pathsep + native_env.get("PYTHONPATH", "")
    children = [subprocess.Popen([sys.executable, "-m", "script.operating_service", "worker"], env=native_env),
                subprocess.Popen(command)]
    # Children inherit the collector's process group and stdout/stderr. On
    # failure/exit, log_runner pins this leader and terminates the whole group.
    while not stop.wait(.1):
        if any(child.poll() is not None for child in children):
            print("Operating service child exited; restarting service group", file=sys.stderr, flush=True)
            return 1
    return 0


def _worker():
    from io import BytesIO
    from PIL import Image
    from script.app import create_app
    from script.capture_receiver import serve_live
    from script.config import Config
    from script.onnx_detector import OnnxYoloDetector
    from script.operating_rpc import ControlServer
    from script.operating_runtime import MODEL_SHA256, OperatingRuntime
    from script.validation import is_valid_camera_id

    _configure_native_logging()
    camera = os.getenv("ESP32CAM_OPERATING_CAMERA_ID", "esp32cam-01")
    path = os.getenv("ESP32CAM_OPERATING_SOCKET", "")
    model_path = Path(os.environ["ESP32CAM_OPERATING_MODEL_PATH"])
    if not is_valid_camera_id(camera) or len(camera) > 64 or not path:
        raise ValueError("Operating camera (<=64 characters) and private socket are required")
    digest = hashlib.sha256()
    with model_path.open("rb") as model:
        for chunk in iter(lambda: model.read(512 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != MODEL_SHA256:
        raise ValueError("Operating model does not match the approved FP32 SHA256")
    app = create_app({"TESTING": True, "DETECTOR": "disabled", "INFERENCE_MODE": "sync",
        "DEVICE_LOG_RETENTION_MAX_ROWS": _retention_limit(Config.DEVICE_LOG_RETENTION_MAX_ROWS, 10_000),
        "DEVICE_LOG_RETENTION_MAX_AGE_SECONDS": _retention_limit(Config.DEVICE_LOG_RETENTION_MAX_AGE_SECONDS, 604_800),
        "STORAGE_RETENTION_MAX_BYTES": _retention_limit(Config.STORAGE_RETENTION_MAX_BYTES, 8 * 1024**3),
        "STORAGE_RETENTION_MAX_AGE_SECONDS": _retention_limit(Config.STORAGE_RETENTION_MAX_AGE_SECONDS, 30 * 86400)})
    key = app.config["DEVICE_API_KEYS"].get(camera)
    if not key and app.config["SHARED_KEY_DEVICE_ID"] == camera:
        key = app.config["DEVICE_API_KEY"]
    if not key:
        raise ValueError("A camera-bound device key is required")
    detector = OnnxYoloDetector(model_path, threshold=.5, num_threads=3, input_size=320)
    warmup = BytesIO()
    Image.new("RGB", (320, 240)).save(warmup, format="JPEG", quality=88)
    detector.detect(warmup.getvalue())
    app.extensions["metadata_store"].ensure_camera(camera)  # Once, never a per-frame heartbeat.
    app.extensions["metadata_store"].prune_device_logs()
    runtime = OperatingRuntime(app, detector, camera, key,
                 os.getenv("ESP32CAM_OPERATING_DIAGNOSTIC_DIR", str(Path.home() / ".local/state/esp32cam/diagnostics")))
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        runtime.start()
        with ControlServer(path, runtime.dispatch) as control, serve_live(runtime.receiver,
                os.getenv("ESP32CAM_OPERATING_HOST", "0.0.0.0"),
                int(os.getenv("ESP32CAM_OPERATING_PORT", "8766"))) as live:
            _serve(runtime, control, live, stop)
    finally:
        runtime.close()
    return 0


def _serve(runtime, control, live, stop):
    threads = [Thread(target=control.serve_forever, daemon=True),
               Thread(target=live.serve_forever, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        while not stop.wait(.2):
            if (runtime.receiver.failed or not runtime.monitor.is_alive() or not runtime.maintenance.is_alive()
                    or any(not thread.is_alive() for thread in threads)):
                raise RuntimeError("Operating worker health failed")
    finally:
        runtime.close()
        live.shutdown()
        control.shutdown()
        for thread in threads:
            thread.join(timeout=1)


def _retention_limit(value, limit):
    if type(value) is not int or value < 0:
        raise ValueError("Retention settings must be nonnegative integers")
    return min(value or limit, limit)


def _configure_native_logging():
    # A native worker has no Gunicorn log setup. Keep other modules at their
    # existing levels; only this bounded minute-summary logger needs INFO.
    logger = logging.getLogger("script.operating_runtime")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, "_operating_native", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler._operating_native = True
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    supervisor = subparsers.add_parser("supervise")
    supervisor.add_argument("command", nargs=argparse.REMAINDER)
    subparsers.add_parser("worker")
    args = parser.parse_args()
    return _supervise(args.command) if args.operation == "supervise" else _worker()


if __name__ == "__main__":
    raise SystemExit(main())
