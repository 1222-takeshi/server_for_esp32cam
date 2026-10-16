"""Isolated, memory-bounded receiver for experimental ESP32-CAM live frames."""

from __future__ import annotations

import hmac
import json
import math
import secrets
import time
from contextlib import suppress
from http import HTTPStatus
from io import BytesIO
from threading import Event, Lock, Thread, current_thread

from PIL import Image, UnidentifiedImageError
from websockets.datastructures import MultipleValuesError
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from script.detector import Detection
from script.frame_protocol import ClockSample, FrameOrder, HEADER, MAX_JPEG_BYTES, decode_frame


TEXT_LIMIT = 512
UINT64_MAX = 2**64 - 1
MAX_PIXELS = 1_000_000


class SinkFailure(RuntimeError):
    """The observation stream cannot be trusted after its sink fails."""


def _uint(value, *, zero=False):
    return (isinstance(value, int) and not isinstance(value, bool)
            and (0 if zero else 1) <= value <= UINT64_MAX)


def _jpeg_header(jpeg):
    if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
        return False
    try:
        with Image.open(BytesIO(jpeg)) as image:
            width, height = image.size
            return (image.format == "JPEG" and 0 < width <= 1600
                    and 0 < height <= 1200 and width * height <= MAX_PIXELS)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        return False


def _text_message(raw):
    if len(raw.encode("utf-8")) > TEXT_LIMIT:
        raise ValueError("text_size")
    try:
        item = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(item, dict):
        raise ValueError("invalid_text")
    kind = item.get("type")
    if kind == "clock.pong":
        if (set(item) != {"type", "id", "boot_id", "device_us"}
                or not isinstance(item["id"], str)
                or len(item["id"]) != 16
                or not _uint(item["boot_id"]) or not _uint(item["device_us"])):
            raise ValueError("invalid_pong")
    elif kind == "capture.status":
        fields = {"type", "boot_id", "attempts", "captured", "capture_failures",
                  "stale", "sent", "send_failures"}
        if set(item) != fields or not _uint(item["boot_id"]):
            raise ValueError("invalid_status")
        if any(not _uint(item[field], zero=True) for field in fields - {"type", "boot_id"}):
            raise ValueError("invalid_status")
    else:
        raise ValueError("unknown_type")
    return item


def _detections(value):
    """Keep inference records small, JSON safe, and free of arbitrary model data."""
    if not isinstance(value, list) or len(value) > 300:
        raise ValueError("invalid_detections")
    result = []
    for item in value:
        if not isinstance(item, Detection):
            raise ValueError("invalid_detections")
        if (not isinstance(item.label, str) or not item.label.strip()
                or len(item.label) > 64 or any(ord(char) < 32 for char in item.label)):
            raise ValueError("invalid_detections")
        values = (item.score, item.x1, item.y1, item.x2, item.y2)
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
                or not 0 <= v <= 1 or not math.isfinite(v) for v in values):
            raise ValueError("invalid_detections")
        if item.x1 >= item.x2 or item.y1 >= item.y2:
            raise ValueError("invalid_detections")
        if item.track_id is not None and not _uint(item.track_id):
            raise ValueError("invalid_detections")
        result.append({"label": item.label, "score": item.score, "x1": item.x1,
                       "y1": item.y1, "x2": item.x2, "y2": item.y2,
                       "track_id": item.track_id})
    return result


