import json
from io import BytesIO
from threading import Event, Thread

import pytest
from PIL import Image
from websockets.sync.client import connect

from script.capture_receiver import _text_message, create_receiver, serve_live
from script.frame_protocol import CameraFrame, HEADER, MAX_GENERATION, PROFILE_HEADER
from script.operating_mode import OperatingMode


class Clock:
    value = 2_000_000

    def __call__(self):
        self.value += 100
        return self.value


def image():
    output = BytesIO()
    Image.new("RGB", (8, 8)).save(output, format="JPEG")
    return output.getvalue()


def hello(**changes):
    return {"type": "profile.hello", "schema": 2, "boot_id": 7,
            "generation_floor": 0, "profiles": ["eco", "tracking"], **changes}


class Detector:
    def __init__(self):
        self.calls = 0

    def detect(self, _jpeg):
        self.calls += 1
        return []


class Socket:
    def __init__(self, clock, messages):
        self.clock, self.messages = clock, iter(messages)
        self.sent = []
        self.closed = False
        self.sequence = 0

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def last(self, kind):
        return next(item for item in reversed(self.sent) if item["type"] == kind)

    def receive(self, timeout=None):
        message = next(self.messages, None)
        if callable(message):
            message = message(self)
        if message == "HELLO":
            return json.dumps(hello())
        if message == "PONG":
            return json.dumps({"type": "clock.pong", "id": self.last("clock.ping")["id"],
                               "boot_id": 7, "device_us": self.clock.value - 1_000_000})
        if message == "ACK":
            command = self.last("profile.set")
            return json.dumps({**command, "type": "profile.ack", "applied_us": 500_000})
        if message in ("FRAME", "OLD", "LEGACY"):
            self.sequence += 1
            stamp = self.clock.value - 1_000_000 - 100
            jpeg = image()
            if message == "LEGACY":
                return HEADER.pack(b"ECF1", 7, self.sequence, stamp, stamp, len(jpeg)) + jpeg
            generation = self.last("profile.set")["generation"] - (message == "OLD")
            return PROFILE_HEADER.pack(b"ECF2", 7, self.sequence, stamp, stamp, generation, len(jpeg)) + jpeg
        return message

    def close(self):
        self.closed = True


def wait(event):
    def receive(_socket):
        assert event.wait(2)
        raise TimeoutError
    return receive


def setup(**kwargs):
    clock = Clock()
    mode = OperatingMode(clock=lambda: clock.value / 1_000_000)
    events, results = [], []
    done = Event()

    def result(frame, event):
        results.append((frame, event))
        done.set()

    receiver = create_receiver(device_api_key="private", profile=mode, clock_us=clock,
                               event_sink=events.append, result_sink=result, **kwargs)
    return clock, mode, events, results, done, receiver


def test_ack_and_generation_gate_preserves_bounded_result_handoff():
    detector = Detector()
    clock, mode, events, results, done, receiver = setup(detector=detector)
    socket = Socket(clock, ["HELLO", "PONG", "FRAME", "ACK", "OLD", "LEGACY", "FRAME", wait(done)])
    receiver.handle(socket, "esp32cam-01", "private")
    assert socket.closed and not receiver.failed
    assert detector.calls == 1
    assert len(results) == 1
    frame, event = results[0]
    assert frame.jpeg == image()
    assert frame.mode_generation == event["mode_generation"]
    assert event["status"] == "success" and event["sequence"] == 4
    assert len([e for e in events if e.get("reason") == "profile_unconfirmed"]) == 3
    assert mode.status()["state"] == "unknown"  # Disconnect invalidates actual state.


def test_mode_change_drops_waiting_and_running_old_generation():
    entered, release, dropped = Event(), Event(), Event()

    class Slow(Detector):
        def detect(self, jpeg):
            if self.calls == 0:
                entered.set()
                assert release.wait(2)
            return super().detect(jpeg)

    detector = Slow()
    clock, mode, events, results, done, receiver = setup(detector=detector)

    def event_sink(event):
        events.append(event)
        if event.get("reason") == "profile_changed" and "infer_finished_us" in event:
            dropped.set()

    receiver.sink = event_sink

    def change(_socket):
        mode.request("tracking", mode.version, ready=True)
        release.set()
        return "ACK"  # A late ACK for the previous command must be harmless.

    socket = Socket(clock, ["HELLO", "PONG", "ACK", "FRAME", wait(entered), "FRAME",
                            change, wait(dropped), "ACK", "FRAME", wait(done)])
    receiver.handle(socket, "esp32cam-01", "private")
    assert detector.calls == 2
    assert len(results) == 1 and results[0][1]["sequence"] == 3
    assert len([e for e in events if e.get("reason") == "profile_changed"]) == 2


def test_continuous_profile_ignores_experiment_lifetime_and_count_caps():
    clock, mode, events, results, done, receiver = setup(detector=Detector(), continuous=True,
                                                        max_duration_s=1, max_frames=1, max_messages=1)

    def advance(_socket):
        done.clear()
        clock.value += 1_100_000
        return "FRAME"

    socket = Socket(clock, ["HELLO", "PONG", "ACK", "FRAME", wait(done), advance, wait(done)])
    receiver.handle(socket, "esp32cam-01", "private")
    assert len(results) == 2
    assert results[-1][1]["counters"]["frames"] == 2
    with pytest.raises(ValueError):
        create_receiver(device_api_key="private", continuous=True)


