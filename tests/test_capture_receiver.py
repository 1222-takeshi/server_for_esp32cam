"""Behavioral checks for the isolated, bounded live receiver."""

import json
import struct
import time
from io import BytesIO
from threading import Event, Thread
from urllib.request import urlopen

import pytest
from PIL import Image
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from script.capture_receiver import create_receiver, serve_live
from script.detector import Detection


def jpeg():
    output = BytesIO()
    Image.new("RGB", (8, 8)).save(output, format="JPEG")
    return output.getvalue()


def frame(sequence=1, *, boot=7, captured=1_000_100, sent=1_000_110, image=None):
    image = jpeg() if image is None else image
    return struct.pack(">4sQQQQI", b"ECF1", boot, sequence, captured, sent, len(image)) + image


class Clock:
    def __init__(self):
        self.value = 2_000_000

    def __call__(self):
        self.value += 100
        return self.value


class Socket:
    def __init__(self, messages, idle_wait=0):
        self.messages = list(messages)
        self.sent = []
        self.closed = False
        self.idle_wait = idle_wait

    def send(self, message):
        self.sent.append(json.loads(message))

    def receive(self, timeout=None):
        if not self.messages:
            if self.idle_wait > 0:
                self.idle_wait -= 0.01
                time.sleep(0.01)
                raise TimeoutError
            return None
        message = self.messages.pop(0)
        if message == "PAUSE":
            time.sleep(0.02)
            raise TimeoutError
        if message == "PONG":
            return json.dumps({"type": "clock.pong", "id": self.sent[-1]["id"],
                               "boot_id": 7, "device_us": 1_000_000})
        return message

    def close(self, *args, **kwargs):
        self.closed = True


def receiver(messages, **kwargs):
    events = []
    app = create_receiver(device_api_key="private", clock_us=Clock(), event_sink=events.append,
                          **kwargs)
    socket = Socket(messages, idle_wait=0.05 if kwargs.get("detector") else 0)
    app.handle(socket, "esp32cam-01", "private")
    return socket, events