class CaptureReceiver:
    def __init__(self, *, camera_id, device_api_key, detector=None, event_sink=None,
                 clock_us=None, max_age_ms=500, max_duration_s=3600,
                 max_frames=100_000, max_messages=200_000):
        key = device_api_key.encode("utf-8") if isinstance(device_api_key, str) else device_api_key
        if not isinstance(key, bytes) or not key:
            raise ValueError("A nonempty device API key is required")
        if not isinstance(camera_id, str) or not camera_id or len(camera_id) > 64:
            raise ValueError("Invalid camera ID")
        if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in
                   (max_age_ms, max_duration_s, max_frames, max_messages)):
            raise ValueError("Invalid receiver limit")
        self.camera_id = camera_id
        self._key = key
        self.detector = detector
        self.sink = event_sink or (lambda event: None)
        self.clock_us = clock_us or (lambda: time.monotonic_ns() // 1000)
        self.max_age_us = max_age_ms * 1000
        self.max_duration_us = max_duration_s * 1_000_000
        self.max_frames = max_frames
        self.max_messages = max_messages
        self.order = FrameOrder()
        self.active = False
        self._session_lock = Lock()
        self._event_lock = Lock()
        self._sink_failed = Event()
        self._stop_requested = Event()
        self._current_cancel = None
        self._current_websocket = None
        self._current_worker = None
        self._session_stopping = None

    @property
    def failed(self):
        return self._sink_failed.is_set()

    def close(self):
        """Stop from an external thread; never call from event_sink."""
        with self._session_lock:
            self._stop_requested.set()
            cancel = self._current_cancel
            websocket = self._current_websocket
            worker = self._current_worker
        if cancel is not None:
            cancel()
        # Fence any sink call that started before cancellation. A sink must be
        # bounded and must not call close() recursively.
        with self._event_lock:
            pass
        if websocket is not None:
            self._close(websocket)
        if worker is not None and worker.ident is not None and worker is not current_thread():
            worker.join(timeout=0.2)

    def _emit(self, *, _counter=None, **fields):
        with self._event_lock:
            if (self._session_stopping is not None
                    and self._session_stopping.is_set()
                    and not (fields.get("status") == "dropped"
                             and fields.get("reason") == "cancelled")):
                return
            if self._sink_failed.is_set():
                raise SinkFailure()
            try:
                if _counter is not None:
                    fields["counters"] = _counter()
                self.sink({"camera_id": self.camera_id, **fields})
            except Exception as exc:
                self._sink_failed.set()
                raise SinkFailure() from exc

    def _authorized(self, camera_id, provided):
        if camera_id != self.camera_id or not isinstance(provided, str):
            return False
        return hmac.compare_digest(provided.encode("utf-8"), self._key)

    @staticmethod
    def _close(websocket):
        with suppress(ConnectionClosed, OSError, TimeoutError):
            websocket.close()

    def handle(self, websocket, camera_id, provided_key):
        if (not self._authorized(camera_id, provided_key) or self.failed
                or self._stop_requested.is_set()):
            self._close(websocket)
            return
        wake = Event()
        stopping = Event()
        slot_lock = Lock()
        counter_lock = Lock()
        waiting = None
        running = None
        counters = {"frames": 0, "accepted": 0, "dropped": 0, "overwritten": 0,
                    "failed": 0, "inferred": 0, "fresh_success": 0}

        def bump(*names):
            with counter_lock:
                for name in names:
                    counters[name] += 1
                return dict(counters)

        def emit_counted(*names, **fields):
            self._emit(_counter=lambda: bump(*names), **fields)

        def age_high_now(metadata, now):
            return metadata["age_high_us"] + max(0, now - metadata["host_received_us"])

        def cancel_session():
            nonlocal waiting, running
            with slot_lock:
                if stopping.is_set():
                    return
                stopping.set()
                cancelled = [item for item in (waiting, running) if item is not None]
                waiting = None
                running = None
                for _, metadata in cancelled:
                    if not self.failed:
                        with suppress(SinkFailure):
                            emit_counted("dropped", status="dropped", reason="cancelled",
                                         **metadata)
            wake.set()

        def inference_worker():
            nonlocal waiting, running
            try:
                while not stopping.is_set():
                    wake.wait(timeout=0.1)
                    with slot_lock:
                        if stopping.is_set():
                            break
                        item = waiting
                        waiting = None
                        running = item
                        wake.clear()
                    if item is None:
                        continue
                    frame, metadata = item
                    started = self.clock_us()
                    with slot_lock:
                        if stopping.is_set():
                            break
                        if age_high_now(metadata, started) > self.max_age_us:
                            running = None
                            emit_counted("dropped", status="dropped", reason="expired_queue",
                                         queue_checked_us=started, **metadata)
                            continue
                    try:
                        detections = _detections(self.detector.detect(frame.jpeg))
                    except Exception:  # Model errors are intentionally not logged.
                        with slot_lock:
                            if stopping.is_set():
                                break
                            running = None
                            emit_counted("failed", status="failed", infer_started_us=started,
                                         infer_finished_us=self.clock_us(), **metadata)
                        continue
                    with slot_lock:
                        if stopping.is_set():
                            break
                        running = None
                        finished = self.clock_us()
                        if age_high_now(metadata, finished) > self.max_age_us:
                            emit_counted("inferred", "dropped", status="dropped",
                                         reason="expired_result", infer_started_us=started,
                                         infer_finished_us=finished, **metadata)
                        else:
                            emit_counted("inferred", "fresh_success", status="success",
                                         infer_started_us=started, infer_finished_us=finished,
                                         detections=detections, **metadata)
            except SinkFailure:
                stopping.set()
            finally:
                with self._session_lock:
                    self.active = False
                    self._current_cancel = None
                    self._current_websocket = None
                    self._current_worker = None

        worker = Thread(target=inference_worker, daemon=True) if self.detector is not None else None
        with self._session_lock:
            rejected = self.active or self.failed or self._stop_requested.is_set()
            if not rejected:
                self.active = True
                self._current_cancel = cancel_session
                self._current_websocket = websocket
                self._current_worker = worker
                self._session_stopping = stopping
        if rejected:
            self._close(websocket)
            return
        if worker is not None:
            worker.start()
        begun = self.clock_us()
        next_ping = begun
        pending = None
        sample = None
        boot = None
        messages = 0

        def ping(now):
            nonce = secrets.token_hex(8)
            websocket.send(json.dumps({"type": "clock.ping", "id": nonce}))
            return (nonce, now), now + 2_000_000

        try:
            while True:
                if self.failed or self._stop_requested.is_set() or stopping.is_set():
                    break
                now = self.clock_us()
                if now - begun >= self.max_duration_us or messages >= self.max_messages:
                    break
                if pending is not None and now - pending[1] > 1_000_000:
                    self._emit(status="closed", reason="clock_timeout")
                    break
                if now >= next_ping and pending is None:
                    pending, next_ping = ping(now)
                try:
                    raw = websocket.receive(timeout=0.1)
                except TimeoutError:
                    continue
                except (ConnectionClosed, OSError):
                    break
                if raw is None:
                    if getattr(websocket, "connected", False):
                        continue
                    break
                messages += 1
                received = self.clock_us()
                if isinstance(raw, str):
                    try:
                        item = _text_message(raw)
                        if item["type"] == "clock.pong":
                            if pending is None or item["id"] != pending[0]:
                                raise ValueError("clock_nonce")
                            if boot is not None and item["boot_id"] != boot:
                                raise ValueError("boot_mismatch")
                            sample = ClockSample.from_exchange(item["boot_id"],
                                                               item["device_us"],
                                                               pending[1], received)
                            boot = item["boot_id"]
                            pending = None
                            self._emit(status="clock", boot_id=boot,
                                       clock_device_us=item["device_us"],
                                       host_received_us=received,
                                       offset_low_us=sample.offset_low_us,
                                       offset_high_us=sample.offset_high_us,
                                       round_trip_us=received - sample.received_us +
                                       sample.offset_high_us - sample.offset_low_us,
                                       drift_bound_ppm=200)
                        else:
                            if boot is not None and item["boot_id"] != boot:
                                raise ValueError("boot_mismatch")
                            boot = item["boot_id"]
                            self._emit(status="device_status", host_received_us=received,
                                       **{k: v for k, v in item.items() if k != "type"})
                    except ValueError as exc:
                        self._emit(status="closed", reason=str(exc))
                        break
                    continue
                if not isinstance(raw, bytes):
                    self._emit(status="closed", reason="invalid_message")
                    break
                if bump("frames")["frames"] > self.max_frames:
                    break
                try:
                    frame = decode_frame(raw)
                except ValueError:
                    self._emit(status="closed", reason="invalid_frame")
                    break
                metadata = {"boot_id": frame.boot_id, "sequence": frame.sequence,
                            "captured_us": frame.captured_us, "sent_us": frame.sent_us,
                            "host_received_us": received, "payload_bytes": len(raw)}
                if boot is not None and frame.boot_id != boot:
                    self._emit(status="closed", reason="boot_mismatch", **metadata)
                    break
                boot = frame.boot_id
                classification, gap = self.order.observe(frame)
                if classification in {"duplicate", "stale", "retired_boot"}:
                    reason = classification
                else:
                    metadata.update(classification=classification, sequence_gap=gap)
                    reason = None
                if reason is None and not _jpeg_header(frame.jpeg):
                    reason = "invalid_jpeg"
                elif reason is None and sample is None:
                    reason = "clock_unknown"
                elif reason is None:
                    age = sample.age_bounds_us(frame, received)
                    if age is None:
                        reason = "clock_expired"
                    elif age[1] > self.max_age_us:
                        reason = "stale_capture"
                    else:
                        metadata.update(age_low_us=age[0], age_high_us=age[1])
                if reason:
                    emit_counted("dropped", status="dropped", reason=reason, **metadata)
                    continue
                # Publish acceptance and hand off its frame atomically with
                # cancellation. Otherwise close() can miss an accepted frame.
                with slot_lock:
                    if stopping.is_set():
                        break
                    emit_counted("accepted", status="accepted", **metadata)
                    if self.detector is not None:
                        replaced = waiting
                        waiting = (frame, metadata)
                        if replaced is not None:
                            emit_counted("overwritten", "dropped", status="dropped",
                                         reason="overwritten", **replaced[1])
                        wake.set()
        except (SinkFailure, ConnectionClosed, OSError, TimeoutError):
            pass
        finally:
            cancel_session()
            if worker is not None:
                worker.join(timeout=0.2)
            else:
                with self._session_lock:
                    self.active = False
                    self._current_cancel = None
                    self._current_websocket = None
                    self._current_worker = None
            self._close(websocket)
            with self._session_lock:
                if self._session_stopping is stopping:
                    self._session_stopping = None


def create_receiver(*, camera_id="esp32cam-01", device_api_key, detector=None,
                    event_sink=None, clock_us=None, max_age_ms=500,
                    max_duration_s=3600, max_frames=100_000, max_messages=200_000):
    """Build an isolated receiver; caller supplies a private key."""
    return CaptureReceiver(camera_id=camera_id, device_api_key=device_api_key,
                           detector=detector, event_sink=event_sink, clock_us=clock_us,
                           max_age_ms=max_age_ms, max_duration_s=max_duration_s,
                           max_frames=max_frames, max_messages=max_messages)


class _SocketAdapter:
    def __init__(self, connection):
        self.connection = connection

    def receive(self, timeout=None):
        return self.connection.recv(timeout=timeout)

    def send(self, payload):
        self.connection.send(payload)

    def close(self):
        self.connection.close()


def serve_live(receiver, host="127.0.0.1", port=8765):
    """Return a websockets server with bounded ingress and TCP backpressure.

    Use as ``with serve_live(receiver, host, port) as server: server.serve_forever()``.
    The (2, 1) high/low watermarks pause socket reads, with bounded read-buffer
    overshoot. Each message is at most one ECF1 envelope in size. Disable
    compression to avoid expansion and CPU spikes.
    """
    path = f"/ws/cameras/{receiver.camera_id}/live"

    def request_key(request):
        try:
            return request.headers.get("X-API-Key")
        except MultipleValuesError:  # Duplicate credential headers aren't accepted.
            return None

    def process_request(connection, request):
        if request.path == "/healthz":
            return connection.respond(HTTPStatus.OK, json.dumps(
                {"ok": not receiver._sink_failed.is_set(), "active": receiver.active}))
        if request.path != path:
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found")
        if not receiver._authorized(receiver.camera_id, request_key(request)):
            return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden")
        if receiver.active or receiver.failed or receiver._stop_requested.is_set():
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Unavailable")
        return None

    def handler(connection):
        receiver.handle(_SocketAdapter(connection), receiver.camera_id,
                        request_key(connection.request))

    return serve(handler, host, port, process_request=process_request,
                 max_size=HEADER.size + MAX_JPEG_BYTES, max_queue=(2, 1),
                 compression=None, ping_interval=20, close_timeout=1)
