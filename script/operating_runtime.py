"""Native Raspberry Pi recognition with RAM preview and bounded optional writes."""

from __future__ import annotations

import json
import logging
import math
import os
import resource
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import uuid4

from script.capture_receiver import create_receiver
from script.detector import Detection
from script.diagnostic_logs import DiagnosticLog
from script.operating_mode import ModeError, OperatingMode
from script.representative_writer import RepresentativeWriter, ResourceGuard, sample_resources
from script.retention import apply_scheduled_retention, apply_upload_retention
from script.tracking import ObjectTracker

MODEL_SHA256 = "ff1013da8734fe2b8ede5f34a2124855d447fbefd248d148e9f29e7f8e28c9c4"
logger = logging.getLogger(__name__)


class HardwareProbe:
    def __init__(self):
        self.last = -float("inf")
        self.throttled = None

    def __call__(self):
        temperature = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        if time.monotonic() - self.last >= 10:
            result = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                    text=True, check=True, timeout=.5)
            self.throttled = int(result.stdout.strip().split("=", 1)[1], 16)
            self.last = time.monotonic()
        return temperature, self.throttled


class OperatingRuntime:
    def __init__(self, app, detector, camera_id, device_key, diagnostic_dir, *,
                 clock=time.monotonic, sampler=sample_resources, hardware=None):
        self.app, self.camera_id, self.clock = app, camera_id, clock
        self.sampler, self.hardware = sampler, hardware or HardwareProbe()
        self.diagnostic_dir = Path(diagnostic_dir)
        self.mode = OperatingMode(clock=clock)
        self.guard = ResourceGuard(clock=clock)
        self.ram_lock, self.diagnostic_lock = Lock(), Lock()
        self.latest = None
        self.counters = {}
        self.ages = deque(maxlen=512)
        self.age_histogram = [0] * 501
        self.metric_generation, self.last_success, self.max_gap_ms = None, None, 0
        self.closed = False
        self.vitals = {"temperature_c": None, "throttled": None, "cpu_percent": 0,
                       "rss_bytes": 0, "thermal_ok": False}
        self.notification_errors = 0
        self.diagnostic = None
        self._tracker, self._tracking_generation = None, None
        self.stop = Event()
        self.monitor = None
        self.maintenance = None
        self.retention_ok = None
        self.retention_interval_seconds = 3600
        self.writer = RepresentativeWriter(self._save, self.guard, clock=clock,
                    keep_only_detected=app.config["KEEP_ONLY_DETECTED_SNAPSHOTS"])
        self.receiver = create_receiver(camera_id=camera_id, device_api_key=device_key,
                    detector=detector, event_sink=self._event, result_sink=self._result,
                    profile=self.mode, continuous=True, health_check=self._protect,
                    inference_reserve_ms=165,
                    tracking_ping_interval_ms=50,
                    clock_us=lambda: int(clock() * 1_000_000))

    def _event(self, event):
        with self.ram_lock:
            if "counters" in event:
                self.counters = dict(event["counters"])
            if event["status"] == "success":
                age = (event["age_high_us"] + event["infer_finished_us"] - event["host_received_us"]) / 1000
                generation = (event["boot_id"], event["mode_generation"])
                if generation != self.metric_generation:
                    self.age_histogram = [0] * 501
                    self.ages.clear()
                    self.last_success, self.max_gap_ms, self.metric_generation = None, 0, generation
                self.ages.append(age)
                self.age_histogram[min(500, max(0, math.ceil(age)))] += 1
                if self.last_success is not None:
                    self.max_gap_ms = max(self.max_gap_ms, (event["infer_finished_us"] - self.last_success) / 1000)
                self.last_success = event["infer_finished_us"]
        diagnostic = self.diagnostic
        if diagnostic is not None and not self.guard.status()["paused"]:
            diagnostic.submit(event)

    def _result(self, frame, event):
        detections = tuple(Detection(**item) for item in event["detections"])
        if self.app.config["TRACKING_ENABLED"]:
            if self._tracking_generation != frame.mode_generation:
                self._tracker = ObjectTracker(
                    iou_threshold=self.app.config["TRACKING_IOU_THRESHOLD"],
                    max_missed_frames=self.app.config["TRACKING_MAX_MISSED_FRAMES"],
                    max_tracks_per_camera=self.app.config["TRACKING_MAX_TRACKS_PER_CAMERA"],
                    max_cameras=1)
                self._tracking_generation = frame.mode_generation
            detections = tuple(self._tracker.update(self.camera_id, list(detections)))
        captured_host_us = event["host_received_us"] - (event["age_low_us"] + event["age_high_us"]) // 2
        with self.ram_lock:
            self.latest = (frame.boot_id, frame.mode_generation, frame.sequence, frame.jpeg, self.clock())
        self.writer.submit(self.camera_id, frame.jpeg, detections,
                           (frame.boot_id, frame.mode_generation, frame.sequence, captured_host_us))

    def _save(self, camera, jpeg, detections, identity):
        if self.retention_ok is not True:
            return False
        with self.mode.lock:
            if (not self.mode.connected or self.mode.ack is None
                    or identity[:2] != (self.mode.boot, self.mode.version)):
                return False
        # Never keep the mode lock across filesystem, SQLite, retention or HTTP.
        storage, store = self.app.extensions["storage"], self.app.extensions["metadata_store"]
        captured = datetime.now(timezone.utc) - timedelta(microseconds=max(0, int(self.clock() * 1_000_000) - identity[3]))
        stored = storage.save_image(jpeg, "image/jpeg", device_id=camera, captured_at=captured)
        try:
            sidecar = (storage.upload_dir / stored.relative_path).with_suffix(".jpg.json")
            payload = json.loads(sidecar.read_text())
            payload["operating_frame"] = {"boot_id": identity[0], "generation": identity[1],
                "sequence": identity[2], "capture_time_estimated": True, "model_sha256": MODEL_SHA256}
            temporary = sidecar.with_name(f".{sidecar.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(payload))
                os.replace(temporary, sidecar)
            finally:
                temporary.unlink(missing_ok=True)
            snapshot = store.record_completed_snapshot(stored, detections)
        except Exception:
            storage.delete_image_by_relative_path(stored.relative_path)
            raise
        with self.app.app_context():
            apply_upload_retention(stored)
        try:
            self.app.extensions["webhook_notifier"].notify(snapshot, list(detections))
        except Exception:
            self.notification_errors += 1
        return True

    def sample(self):
        try:
            self.guard.observe(self.sampler(self.app.config["UPLOAD_DIR"], self.app.config["DB_PATH"]))
            temperature, throttled = self.hardware()
            thermal_ok = 0 < temperature < 80 and throttled == 0
            self.vitals.update(temperature_c=temperature, throttled=throttled, thermal_ok=thermal_ok)
            if not thermal_ok:
                self.guard.pause("thermal_fault")
            if self.retention_ok is False:
                self.guard.monitor_failed()
            self._protect()  # Update the recovery gate before tick can clear an eco latch.
            mode = self.mode.status()
            if (mode["reason"] in {"frames_expired", "cadence_low", "publication_failed", "ack_timeout"}
                    and not mode["eco_auto_recovery_pending"] and not self.guard.status()["paused"]):
                self.guard.pause("pipeline_fault")
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            self.guard.monitor_failed()
        self._protect()

    def _protect(self):
        guard = self.guard.status()
        with self.mode.lock:
            self.mode.eco_recovery_ready = (not guard["paused"] and self.vitals["thermal_ok"]
                                            and self.retention_ok is True)
            if guard["paused"] and (self.mode.reason is None or self.mode.requested == "tracking"
                                     or self.mode.eco_recoverable):
                self.mode.protect(f"resource_{guard['reason']}")
        if guard["paused"] and self.diagnostic is not None and self.diagnostic.status()["state"] == "running":
            self.diagnostic.close(wait=False)

    def start(self):
        self.sample()

        def monitor():
            last_cpu = resource.getrusage(resource.RUSAGE_SELF)
            last_time, next_log = self.clock(), self.clock() + 60
            while not self.stop.wait(1):
                self.sample()
                now, cpu = self.clock(), resource.getrusage(resource.RUSAGE_SELF)
                self.vitals["cpu_percent"] = 100 * (cpu.ru_utime + cpu.ru_stime - last_cpu.ru_utime - last_cpu.ru_stime) / max(.001, now - last_time)
                try:
                    self.vitals["rss_bytes"] = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
                except (OSError, ValueError, IndexError):
                    self.guard.monitor_failed()
                    self._protect()
                last_cpu, last_time = cpu, now
                if now >= next_log:
                    summary = self.status()
                    summary.pop("age_histogram_ms")  # Full histogram remains in bounded RAM/API.
                    logger.info("operating_summary %s", json.dumps(summary, separators=(",", ":")))
                    next_log = now + 60
        self.monitor = Thread(target=monitor, daemon=True)
        self.monitor.start()
        self.maintenance = Thread(target=self._maintenance_loop, daemon=True)
        self.maintenance.start()

    def maintain(self):
        try:
            with self.app.app_context():
                apply_scheduled_retention()
        except Exception:
            self.retention_ok = False
            self.guard.monitor_failed()
            self._protect()
            raise
        self.retention_ok = True

    def _maintenance_loop(self):
        while not self.stop.is_set():
            try:
                self.maintain()
            except Exception:
                logger.error("operating_retention_failed")
            if self.stop.wait(self.retention_interval_seconds if self.retention_ok else 30):
                break

    def status(self):
        self._protect()
        with self.mode.lock:
            mode = self.mode.status()
            with self.ram_lock:
                counts, ages = dict(self.counters), sorted(self.ages)
                histogram, generation, gap = list(self.age_histogram), self.metric_generation, self.max_gap_ms
        return {**mode, "camera_id": self.camera_id, "native_pid": os.getpid(),
                "model_sha256": MODEL_SHA256, "input_size": 320, "threshold": .5, "threads": 3,
                "resources": self.guard.status(), "writer": self.writer.status(),
                "retention_ok": self.retention_ok,
                "webhook": self.app.extensions["webhook_notifier"].budget_status(),
                "diagnostic": self.diagnostic.status() if self.diagnostic else {"state": "off"},
                "counters": counts, "vitals": dict(self.vitals), "notification_errors": self.notification_errors,
                "metric_generation": generation, "age_histogram_ms": histogram, "max_gap_ms": gap,
                "age_p95_ms": ages[int((len(ages) - 1) * .95)] if ages else None}

    def dispatch(self, request):
        try:
            if request.get("camera_id") != self.camera_id:
                raise ModeError("Camera does not support operating profiles", 404)
            op = request.get("op")
            keys = {"status": set(), "frame": set(), "mode": {"mode", "expected_version"},
                    "resume": {"expected_version"}, "diagnostic.start": {"duration_seconds"}, "diagnostic.stop": set()}
            if type(op) is not str or op not in keys or set(request) != keys[op] | {"camera_id", "op"}:
                raise ModeError("Invalid control operation")
            if op == "frame":
                with self.mode.lock, self.ram_lock:
                    latest = self.latest
                    return latest[3] if (latest and self.mode.connected and self.mode.ack is not None
                        and latest[:2] == (self.mode.boot, self.mode.version)
                        and self.clock() - latest[4] <= (.5 if self.mode.requested == "tracking" else 7)) else b""
            ready = (self.retention_ok is True and not self.guard.status()["paused"]
                     and self.vitals["thermal_ok"])
            if op == "mode":
                self.mode.request(request["mode"], request["expected_version"], ready=ready)
            elif op == "resume":
                with self.mode.lock:
                    if type(request["expected_version"]) is not int or request["expected_version"] != self.mode.version:
                        raise ModeError("Mode version conflict", 409)
                    if self.retention_ok is not True or not self.vitals["thermal_ok"] or not self.guard.resume():
                        raise ModeError("Normal resources must remain stable for 60 seconds", 409)
                    self.mode.request("eco", request["expected_version"], ready=True)
            elif op.startswith("diagnostic."):
                duration = request.get("duration_seconds")
                if op == "diagnostic.start" and (type(duration) is not int or not 1 <= duration <= 600):
                    raise ModeError("Diagnostic duration must be 1..600 seconds")
                with self.diagnostic_lock:
                    if op == "diagnostic.start":
                        if not ready:
                            raise ModeError("Resources unavailable", 409)
                        if self.diagnostic and self.diagnostic.status()["state"] == "running":
                            raise ModeError("Diagnostic already running", 409)
                        if self.diagnostic:
                            self.diagnostic.close()
                        self.diagnostic = DiagnosticLog(self.diagnostic_dir, duration_seconds=duration)
                    elif self.diagnostic:
                        self.diagnostic.close()
            return self.status()
        except ModeError as exc:
            return {"error": str(exc), "status_code": exc.status}

    def close(self):
        with self.ram_lock:
            if self.closed:
                return
            self.closed = True
        self.receiver.close()
        self.stop.set()
        self.writer.close()
        if self.diagnostic:
            self.diagnostic.close()
        if self.monitor:
            self.monitor.join(timeout=1)
        if self.maintenance:
            self.maintenance.join(timeout=1)
        self.app.extensions["webhook_notifier"].close(wait=False)
