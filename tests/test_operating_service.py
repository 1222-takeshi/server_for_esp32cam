import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest

from conftest import MINIMAL_JPEG
from script.detector import Detection
from script.frame_protocol import CameraFrame
from script.operating_runtime import MODEL_SHA256, OperatingRuntime
from script.operating_service import _retention_limit, _serve
from script.storage import StorageRetentionPolicy

CAT = Detection("cat", .9, .1, .2, .6, .8)


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def normal_sample(*_):
    return {"free": 5 * 1024**3, "total": 10 * 1024**3,
            "db": 0, "wal": 0, "write_bytes": 0}


@pytest.fixture
def runtime(app, tmp_path):
    app.config["KEEP_ONLY_DETECTED_SNAPSHOTS"] = True
    clock = Clock()
    rt = OperatingRuntime(app, object(), "cam", "private", tmp_path / "diagnostics",
                          clock=clock, sampler=normal_sample, hardware=lambda: (45, 0))
    rt.maintain()
    rt.sample()
    rt.mode.hello(7, 0)
    assert rt.mode.acknowledge(7, rt.mode.version, "eco", 1)
    yield rt
    rt.close()


def publish(rt, sequence, detections=(CAT,)):
    stamp = int(rt.clock() * 1_000_000)
    frame = CameraFrame(7, sequence, stamp - 2000, stamp - 1000, MINIMAL_JPEG, rt.mode.version)
    event = {"status": "success", "boot_id": 7, "mode_generation": frame.mode_generation,
             "sequence": sequence, "host_received_us": stamp, "infer_finished_us": stamp + 1000,
             "age_low_us": 1000, "age_high_us": 3000, "detections": [asdict(d) for d in detections],
             "counters": {"frames": sequence, "fresh_success": sequence}}

    def result():
        rt._event(event)
        rt._result(frame, event)

    assert rt.mode.publish_result(frame, result)


def wait_for(predicate):
    end = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < end
        time.sleep(.005)


def advance(rt, seconds):
    for _ in range(seconds):
        rt.clock.now += 1
        rt.sample()


def test_live_ram_representative_archive_and_atomic_completed_metadata(runtime, monkeypatch):
    rt = runtime
    notifications = []
    monkeypatch.setattr(rt.app.extensions["webhook_notifier"], "notify", lambda *args: notifications.append(args))
    publish(rt, 1)
    wait_for(lambda: rt.writer.status()["saved"] == 1)
    advance(rt, 5)
    publish(rt, 2)
    advance(rt, 5)
    publish(rt, 3)
    wait_for(lambda: rt.writer.status()["saved"] == 2)
    assert len(notifications) == 2
    storage, store = rt.app.extensions["storage"], rt.app.extensions["metadata_store"]
    images = storage.list_images()
    assert len(images) == 2 and rt.writer.status()["interval"] == 1
    for image in images:
        snapshot = store.get_snapshot(store.get_snapshot_by_filename(image.name).id)
        assert snapshot.inference_status == "succeeded" and len(snapshot.detections) == 1
        metadata = json.loads((storage.upload_dir / image.relative_path).with_suffix(".jpg.json").read_text())
        assert metadata["operating_frame"]["model_sha256"] == MODEL_SHA256
        assert metadata["operating_frame"]["generation"] == rt.mode.version
        assert metadata["operating_frame"]["capture_time_estimated"] is True
    assert not list(store.iter_pending_snapshot_ids())
    assert rt.dispatch({"camera_id": "cam", "op": "frame"}) == MINIMAL_JPEG
    status = rt.status()
    assert sum(status["age_histogram_ms"]) == 3
    assert status["diagnostic"]["state"] == "off"
    assert status["actuation_state"] == "hold"


def test_no_detection_no_disk_and_old_generation_preview_save_rejected(runtime):
    rt = runtime
    publish(rt, 1, ())
    assert not rt.app.extensions["storage"].list_images()
    assert rt.writer.status()["undetected"] == 1
    old = (7, rt.mode.version, 1, int(rt.clock() * 1_000_000))
    result = rt.dispatch({"camera_id": "cam", "op": "mode", "mode": "tracking",
                          "expected_version": rt.mode.version})
    assert result["requested_mode"] == "tracking" and result["state"] == "pending"
    assert rt.dispatch({"camera_id": "cam", "op": "frame"}) == b""
    assert rt._save("cam", MINIMAL_JPEG, (CAT,), old) is False
    assert not rt.app.extensions["storage"].list_images()


