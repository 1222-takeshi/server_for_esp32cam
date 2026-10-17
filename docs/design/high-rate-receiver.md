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

## Portable bounded observer CLI (#496)

Install `.[capture]`; add `.[onnx]` for cat inference. Point firmware at the LAN
listener and use `--host` on an isolated network (default loopback). The key file
holds one ASCII token, is owner-readable only (`chmod 600`), and stays out of output/argv.

```sh
python -m script.live_observer \
  --key-file /private/path/device-key.txt \
  --output-dir /private/path/new-run \
  --host 192.168.1.221 --camera-id esp32cam-01 \
  --duration-seconds 120 --warmup-seconds 0
```

For cat inference, add `--model /private/path/cat.onnx` (threshold 0.5).
The [Pi-only trial](raspi-high-rate-recognition-results.md) used `--num-threads 3`
(1–256, default 4); the manifest
records the value actually passed to the detector. Use `--clock-source raw` on
Linux for a shared receiver/event/window/footer clock without NTP rate slewing;
unsupported platforms fail before output creation. The default `monotonic`
remains available. See [Python clock constants](https://docs.python.org/3/library/time.html#time.CLOCK_MONOTONIC_RAW).
Run the listener and model
on the Raspberry Pi for Pi-only acceptance; a Mac listener is reference only. The
output directory must be new, outside the repository, and mode 0700; its files
are mode 0600. No image or model bytes are stored. `manifest.json` records
settings, source/model SHA256, dependency versions, and windows. `events.jsonl`
stops at 100,000 records or 128 MiB; `footer.json` records the outcome, SHA256
of the closed event file, the final window snapshot, and a canonical SHA256 of
the footer (excluding its own hash). Use a trusted parent for `--output-dir`;
parents must belong to the current user or root, and non-sticky
group/world-writable parents are rejected. This check does not
protect against an adversary that can replace an ancestor directory.

The listener waits at most 180 seconds for the first **accepted** frame, outside
the measurement duration. Warmup begins there; measurement starts after
`--warmup-seconds` and lasts `--duration-seconds` (1–3600). `footer.json` records
completion, timeout, interruption, or failure and event totals. A separate
monitor closes `receiver` → `server` → writer, including on a JSONL cap. Validate
actual capture and fresh inference rates from the records.