def test_reconnect_cannot_enter_before_old_profile_teardown():
    clock, mode, events, results, done, receiver = setup(detector=Detector())
    closing, release = Event(), Event()
    original = mode.disconnect

    def blocked_disconnect():
        closing.set()
        assert release.wait(2)
        original()

    mode.disconnect = blocked_disconnect
    first = Socket(clock, ["HELLO", "PONG", "ACK", "FRAME", wait(done)])
    thread = Thread(target=receiver.handle, args=(first, "esp32cam-01", "private"))
    thread.start()
    try:
        assert closing.wait(2)
        assert receiver.active
        competing = Socket(clock, ["HELLO"])
        receiver.handle(competing, "esp32cam-01", "private")
        assert competing.closed and competing.sent == []
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and not receiver.active
    done.clear()
    replacement = Socket(clock, ["HELLO", "PONG", "ACK", "FRAME", wait(done)])
    replacement.sequence = first.sequence  # Same boot keeps its monotonic sequence.
    receiver.handle(replacement, "esp32cam-01", "private")
    assert replacement.last("profile.set")["mode"] == "eco"
    assert len(results) == 2


def test_reconnect_waits_for_native_inference_after_transport_closed():
    entered, release = Event(), Event()

    class Slow(Detector):
        def detect(self, jpeg):
            entered.set()
            assert release.wait(3)
            return super().detect(jpeg)

    clock, mode, events, results, done, receiver = setup(detector=Slow())
    first = Socket(clock, ["HELLO", "PONG", "ACK", "FRAME", wait(entered)])
    thread = Thread(target=receiver.handle, args=(first, "esp32cam-01", "private"))
    thread.start()
    try:
        assert entered.wait(2)
        thread.join(timeout=1)
        assert not thread.is_alive() and receiver.active
        assert mode.status()["state"] == "unknown"
        worker = receiver._current_worker
        competing = Socket(clock, ["HELLO"])
        receiver.handle(competing, "esp32cam-01", "private")
        assert competing.closed and not competing.sent
    finally:
        release.set()
        thread.join(timeout=2)
    worker.join(timeout=2)
    assert not receiver.active and not results


def test_close_holds_synchronously_while_handler_receive_is_stalled():
    entered, release = Event(), Event()
    clock, mode, events, results, done, receiver = setup(detector=Detector())

    def stall(_socket):
        entered.set()
        assert release.wait(3)
        raise TimeoutError

    socket = Socket(clock, ["HELLO", "PONG", "ACK", stall])
    thread = Thread(target=receiver.handle, args=(socket, "esp32cam-01", "private"))
    thread.start()
    try:
        assert entered.wait(2)
        mode.request("tracking", mode.version, ready=True)
        assert mode.acknowledge(7, mode.version, "tracking", 100)
        for sequence in range(1, 106):
            clock.value += 100_000
            candidate = CameraFrame(7, sequence, 100 + sequence, 100 + sequence, b"x", mode.version)
            assert mode.publish_result(candidate, lambda: None)
        assert mode.status()["state"] == "active"
        receiver.close()
        assert thread.is_alive()  # The public close cannot rely on handler scheduling.
        status = mode.status()
        assert status["requested_mode"] == "eco" and status["actuation_state"] == "hold"
        version = status["version"]
    finally:
        release.set()
        thread.join(timeout=2)
    assert mode.version == version  # Handler's duplicate disconnect is idempotent.


@pytest.mark.parametrize("message", [hello(schema=True), hello(generation_floor=True),
                                    hello(generation_floor=MAX_GENERATION + 1),
                                    hello(profiles=["tracking"]), hello(extra=1),
                                    {"type": "profile.ack", "boot_id": 7, "generation": True,
                                     "mode": "eco", "applied_us": 1}])
def test_strict_profile_control_messages(message):
    with pytest.raises(ValueError):
        _text_message(json.dumps(message), profiles=True)
    with pytest.raises(ValueError):
        _text_message(json.dumps(hello()))  # Explicit opt-in only.


def test_profile_hello_timeout_without_frames():
    clock, mode, events, results, done, receiver = setup()

    def advance(_socket):
        clock.value += 5_000_000
        raise TimeoutError

    socket = Socket(clock, ["PONG", advance])
    receiver.handle(socket, "esp32cam-01", "private")
    assert any(e.get("reason") == "profile_hello_timeout" for e in events)
    assert mode.status()["actuation_state"] == "hold"


def test_real_websocket_profile_ack_and_result():
    clock, mode, events, results, done, receiver = setup(detector=Detector())
    with serve_live(receiver, "127.0.0.1", 0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.socket.getsockname()[1]
            with connect(f"ws://127.0.0.1:{port}/ws/cameras/esp32cam-01/live",
                         additional_headers={"X-API-Key": "private"}) as websocket:
                ping = json.loads(websocket.recv())
                websocket.send(json.dumps(hello()))
                websocket.send(json.dumps({"type": "clock.pong", "id": ping["id"], "boot_id": 7,
                                           "device_us": clock.value - 1_000_000}))
                command = json.loads(websocket.recv())
                assert command["type"] == "profile.set" and command["mode"] == "eco"
                websocket.send(json.dumps({**command, "type": "profile.ack", "applied_us": 500_000}))
                jpeg, stamp = image(), clock.value - 1_000_000
                websocket.send(PROFILE_HEADER.pack(b"ECF2", 7, 1, stamp, stamp,
                                                   command["generation"], len(jpeg)) + jpeg)
                assert done.wait(2)
                assert len(results) == 1
        finally:
            receiver.close()
            server.shutdown()
            thread.join(timeout=2)