def test_service_has_health_and_bounded_websocket_ingress():
    events = []
    app = create_receiver(device_api_key="private", clock_us=Clock(),
                          event_sink=events.append)
    with serve_live(app, "127.0.0.1", 0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.socket.getsockname()[1]
        try:
            with urlopen(f"http://127.0.0.1:{port}/healthz") as response:
                assert json.load(response)["ok"] is True
            with connect(f"ws://127.0.0.1:{port}/ws/cameras/esp32cam-01/live",
                         additional_headers={"X-API-Key": "private"}) as websocket:
                ping = json.loads(websocket.recv())
                assert ping["type"] == "clock.ping"
                connection = next(iter(server.connections))
                assert connection.recv_messages.high == 2
                websocket.send(json.dumps({"type": "clock.pong", "id": ping["id"],
                                           "boot_id": 7, "device_us": 1_000_000}))
                websocket.send(frame())
                for _ in range(100):
                    if any(event["status"] == "accepted" for event in events):
                        break
                    time.sleep(0.01)
                assert any(event["status"] == "accepted" for event in events)
            with pytest.raises(InvalidStatus) as denied:
                connect(f"ws://127.0.0.1:{port}/ws/cameras/esp32cam-01/live",
                        additional_headers={"X-API-Key": "wrong"})
            assert denied.value.response.status_code == 403
            with pytest.raises(InvalidStatus) as duplicate:
                connect(f"ws://127.0.0.1:{port}/ws/cameras/esp32cam-01/live",
                        additional_headers=[("X-API-Key", "private"),
                                            ("X-API-Key", "private")])
            assert duplicate.value.response.status_code == 403
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize("key,camera", [(None, "esp32cam-01"), ("wrong", "esp32cam-01"),
                                        ("private", "other")])
def test_connection_requires_key_and_configured_camera(key, camera):
    app = create_receiver(device_api_key="private")
    socket = Socket([])
    app.handle(socket, camera, key)
    assert socket.closed
    assert socket.sent == []


def test_factory_refuses_empty_key():
    with pytest.raises(ValueError):
        create_receiver(device_api_key="")


def test_valid_transfer_has_capture_age_and_no_inference_or_image_event():
    socket, events = receiver(["PONG", frame()])
    assert socket.closed
    accepted = [event for event in events if event["status"] == "accepted"]
    assert len(accepted) == 1
    assert accepted[0]["sequence"] == 1
    assert accepted[0]["age_high_us"] < 500_000
    assert all("jpeg" not in event and "inference_ms" not in event for event in events)


def test_unsolicited_or_replayed_clock_nonce_closes_connection():
    invalid = json.dumps({"type": "clock.pong", "id": "0123456789abcdef", "boot_id": 7,
                          "device_us": 1_000_000})
    socket, events = receiver([invalid, frame()])
    assert socket.closed
    assert not any(event["status"] == "accepted" for event in events)


def test_pong_rejects_boolean_boot_id():
    class BadPong(Socket):
        def receive(self, timeout=None):
            self.messages.pop(0)
            return json.dumps({"type": "clock.pong", "id": self.sent[-1]["id"],
                               "boot_id": True, "device_us": 1_000_000})

    events = []
    app = create_receiver(device_api_key="private", clock_us=Clock(), event_sink=events.append)
    socket = BadPong(["ignored"])
    app.handle(socket, "esp32cam-01", "private")
    assert socket.closed and events[-1]["reason"] == "invalid_pong"


@pytest.mark.parametrize("bad", ["{}", "x" * 513, json.dumps({"type": "capture.status",
    "boot_id": True, "attempts": 0, "captured": 0, "capture_failures": 0,
    "stale": 0, "sent": 0, "send_failures": 0})])
def test_invalid_text_closes_connection(bad):
    socket, events = receiver([bad])
    assert socket.closed
    assert not any(event["status"] == "accepted" for event in events)


def test_unknown_clock_and_bad_jpeg_are_dropped_without_storage(tmp_path):
    socket, events = receiver([frame(), "PONG", frame(2, captured=1_000_200,
                                                       sent=1_000_210, image=b"garbage")])
    assert socket.closed
    assert [event["reason"] for event in events if event["status"] == "dropped"] == [
        "clock_unknown", "invalid_jpeg"]
    assert list(tmp_path.iterdir()) == []


def test_jpeg_with_excessive_dimensions_is_dropped():
    output = BytesIO()
    Image.new("RGB", (1601, 1)).save(output, format="JPEG")
    _, events = receiver(["PONG", frame(image=output.getvalue())])
    assert any(event.get("reason") == "invalid_jpeg" for event in events)


def test_duplicate_stale_and_changed_boot_are_rejected():
    socket, events = receiver(["PONG", frame(), frame(), frame(2, captured=1_000_050),
                               frame(1, boot=8)])
    assert socket.closed
    assert [event["reason"] for event in events if event["status"] == "dropped"] == [
        "duplicate", "stale"]
    assert events[-1]["reason"] == "boot_mismatch"


def test_detector_failure_does_not_stop_next_frame():
    class Detector:
        calls = 0

        def detect(self, image):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("sensitive failure")
            return [Detection("cat", 0.9, 0.1, 0.2, 0.5, 0.7)]

    socket, events = receiver(["PONG", frame(), "PAUSE", frame(2, captured=1_000_200,
                                                      sent=1_000_210)], detector=Detector())
    assert socket.closed
    assert any(event["status"] == "failed" for event in events)
    assert next(event for event in events if event["status"] == "failed")[
        "counters"]["failed"] == 1
    assert all("sensitive failure" not in str(event) for event in events)
    assert any(event["status"] == "success" for event in events)
    assert next(event for event in events if event["status"] == "success")["detections"] == [
        {"label": "cat", "score": 0.9, "x1": 0.1, "y1": 0.2,
         "x2": 0.5, "y2": 0.7, "track_id": None}]


@pytest.mark.parametrize("bad", [
    {"label": "cat", "score": 0.8},
    Detection("cat", float("nan"), 0.1, 0.1, 0.5, 0.5),
    Detection("cat", 0.8, 0.5, 0.1, 0.5, 0.5),
    Detection("", 0.8, 0.1, 0.1, 0.5, 0.5),
])
def test_invalid_detection_result_is_failed_and_has_no_success(bad):
    class Detector:
        def detect(self, image):
            return [bad]

    _, events = receiver(["PONG", frame()], detector=Detector())
    assert any(event["status"] == "failed" for event in events)
    assert not any(event["status"] == "success" for event in events)


def test_empty_detections_are_a_success():
    class Detector:
        def detect(self, image):
            return []

    _, events = receiver(["PONG", frame()], detector=Detector())
    assert next(event for event in events if event["status"] == "success")["detections"] == []


@pytest.mark.parametrize("count,expected", [(300, "success"), (301, "failed")])
def test_detection_record_limit_matches_model_cap(count, expected):
    class Detector:
        def detect(self, image):
            return [Detection("cat", 0.9, 0.1, 0.1, 0.9, 0.9)] * count

    _, events = receiver(["PONG", frame()], detector=Detector())
    assert any(event["status"] == expected for event in events)


def test_stale_capture_is_dropped_using_clock_bounds():
    _, events = receiver(["PONG", frame(captured=100_000, sent=100_010)])
    assert [event["reason"] for event in events if event["status"] == "dropped"] == [
        "stale_capture"]


def test_expired_clock_sample_drops_frame_even_if_capture_is_recent():
    clock = Clock()
    events = []
    app = create_receiver(device_api_key="private", clock_us=clock, event_sink=events.append)

    class DelayedSocket(Socket):
        def receive(self, timeout=None):
            if self.messages and isinstance(self.messages[0], bytes):
                clock.value += 11_000_000
            return super().receive(timeout)

    socket = DelayedSocket(["PONG", frame(captured=12_000_000, sent=12_000_010)])
    app.handle(socket, "esp32cam-01", "private")
    assert socket.closed
    assert any(event.get("reason") == "clock_expired" for event in events)


def test_oversized_envelope_closes_and_does_not_reach_sink_as_image():
    socket, events = receiver(["PONG", b"x" * (40 + 128 * 1024 + 1)])
    assert socket.closed
    assert events[-1]["reason"] == "invalid_frame"
    assert all("jpeg" not in event for event in events)


def test_status_counters_are_bounded_and_reported():
    status = {"type": "capture.status", "boot_id": 7, "attempts": 2,
              "captured": 1, "capture_failures": 1, "stale": 0,
              "sent": 1, "send_failures": 0}
    _, events = receiver([json.dumps(status), "PONG", frame()])
    assert any(event["status"] == "device_status" and event["attempts"] == 2
               for event in events)
    status_event = next(event for event in events if event["status"] == "device_status")
    clock_event = next(event for event in events if event["status"] == "clock")
    assert status_event["host_received_us"] > 0
    assert clock_event["host_received_us"] > 0
    assert clock_event["clock_device_us"] == 1_000_000


def test_second_live_connection_is_refused():
    app = create_receiver(device_api_key="private")
    live = app
    second = Socket([])

    class NestedSocket(Socket):
        def receive(self, timeout=None):
            live.handle(second, "esp32cam-01", "private")
            return None

    live.handle(NestedSocket([]), "esp32cam-01", "private")
    assert second.closed and second.sent == []


def test_idle_receive_timeouts_keep_connection_open_until_frame():
    class IdleSocket(Socket):
        connected = True

        def __init__(self):
            super().__init__(["PONG", frame()])
            self.idle_count = 0

        def receive(self, timeout=None):
            if self.idle_count < 3:
                self.idle_count += 1
                return None  # simple_websocket's receive(timeout) behavior
            if self.messages:
                return super().receive(timeout)
            self.connected = False
            return None

    app = create_receiver(device_api_key="private", clock_us=Clock())
    events = []
    app.sink = events.append
    socket = IdleSocket()
    app.handle(socket, "esp32cam-01", "private")
    assert socket.idle_count == 3
    assert any(event["status"] == "accepted" for event in events)


def test_receiver_drop_does_not_count_as_network_sequence_gap():
    _, events = receiver(["PONG", frame(image=b"bad"), frame(2, captured=1_000_200,
                                                           sent=1_000_210)])
    accepted = next(event for event in events if event["status"] == "accepted")
    assert accepted["sequence_gap"] == 0
    assert accepted["sequence"] == 2


def test_invalid_jpeg_sequence_cannot_be_replayed_with_new_image():
    _, events = receiver(["PONG", frame(image=b"bad"), frame()])
    assert [event["reason"] for event in events if event["status"] == "dropped"] == [
        "invalid_jpeg", "duplicate"]


def test_missing_network_sequence_is_counted():
    _, events = receiver(["PONG", frame(), frame(3, captured=1_000_300,
                                                 sent=1_000_310)])
    accepted = [event for event in events if event["status"] == "accepted"]
    assert accepted[-1]["sequence_gap"] == 1


def test_result_expiring_during_inference_is_not_fresh_success():
    clock = Clock()

    class SlowDetector:
        def detect(self, image):
            clock.value += 600_000
            return [Detection("cat", 0.9, 0.1, 0.1, 0.9, 0.9)]

    events = []
    app = create_receiver(device_api_key="private", clock_us=clock,
                     event_sink=events.append, detector=SlowDetector())
    app.handle(Socket(["PONG", frame()], idle_wait=0.05),
                                               "esp32cam-01", "private")
    assert any(event.get("reason") == "expired_result" for event in events)
    assert not any(event["status"] == "success" for event in events)
    assert next(event for event in events if event.get("reason") == "expired_result")[
        "counters"]["inferred"] == 1


def test_nearly_expired_frame_does_not_delay_newer_fresh_inference():
    clock, skipped = Clock(), Event()
    events, calls = [], []

    def observe(event):
        events.append(event)
        if event.get("reason") == "insufficient_inference_time":
            skipped.set()

    class Detector:
        def detect(self, image):
            calls.append(image)
            clock.value += 150_000
            return []

    class BurstSocket(Socket):
        def receive(self, timeout=None):
            if self.messages and self.messages[0] == "OLD":
                self.messages.pop(0)
                clock.value += 480_000  # Delayed TCP frame, still below the 500ms ingress limit.
                return frame()
            if self.messages and self.messages[0] == "NEW":
                self.messages.pop(0)
                assert skipped.wait(2)
                return frame(2, captured=1_480_100, sent=1_480_110)
            return super().receive(timeout)

    app = create_receiver(device_api_key="private", clock_us=clock, detector=Detector(),
                          event_sink=observe, inference_reserve_ms=165)
    app.handle(BurstSocket(["PONG", "OLD", "NEW"], idle_wait=.05), "esp32cam-01", "private")
    assert len(calls) == 1
    assert [event["sequence"] for event in events if event["status"] == "success"] == [2]
    rejected = next(event for event in events if event.get("reason") == "insufficient_inference_time")
    assert rejected["sequence"] == 1 and rejected["counters"]["inferred"] == 0


@pytest.mark.parametrize("value", [-1, True, 500, 1.5])
def test_invalid_inference_reserve_is_rejected(value):
    with pytest.raises(ValueError):
        create_receiver(device_api_key="private", inference_reserve_ms=value)


@pytest.mark.parametrize("age_us,accepted", [(335_000, True), (335_001, False)])
@pytest.mark.parametrize("duration_us", [150_000, 221_000])
def test_inference_reserve_exact_deadline_boundary(age_us, accepted, duration_us):
    class FixedClock:
        value = 2_000_000

        def __call__(self):
            return self.value

    clock, calls, events = FixedClock(), [], []

    class Detector:
        def detect(self, image):
            calls.append(image)
            clock.value += duration_us
            return []

    class DelayedSocket(Socket):
        def receive(self, timeout=None):
            if self.messages and self.messages[0] == "IMAGE":
                self.messages.pop(0)
                clock.value += age_us
                return frame(captured=1_000_000, sent=1_000_000)
            return super().receive(timeout)

    app = create_receiver(device_api_key="private", clock_us=clock, detector=Detector(),
                          event_sink=events.append, inference_reserve_ms=165)
    app.handle(DelayedSocket(["PONG", "IMAGE"], idle_wait=.05), "esp32cam-01", "private")
    assert bool(calls) is accepted
    assert any(event["status"] == "success" for event in events) is (accepted and duration_us == 150_000)
    if accepted and duration_us == 221_000:
        assert any(event.get("reason") == "expired_result" for event in events)


def test_waiting_frame_expiring_before_inference_is_dropped():
    clock = Clock()
    started = Event()
    release = Event()

    class SlowFirstDetector:
        def __init__(self):
            self.calls = 0

        def detect(self, image):
            self.calls += 1
            if self.calls == 1:
                started.set()
                release.wait(timeout=1)
                clock.value += 600_000
            return []

    detector = SlowFirstDetector()
    events = []
    app = create_receiver(device_api_key="private", clock_us=clock,
                     event_sink=events.append, detector=detector)
    class TimedSocket(Socket):
        def receive(self, timeout=None):
            if len(self.messages) == 1:
                started.wait(timeout=1)
            if not self.messages:
                release.set()
            return super().receive(timeout)

    socket = TimedSocket(["PONG", frame(), frame(2, captured=1_000_200,
                                                sent=1_000_210)], idle_wait=0.05)
    app.handle(socket, "esp32cam-01", "private")
    assert detector.calls == 1
    assert any(event.get("reason") == "expired_queue" for event in events)


def test_latest_waiting_frame_replaces_previous_under_slow_inference():
    release = Event()

    class SlowDetector:
        def detect(self, image):
            release.wait(timeout=1)
            return []

    events = []
    app = create_receiver(device_api_key="private", clock_us=Clock(),
                     event_sink=events.append, detector=SlowDetector())

    class ReleasingSocket(Socket):
        def receive(self, timeout=None):
            if not self.messages:
                release.set()
            return super().receive(timeout)

    socket = ReleasingSocket(["PONG", frame(), frame(2, captured=1_000_200,
        sent=1_000_210), frame(3, captured=1_000_300, sent=1_000_310)])
    app.handle(socket, "esp32cam-01", "private")
    overwritten = next(event for event in events if event.get("reason") == "overwritten")
    assert overwritten["counters"]["overwritten"] == 1
    assert overwritten["counters"]["dropped"] == 1


def test_disconnect_cancels_waiting_and_running_results():
    entered = Event()
    release = Event()

    class BlockingDetector:
        calls = 0

        def detect(self, image):
            self.calls += 1
            entered.set()
            release.wait(timeout=2)
            return [Detection("cat", 0.9, 0.1, 0.1, 0.9, 0.9)]

    detector = BlockingDetector()
    events = []
    app = create_receiver(device_api_key="private", clock_us=Clock(),
                          event_sink=events.append, detector=detector)

    class ClosingSocket(Socket):
        def receive(self, timeout=None):
            if len(self.messages) == 1:
                entered.wait(timeout=1)
            return super().receive(timeout)

    socket = ClosingSocket(["PONG", frame(), frame(2, captured=1_000_200,
                                                   sent=1_000_210)])
    app.handle(socket, "esp32cam-01", "private")
    assert app.active  # Native call still owns the only connection slot.
    release.set()
    for _ in range(100):
        if not app.active:
            break
        time.sleep(0.01)
    assert not app.active
    assert detector.calls == 1
    assert not any(event["status"] == "success" for event in events)
    assert sum(event.get("reason") == "cancelled" for event in events) == 2


def test_sink_failure_stops_session_and_refuses_reconnect():
    seen = []

    def failing_sink(event):
        seen.append(event)
        if event["status"] == "accepted":
            raise OSError("private sink path")

    app = create_receiver(device_api_key="private", clock_us=Clock(),
                          event_sink=failing_sink)
    socket = Socket(["PONG", frame(), frame(2, captured=1_000_200,
                                            sent=1_000_210)])
    app.handle(socket, "esp32cam-01", "private")
    assert socket.closed
    assert sum(event["status"] == "accepted" for event in seen) == 1
    reconnect = Socket([])
    app.handle(reconnect, "esp32cam-01", "private")
    assert reconnect.closed and reconnect.sent == []


def test_sink_failure_from_detection_thread_stops_receiver():
    events = []

    def failing_sink(event):
        events.append(event)
        if event["status"] == "success":
            raise OSError("private sink path")

    class Detector:
        def detect(self, image):
            return []

    app = create_receiver(device_api_key="private", clock_us=Clock(),
                          event_sink=failing_sink, detector=Detector())
    socket = Socket(["PONG", frame()], idle_wait=0.05)
    app.handle(socket, "esp32cam-01", "private")
    assert socket.closed and not app.active
    assert sum(event["status"] == "success" for event in events) == 1
    second = Socket([])
    app.handle(second, "esp32cam-01", "private")
    assert second.closed and second.sent == []


def test_public_close_cancels_active_session_and_blocks_new_connections():
    detecting = Event()
    release = Event()
    socket_closed = Event()

    class Detector:
        def detect(self, image):
            detecting.set()
            release.wait(timeout=2)
            return []

    class HoldingSocket(Socket):
        def receive(self, timeout=None):
            if self.messages:
                return super().receive(timeout)
            if socket_closed.wait(timeout=0.01):
                return None
            raise TimeoutError

        def close(self, *args, **kwargs):
            super().close(*args, **kwargs)
            socket_closed.set()

    events = []
    app = create_receiver(device_api_key="private", clock_us=Clock(),
                          event_sink=events.append, detector=Detector())
    socket = HoldingSocket(["PONG", frame()])
    handler = Thread(target=app.handle, args=(socket, "esp32cam-01", "private"))
    handler.start()
    assert detecting.wait(timeout=1)
    app.close()
    assert socket.closed
    release.set()
    handler.join(timeout=1)
    assert not handler.is_alive()
    for _ in range(100):
        if not app.active:
            break
        time.sleep(0.01)
    assert not app.active and not app.failed
    assert any(event.get("reason") == "cancelled" for event in events)
    assert not any(event["status"] == "success" for event in events)
    second = Socket([])
    app.handle(second, "esp32cam-01", "private")
    assert second.closed and second.sent == []


def test_close_during_session_start_cannot_leave_orphan_worker(monkeypatch):
    ready = Event()
    release = Event()
    original_start = Thread.start

    def held_worker_start(thread):
        ready.set()
        release.wait(timeout=1)
        original_start(thread)

    monkeypatch.setattr("script.capture_receiver.Thread.start", held_worker_start)

    class Detector:
        def detect(self, image):
            pytest.fail("Cancelled session must not invoke detector")

    events = []
    app = create_receiver(device_api_key="private", clock_us=Clock(),
                          event_sink=events.append, detector=Detector())
    socket = Socket(["PONG", frame()])
    handler = Thread(target=app.handle, args=(socket, "esp32cam-01", "private"))
    original_start(handler)
    assert ready.wait(timeout=1)
    app.close()
    release.set()
    handler.join(timeout=1)
    assert not handler.is_alive()
    assert not app.active and socket.closed
    assert not any(event["status"] == "success" for event in events)


def test_close_during_accepted_sink_emits_terminal_cancel():
    entered = Event()
    release = Event()
    events = []

    app = create_receiver(device_api_key="private", clock_us=Clock(),
                          event_sink=events.append,
                          detector=type("Detector", (), {"detect": lambda self, image: []})())
    original_emit = app._emit

    def held_emit(*, _counter=None, **fields):
        if fields["status"] == "accepted":
            entered.set()
            release.wait(timeout=2)
        return original_emit(_counter=_counter, **fields)

    app._emit = held_emit
    socket = Socket(["PONG", frame()])
    handler = Thread(target=app.handle, args=(socket, "esp32cam-01", "private"))
    handler.start()
    assert entered.wait(timeout=1)
    closer = Thread(target=app.close)
    closer.start()
    assert app._stop_requested.wait(timeout=1)
    release.set()
    closer.join(timeout=2)
    handler.join(timeout=2)
    assert not closer.is_alive() and not handler.is_alive()
    assert [event["status"] for event in events].count("accepted") == 1
    assert sum(event.get("reason") == "cancelled" for event in events) == 1
    assert not any(event["status"] == "success" for event in events)


def test_close_during_overwrite_still_records_old_waiting_drop():
    started = Event()
    release_detector = Event()
    at_overwrite = Event()
    release_overwrite = Event()
    events = []

    class Detector:
        def detect(self, image):
            started.set()
            release_detector.wait(timeout=2)
            return []

    app = create_receiver(device_api_key="private", clock_us=Clock(),
                          event_sink=events.append, detector=Detector())
    original_emit = app._emit

    def held_emit(*, _counter=None, **fields):
        if fields.get("reason") == "overwritten":
            at_overwrite.set()
            release_overwrite.wait(timeout=2)
        return original_emit(_counter=_counter, **fields)

    app._emit = held_emit

    class WaitForDetectorSocket(Socket):
        def receive(self, timeout=None):
            if len(self.messages) == 2:
                started.wait(timeout=1)
            return super().receive(timeout)

    socket = WaitForDetectorSocket(["PONG", frame(), frame(2, captured=1_000_200,
        sent=1_000_210), frame(3, captured=1_000_300, sent=1_000_310)])
    handler = Thread(target=app.handle, args=(socket, "esp32cam-01", "private"))
    handler.start()
    assert at_overwrite.wait(timeout=1)
    closer = Thread(target=app.close)
    closer.start()
    assert app._stop_requested.wait(timeout=1)
    release_overwrite.set()
    closer.join(timeout=2)
    release_detector.set()
    handler.join(timeout=2)
    assert not closer.is_alive() and not handler.is_alive()
    old = next(event for event in events if event.get("reason") == "overwritten")
    assert old["sequence"] == 2 and old["counters"]["dropped"] == 1
    assert sum(event.get("reason") == "cancelled" for event in events) == 2


def test_real_transport_applies_backpressure_when_consumer_stalls():
    from websockets.exceptions import ConnectionClosed

    blocked = Event()
    release = Event()
    completed = Event()
    progressed = Event()
    sent = []

    def sink(event):
        if event['status'] == 'accepted':
            blocked.set()
            release.wait(timeout=3)

    app = create_receiver(device_api_key='private', clock_us=Clock(), event_sink=sink)
    with serve_live(app, '127.0.0.1', 0) as server:
        serving = Thread(target=server.serve_forever, daemon=True)
        serving.start()
        port = server.socket.getsockname()[1]
        with connect(f'ws://127.0.0.1:{port}/ws/cameras/esp32cam-01/live',
                            additional_headers={'X-API-Key': 'private'}, compression=None) as websocket:
            ping = json.loads(websocket.recv())
            websocket.send(json.dumps({'type': 'clock.pong', 'id': ping['id'],
                                       'boot_id': 7, 'device_us': 1_000_000}))
            websocket.send(frame())
            assert blocked.wait(timeout=1)
            # Many maximum-size messages must reach TCP backpressure while the
            # consumer stalls, rather than accumulating in an unbounded input list.
            payload = frame(2, image=b'\xff\xd8' + b'\0' * (128 * 1024 - 4) + b'\xff\xd9')

            def produce():
                try:
                    for index in range(1000):
                        websocket.send(payload)
                        sent.append(index)
                        progressed.set()
                except ConnectionClosed:
                    pass
                finally:
                    completed.set()

            producer = Thread(target=produce, daemon=True)
            producer.start()
            try:
                assert progressed.wait(timeout=1)
                previous = -1
                for _ in range(20):
                    current = len(sent)
                    if current == previous and not completed.is_set():
                        break
                    previous = current
                    time.sleep(0.05)
                else:
                    pytest.fail("sender did not reach backpressure")
                assert 0 < len(sent) < 1000
            finally:
                release.set()
                app.close()
                server.shutdown()
                websocket.close()
                producer.join(timeout=2)
                serving.join(timeout=2)
            assert not producer.is_alive()
