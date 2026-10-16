# Isolated high-rate receiver (#488)

`script.capture_receiver.create_receiver` builds the core for one configured
camera, and `serve_live(receiver, host, port)` returns a standalone
`websockets.sync` server. It does not import the production application or storage.
Install the optional transport with `pip install -e ".[capture]"` (and
`.[onnx]` when using the optional ONNX detector).
The caller must supply a nonempty `device_api_key` from a private environment or
key file. The runner calls `server.serve_forever()` inside the server context
manager; Gunicorn and Flask-Sock are not used. The only HTTP
route is `/healthz`; frames enter at `/ws/cameras/<camera_id>/live` with the
`X-API-Key` header. The configured camera ID defaults to `esp32cam-01`.
The transport sets `max_size=40+128KiB`, `max_queue=(2,1)`, and disables
compression. These are high/low watermarks: reads pause at the high watermark, with bounded
overshoot from the transport read buffer. A fast sender reaches TCP
backpressure instead of growing an internal receive list without bound.

The receiver accepts the bounded ECF1 binary envelope defined in
`script.frame_protocol`. It checks JPEG headers and dimensions without decoding
pixels. A clock ping/pong exchange measures an offset interval; frames whose
capture age cannot be bounded, or whose upper age bound exceeds the default
500 ms, are dropped. The 200 ppm drift bound is an experiment assumption to
validate against device hardware. A sample expires after 10 s. Each connection
binds to one device boot ID, and sequence order persists across reconnects.

With a detector, one native inference runs in a dedicated thread and at most
one newer frame waits. A newly accepted frame replaces that waiting frame and
records an `overwritten` drop. Disconnect cancels the waiting frame and discards
the running result once the native call returns. If the event sink fails, the
receiver closes the session and refuses reconnects. Without a detector,
accepted events measure
transfer only. An injected `event_sink` receives small JSON-compatible records
of timestamps, age bounds, counters, frame IDs, and optional detections. The
sink never receives JPEG or API-key bytes. No image or event is written to disk
by default. A private runner writes bounded JSONL records outside this app.
The runner must enforce a process-wide session duration and bounded output
(the planned launcher uses 30 minutes and bounded JSONL). This is required
because the receiver's counters and message limits reset for each connection;
the receiver alone does not bound the lifetime of repeated reconnects.
The runner ends the observation with `receiver.close()` followed by
`server.shutdown()`, then closes the event sink. `close()` rejects reconnects,
cancels queued and running frames, closes the active socket (transport close
timeout: 1 s), and waits at most 200 ms for native inference. A detector that
outlives that wait can finish in
the background, but cannot emit after `close()` returns. `receiver.failed`
reports whether the event sink has failed.
The sink callback must finish promptly and must not call `receiver.close()`;
shutdown must originate from a separate monitoring thread. A sink exception
fails the receiver closed, so the monitor can end the session safely.

The session ends after 3,600 s, 100,000 frames, or 200,000 messages by default.
Only one connection may be active, including while its last inference finishes.
The application uses monotonic host microseconds; device timestamps are not
wall-clock dates. The clock ping runs every ~2 s, uses a single pending nonce,
and times out after 1 s. Rejecting ambiguous time is intentional for the
observer experiment; it must not drive motors or other actuators.
Inference results use `script.detector.Detection` with finite normalized
coordinates, score, and optional track ID. Up to 300 detections are retained
per event. `inferred` counts valid model completions; `fresh_success` counts
only results still within the capture-age limit at completion. Frames expired
in the waiting slot or during inference emit `expired_queue` or
`expired_result` drops. `sequence_gap` counts missing frames on the wire;
receiver drops after decoding still advance ordering state.
Clock records include the host receive time and device clock sample; device
status records include host receive time so cumulative capture and failure
counters can be converted into rates after the run.