def test_archive_sql_failure_rolls_back_camera_snapshot_detections_and_own_file(runtime):
    rt = runtime
    store = rt.app.extensions["metadata_store"]
    store._get_connection().execute("""CREATE TRIGGER reject_detection BEFORE INSERT ON detections
                                  BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    publish(rt, 1)
    wait_for(lambda: rt.writer.status()["errors"] == 1)
    assert store.total_snapshot_count() == 0
    assert store.get_camera("cam") is None
    assert not rt.app.extensions["storage"].list_images()
    assert rt.guard.status()["paused"]
    assert rt.guard.status()["reserved_bytes_hour"] > 0


def test_optional_notification_failure_does_not_break_recognition_or_archive(runtime, monkeypatch):
    def fail(*_):
        raise RuntimeError("notification unavailable")
    monkeypatch.setattr(runtime.app.extensions["webhook_notifier"], "notify", fail)
    publish(runtime, 1)
    wait_for(lambda: runtime.writer.status()["saved"] == 1)
    assert runtime.notification_errors == 1
    assert not runtime.guard.status()["paused"]
    assert runtime.dispatch({"camera_id": "cam", "op": "frame"}) == MINIMAL_JPEG


def test_thermal_fault_and_manual_recovery_never_auto_tracking(runtime):
    rt = runtime
    rt.mode.request("tracking", rt.mode.version, ready=True)
    rt.hardware = lambda: (80, 0)
    rt.sample()
    status = rt.status()
    assert status["resources"]["reason"] == "thermal_fault"
    assert status["requested_mode"] == "eco" and status["actuation_state"] == "hold"
    assert rt.dispatch({"camera_id": "cam", "op": "resume", "expected_version": rt.mode.version})["status_code"] == 409
    rt.hardware = lambda: (45, 0)
    advance(rt, 61)
    assert rt.guard.status()["paused"] and rt.guard.status()["resume_ready"]
    result = rt.dispatch({"camera_id": "cam", "op": "resume", "expected_version": rt.mode.version})
    assert result["requested_mode"] == "eco" and result["state"] == "pending"
    assert not result["resources"]["paused"]


def test_monitor_stale_protects_from_cached_health_check(runtime):
    rt = runtime
    rt.mode.request("tracking", rt.mode.version, ready=True)
    rt.clock.now += 3.001
    rt.receiver.health_check()
    assert rt.mode.status()["requested_mode"] == "eco"
    assert rt.mode.status()["actuation_state"] == "hold"
    assert rt.guard.status()["reason"] == "monitor_stale"


def test_diagnostic_default_off_ttl_bounds_and_nonblocking_resource_stop(runtime, monkeypatch):
    rt = runtime
    assert not rt.diagnostic_dir.exists()
    for invalid in (0, True, 601):
        assert rt.dispatch({"camera_id": "cam", "op": "diagnostic.start", "duration_seconds": invalid})["status_code"] == 400
    started = rt.dispatch({"camera_id": "cam", "op": "diagnostic.start", "duration_seconds": 600})
    assert started["diagnostic"]["state"] == "running"
    entered, release = Event(), Event()
    original = rt.diagnostic._budget.write

    def blocked(payload):
        entered.set()
        assert release.wait(3)
        original(payload)

    monkeypatch.setattr(rt.diagnostic._budget, "write", blocked)
    try:
        publish(rt, 1)
        assert entered.wait(2)
        rt.sampler = lambda *_: {**normal_sample(), "free": 0}
        began = time.monotonic()
        rt.sample()
        assert time.monotonic() - began < .2
        assert rt.diagnostic.status()["state"] == "closed"
        assert rt.guard.status()["paused"]
    finally:
        release.set()


@pytest.mark.parametrize("payload,code", [
    ({"camera_id": "other", "op": "status"}, 404),
    ({"camera_id": "cam", "op": []}, 400),
    ({"camera_id": "cam", "op": "mode", "mode": "tracking", "expected_version": True}, 400),
    ({"camera_id": "cam", "op": "status", "extra": True}, 400),
])
def test_control_invalid_requests(runtime, payload, code):
    assert runtime.dispatch(payload)["status_code"] == code


def test_supervisor_runs_worker_module_and_collector_cleans_failed_group(tmp_path):
    if not hasattr(os, "waitid"):
        pytest.skip("Collector needs POSIX waitid")
    env = dict(os.environ)
    env.pop("ESP32CAM_OPERATING_MODEL_PATH", None)
    result = subprocess.run([sys.executable, "-m", "script.log_runner", "--directory", str(tmp_path / "logs"),
            "--", sys.executable, "-m", "script.operating_service", "supervise", "--",
            sys.executable, "-c", "import time; time.sleep(30)"],
            env=env, capture_output=True, timeout=12)
    assert result.returncode != 0
    log = (tmp_path / "logs/runtime.log").read_text()
    assert "ESP32CAM_OPERATING_MODEL_PATH" in log
    assert "No module named __main__" not in log


def test_service_help_does_not_load_optional_onnx_runtime():
    result = subprocess.run([sys.executable, "-m", "script.operating_service", "--help"],
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0 and "supervise" in result.stdout


@pytest.mark.parametrize("failed_listener", ["control", "live"])
def test_listener_thread_failure_stops_worker_for_supervised_restart(failed_listener):
    released, closed = Event(), Event()

    class Listener:
        def __init__(self, name):
            self.name = name
            self.stopped = False

        def serve_forever(self):
            if self.name != failed_listener:
                released.wait(2)

        def shutdown(self):
            self.stopped = True
            released.set()

    runtime = SimpleNamespace(receiver=SimpleNamespace(failed=False),
                              monitor=SimpleNamespace(is_alive=lambda: True),
                              maintenance=SimpleNamespace(is_alive=lambda: True), close=closed.set)
    control, live = Listener("control"), Listener("live")
    began = time.monotonic()
    with pytest.raises(RuntimeError, match="health"):
        _serve(runtime, control, live, Event())
    assert time.monotonic() - began < 1
    assert closed.is_set() and control.stopped and live.stopped


def test_periodic_retention_removes_expired_images_without_new_recognition(runtime, tmp_path):
    rt = runtime
    rt.app.extensions["storage_retention_policy"] = StorageRetentionPolicy(max_age_seconds=1)
    storage, store = rt.app.extensions["storage"], rt.app.extensions["metadata_store"]
    raw = tmp_path / "protected-baseline.jpg"
    raw.write_bytes(b"immutable")

    def add_expired():
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        stored = storage.save_image(MINIMAL_JPEG, "image/jpeg", device_id="cam", captured_at=old)
        store.record_completed_snapshot(replace(stored, created_at=old), (CAT,))

    add_expired()
    rt.retention_interval_seconds = 1
    rt.start()
    wait_for(lambda: store.total_snapshot_count() == 0)
    add_expired()  # Next scheduled scan must run even without another saved frame.
    wait_for(lambda: store.total_snapshot_count() == 0)
    assert not storage.list_images() and rt.writer.status()["saved"] == 0
    assert raw.read_bytes() == b"immutable"


@pytest.mark.parametrize("value", [-1, True, "0", None])
def test_invalid_retention_config_cannot_disable_native_quotas(value):
    with pytest.raises(ValueError):
        _retention_limit(value, 100)


def test_retention_config_honors_stricter_values_and_caps_defaults():
    assert _retention_limit(0, 100) == 100
    assert _retention_limit(200, 100) == 100
    assert _retention_limit(1, 100) == 1


def test_retention_failure_latches_until_success_then_requires_stable_recovery(runtime, monkeypatch):
    rt = runtime
    from script import operating_runtime
    original = operating_runtime.apply_scheduled_retention

    def fail():
        raise OSError("retention unavailable")

    monkeypatch.setattr(operating_runtime, "apply_scheduled_retention", fail)
    with pytest.raises(OSError):
        rt.maintain()
    advance(rt, 61)
    assert rt.retention_ok is False and not rt.guard.status()["resume_ready"]
    assert rt.dispatch({"camera_id": "cam", "op": "resume", "expected_version": rt.mode.version})["status_code"] == 409
    assert rt._save("cam", MINIMAL_JPEG, (CAT,), (7, rt.mode.version, 1, 0)) is False
    monkeypatch.setattr(operating_runtime, "apply_scheduled_retention", original)
    rt.maintain()
    assert rt.dispatch({"camera_id": "cam", "op": "resume", "expected_version": rt.mode.version})["status_code"] == 409
    advance(rt, 61)
    result = rt.dispatch({"camera_id": "cam", "op": "resume", "expected_version": rt.mode.version})
    assert result["retention_ok"] is True and not result["resources"]["paused"]
    assert result["requested_mode"] == "eco" and result["actuation_state"] == "hold"


def test_native_minute_logger_is_visible_once_without_enabling_other_info_logs():
    source = """import logging
from script.operating_service import _configure_native_logging
logging.basicConfig(level=logging.WARNING)
_configure_native_logging()
_configure_native_logging()
logging.getLogger('script.operating_runtime').info('operating_summary test')
logging.getLogger('script.capture_receiver').info('unrelated per-frame message')
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert result.stderr.count("operating_summary test") == 1
    assert "per-frame" not in result.stderr and not result.stdout
