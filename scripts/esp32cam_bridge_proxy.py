#!/usr/bin/env python3
"""Recovery bridge proxy for ESP32-CAM devices with stale flashed settings.

An ESP32-CAM bakes the server address and device API key into its firmware
at build time. When the server later moves to a new host or rotates the
API key, an un-reflashed device keeps calling the old address with the old
key and every upload fails — and without a serial cable there is no way to
see why from the device side.

This proxy restores service without touching the device: run it on the
machine whose IP the device still targets, and it forwards every connection
(HTTP and WebSocket) to the real server, replacing the stale ``X-API-Key``
header value with the current key on the way through.

Usage::

    export ESP32CAM_DEVICE_API_KEY=<current-key>
    python3 scripts/esp32cam_bridge_proxy.py \
        --upstream raspi-server.local:8080 --allow-source <device-ip>

The key is read from the environment (or ``--api-key-file``) and is never
logged or written to disk. This is a recovery tool: once the device is
re-flashed with correct settings (ideally an OTA-enabled build), stop the
proxy. See docs/remote-debugging.md for the full runbook.

Caveats:

* Always pass ``--allow-source``: the proxy replaces the ``X-API-Key`` of
  matching requests with a valid key, so any host allowed to connect can
  authenticate as a device.
* Only the first request of each TCP connection is rewritten. The retrying
  ESP32-CAM firmwares open a fresh connection per attempt, so this is fine
  for recovery, but a client that pipelines keep-alive requests would send
  its stale key on the second request.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

HEADER_LIMIT = 16384
_API_KEY_PATTERN = re.compile(rb"(?im)^(x-api-key:[ \t]*)[^\r\n]+")


def rewrite_api_key_header(buffer: bytes, api_key: bytes) -> tuple[bytes, int]:
    """Replace the value of any ``X-API-Key`` header lines in *buffer*.

    *buffer* holds the start of an HTTP request: the header block and,
    possibly, the beginning of the body. Only the header block (up to the
    first blank line) is rewritten; anything after it passes through as-is.
    Returns the rewritten bytes and the number of replacements made.
    """
    head, separator, rest = buffer.partition(b"\r\n\r\n")
    if not separator:
        return buffer, 0
    new_head, count = _API_KEY_PATTERN.subn(lambda m: m.group(1) + api_key, head)
    return new_head + separator + rest, count


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _forward_first_request(
    client_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    api_key: bytes,
) -> bool:
    """Buffer the first request's headers, rewrite the key, then forward.

    Returns ``False`` when the client disconnects before sending headers.
    """
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = await client_reader.read(4096)
        if not chunk:
            return False
        buffer += chunk
        if len(buffer) > HEADER_LIMIT:
            break  # not an HTTP header block; forward untouched

    buffer, count = rewrite_api_key_header(buffer, api_key)
    if count:
        print("[bridge] rewrote X-API-Key header", flush=True)
    upstream_writer.write(buffer)
    await upstream_writer.drain()
    return True


def _make_handler(
    upstream_host: str,
    upstream_port: int,
    api_key: bytes,
    allowed_sources: frozenset[str],
):
    async def handle(client_reader, client_writer):
        peer = client_writer.get_extra_info("peername")
        if allowed_sources and (peer is None or peer[0] not in allowed_sources):
            print(f"[bridge] rejected {peer}: not in --allow-source", flush=True)
            client_writer.close()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                upstream_host, upstream_port
            )
        except OSError as exc:
            print(f"[bridge] upstream connect failed for {peer}: {exc}", flush=True)
            client_writer.close()
            return
        print(f"[bridge] {peer} -> {upstream_host}:{upstream_port}", flush=True)
        try:
            forwarded = await _forward_first_request(client_reader, upstream_writer, api_key)
        except (ConnectionResetError, BrokenPipeError):
            forwarded = False
        if not forwarded:
            client_writer.close()
            upstream_writer.close()
            return
        await asyncio.gather(
            _pipe(client_reader, upstream_writer),
            _pipe(upstream_reader, client_writer),
        )
        print(f"[bridge] closed {peer}", flush=True)

    return handle


def _load_api_key(args: argparse.Namespace) -> bytes:
    if args.api_key_file:
        with open(args.api_key_file, encoding="utf-8") as f:
            key = f.readline().strip()
    else:
        key = os.environ.get("ESP32CAM_DEVICE_API_KEY", "").strip()
    if not key:
        sys.exit("error: no API key. Set ESP32CAM_DEVICE_API_KEY or pass --api-key-file.")
    return key.encode()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--upstream",
        required=True,
        help="real server as host:port, e.g. raspi-server.local:8080",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=8080,
        help="local port the stale device connects to (default: 8080)",
    )
    parser.add_argument(
        "--api-key-file",
        help="file whose first line is the current device API key "
        "(default: read ESP32CAM_DEVICE_API_KEY from the environment)",
    )
    parser.add_argument(
        "--allow-source",
        help="comma-separated client IPs allowed to use the bridge "
        "(strongly recommended: the proxy injects a valid API key into "
        "matching requests, so restrict it to the known device IP)",
    )
    return parser.parse_args(argv)


def parse_upstream(upstream: str) -> tuple[str, int]:
    """Split ``host``, ``host:port``, or ``[ipv6]:port`` into (host, port)."""
    host, _, port = upstream.rpartition(":")
    if not host or (host.startswith("[") and not host.endswith("]")):
        return upstream.strip("[]"), 8080  # no port given
    return host.strip("[]"), int(port)


async def _main(args: argparse.Namespace) -> None:
    host, port = parse_upstream(args.upstream)
    api_key = _load_api_key(args)
    allowed = frozenset(ip.strip() for ip in (args.allow_source or "").split(",") if ip.strip())
    server = await asyncio.start_server(
        _make_handler(host, port, api_key, allowed), "0.0.0.0", args.listen_port
    )
    print(f"[bridge] listening on 0.0.0.0:{args.listen_port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(_main(_parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
