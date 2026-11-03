"""R2: read-only sampler of the Pi during a live trial (v2 spec D6). Measurement only.

The Mac runs one ssh session whose stdin is `r2_remote.sh` (nothing is created on the
Pi), turns the marker text it prints into `r2-sampler.jsonl` and never lets an ssh
failure reach the caller: a lost or failed session is recorded as a gap. The sampler
reports its own CPU use (the remote shell's `/proc/self/stat`, children included) and
`check_budget` judges it against the design budget; an overrun is a note about the R2
data, never a verdict on the trial. Not measured on a Pi yet.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

REMOTE_SCRIPT = Path(__file__).with_name("r2_remote.sh")
OUTPUT_NAME = "r2-sampler.jsonl"
SCHEMA = 1
# Design budget: 10 % of one core (= 2.5 % of the four cores) for the whole sampler.
BUDGET_ONE_CORE = 0.10
WATCHDOG_MARGIN_S = 30
MAX_SECONDS = 4_500
HOST_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*")
IP_PATTERN = re.compile(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})")
WORD_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
TCP_FIELDS = ("RetransSegs", "InSegs", "OutSegs", "InErrs", "OutRsts")
TCPEXT_FIELDS = (
    "TCPOFOQueue",
    "TCPOFODrop",
    "TCPOFOMerge",
    "TCPLostRetransmit",
    "TCPTimeouts",
    "TCPFastRetrans",
    "TCPSlowStartRetrans",
    "TCPRcvQDrop",
    "TCPBacklogDrop",
)
TICK_KEYS = (
    "ss",
    "cpu_stat",
    "psi_cpu_some_total_us",
    "freq_khz",
    "sched",
    "snmp",
    "netstat",
    "wireless",
    "iw",
    "throttled",
    "self_cpu_ticks",
)


# --- argument validation (the values end up in a remote command line) --------------


def validate_arguments(host: str, camera_ip: str, seconds: int, process: str, interface: str):
    if not HOST_PATTERN.fullmatch(host):
        raise ValueError("invalid ssh host")
    match = IP_PATTERN.fullmatch(camera_ip)
    if not match or any(int(part) > 255 for part in match.groups()):
        raise ValueError("camera ip must be a dotted IPv4 address")
    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise ValueError(f"seconds must be 1..{MAX_SECONDS}")
    if not WORD_PATTERN.fullmatch(process) or not WORD_PATTERN.fullmatch(interface):
        raise ValueError("invalid process or interface name")


def ssh_argv(
    host: str,
    camera_ip: str,
    seconds: int,
    *,
    process: str = "frame-live",
    interface: str = "wlan0",
    ssh: str = "ssh",
    options: Iterable[str] = (),
) -> list[str]:
    """`ssh <options> <host> sh -s -- <ip> <seconds> <process> <interface>`; the script is stdin.

    Keys and agents are chosen through `options` (for example `-i path`) and are never
    echoed into the output.
    """
    validate_arguments(host, camera_ip, seconds, process, interface)
    fixed = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5"]
    remote = ["sh", "-s", "--", camera_ip, str(seconds), process, interface]
    return [ssh, *fixed, *options, host, *remote]


# --- parsing of the marker text -----------------------------------------------------


def _int(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def _float(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text)
    return float(match.group(1)) if match else None


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def parse_ss(lines: list[str]) -> list[dict] | None:
    """One dict per connection of `ss -tin`; None when ss printed nothing at all."""
    if not lines:
        return None
    connections: list[dict] = []
    for line in lines:
        if not line.strip() or line.startswith("State"):
            continue
        if not line[0].isspace():
            connections.append({"state": line.split()[0], "text": ""})
        elif connections:
            connections[-1]["text"] += " " + line
    parsed = []
    for connection in connections:
        text = connection["text"]
        retrans = re.search(r"retrans:(\d+)/(\d+)", text)
        parsed.append(
            {
                "state": connection["state"],
                "retrans_now": int(retrans.group(1)) if retrans else None,
                "retrans_total": int(retrans.group(2)) if retrans else None,
                "lost": _int(text, r"\blost:(\d+)"),
                "rto_ms": _float(text, r"\brto:([\d.]+)"),
                "rcv_rtt_ms": _float(text, r"\brcv_rtt:([\d.]+)"),
                "lastrcv_ms": _int(text, r"\blastrcv:(\d+)"),
            }
        )
    return parsed


def parse_table(lines: list[str], prefix: str, wanted: Iterable[str]) -> dict | None:
    """`/proc/net/snmp` and `netstat`: a `Prefix: names` line followed by `Prefix: values`."""
    header = None
    for line in lines:
        if not line.startswith(prefix + ":"):
            continue
        fields = line.split()[1:]
        if header is None:
            header = fields
            continue
        row = dict(zip(header, fields, strict=False))
        return {name: _to_int(row.get(name)) for name in wanted}
    return None


def parse_wireless(lines: list[str]) -> dict | None:
    """First interface of `/proc/net/wireless`: link quality, signal level, noise."""
    for line in lines:
        if ":" not in line or "|" in line:
            continue
        name, _, rest = line.partition(":")
        values = [value.rstrip(".") for value in rest.split()]
        if len(values) >= 4:
            return {
                "interface": name.strip(),
                "link": _to_float(values[1]),
                "level_dbm": _to_float(values[2]),
                "noise_dbm": _to_float(values[3]),
            }
    return None


def parse_iw(lines: list[str]) -> dict | None:
    if not lines:
        return None
    text = "\n".join(lines)
    return {
        "signal_dbm": _float(text, r"signal:\s*(-?[\d.]+)"),
        "tx_retries": _int(text, r"tx retries:\s*(\d+)"),
        "tx_failed": _int(text, r"tx failed:\s*(\d+)"),
        "tx_bitrate_mbps": _float(text, r"tx bitrate:\s*([\d.]+)"),
    }


def parse_sched(lines: list[str]) -> dict | None:
    """Per-thread `schedstat` (run ns, run-queue wait ns, timeslices); cumulative values."""
    threads = {}
    for line in lines:
        parts = line.split()
        if len(parts) == 4 and all(part.isdigit() for part in parts):
            threads[parts[0]] = [int(parts[1]), int(parts[2]), int(parts[3])]
    return threads or None


def parse_self(lines: list[str]) -> list[int] | None:
    """utime, stime, cutime, cstime (clock ticks) of the remote shell and its reaped children."""
    if not lines:
        return None
    try:
        # comm is in parentheses and may contain spaces: split after the last ')'.
        fields = lines[0].rpartition(")")[2].split()
        return [int(fields[11]), int(fields[12]), int(fields[13]), int(fields[14])]
    except (IndexError, ValueError):
        return None


def parse_tick(sections: dict[str, list[str]]) -> dict:
    """The measurement part of one tick; every key is present and null when not measured."""
    stat = sections.get("stat")
    psi = " ".join(sections.get("psi", []))
    freq = sections.get("freq")
    throttled = " ".join(sections.get("throttled", []))
    cpu = None
    if stat and stat[0].startswith("cpu "):
        cpu = [v for v in (_to_int(x) for x in stat[0].split()[1:]) if v is not None] or None
    snmp, netstat = sections.get("snmp"), sections.get("netstat")
    hex_value = re.search(r"throttled=(0x[0-9a-fA-F]+)", throttled)
    return {
        "ss": parse_ss(sections["ss"]) if "ss" in sections else None,
        "cpu_stat": cpu,
        "psi_cpu_some_total_us": _int(psi, r"some .*total=(\d+)"),
        "freq_khz": _to_int(freq[0]) if freq else None,
        "sched": parse_sched(sections.get("sched", [])),
        "snmp": parse_table(snmp, "Tcp", TCP_FIELDS) if snmp else None,
        "netstat": parse_table(netstat, "TcpExt", TCPEXT_FIELDS) if netstat else None,
        "wireless": parse_wireless(sections.get("wireless", [])),
        "iw": parse_iw(sections.get("iw", [])),
        "throttled": int(hex_value.group(1), 16) if hex_value else None,
        "self_cpu_ticks": parse_self(sections.get("self", [])),
    }


# --- budget ----------------------------------------------------------------------------


def budget(first: tuple[float, list[int]] | None, last: tuple[float, list[int]] | None, hz: int):
    """CPU use of the whole sampler as a fraction of one core, from two `self` samples."""
    if first is None or last is None or last[0] <= first[0] or hz <= 0:
        return {"limit": BUDGET_ONE_CORE, "exceeded": None, "one_core_fraction": None}
    ticks = sum(last[1]) - sum(first[1])
    elapsed = last[0] - first[0]
    fraction = ticks / hz / elapsed
    return {
        "limit": BUDGET_ONE_CORE,
        "cpu_ticks": ticks,
        "clk_tck": hz,
        "elapsed_s": round(elapsed, 3),
        "one_core_fraction": round(fraction, 5),
        "exceeded": fraction > BUDGET_ONE_CORE,
    }


def check_budget(path: str | os.PathLike) -> dict:
    """Judge a saved `r2-sampler.jsonl` against the budget (report only)."""
    first = last = None
    hz = 100
    try:
        with open(path) as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("kind") == "hello":
                    hz = record.get("clk_tck") or hz
                ticks = record.get("self_cpu_ticks") if record.get("kind") == "tick" else None
                if ticks and record.get("pi_uptime_s") is not None:
                    last = (record["pi_uptime_s"], ticks)
                    first = first or last
    except OSError as error:
        return {"limit": BUDGET_ONE_CORE, "exceeded": None, "error": type(error).__name__}
    return budget(first, last, hz)


# --- the session --------------------------------------------------------------------------


class _Writer:
    def __init__(self, handle):
        self.handle = handle

    def write(self, record: dict) -> None:
        self.handle.write(json.dumps(record, sort_keys=True) + "\n")
        self.handle.flush()


def run(
    argv: list[str],
    out_dir: str | os.PathLike,
    *,
    host: str,
    camera_ip: str,
    seconds: int,
    script: str | None = None,
    mono_ns: Callable[[], int] = time.monotonic_ns,
    popen: Callable = subprocess.Popen,
) -> dict:
    """Run one session and write `r2-sampler.jsonl` (new file only). Never raises on ssh trouble.

    Returns the final summary (also the last line of the file). A spawn failure, a
    non-zero ssh exit, a vanished session or a stalled one become gap records.
    """
    text = script if script is not None else REMOTE_SCRIPT.read_text()
    fd = os.open(Path(out_dir) / OUTPUT_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        out = _Writer(handle)
        state = {"ticks": 0, "hz": 100, "first": None, "last": None, "gap": None, "ended": False}
        out.write(
            {
                "kind": "header",
                "v": SCHEMA,
                "clock": "pi_uptime_s and mac_monotonic_ns",
                "host": host.rpartition("@")[2],
                "camera_ip": camera_ip,
                "seconds": seconds,
                "nominal_interval_ms": 100,
                "tick_keys": list(TICK_KEYS),
            }
        )
        code = _session(argv, text, seconds, out, state, mono_ns, popen)
        summary = {
            "kind": "end",
            "ssh_exit": code,
            "ticks": state["ticks"],
            "gap": state["gap"],
            "budget": budget(state["first"], state["last"], state["hz"]),
        }
        out.write(summary)
    return summary


def _session(argv, text, seconds, out, state, mono_ns, popen) -> int | None:
    try:
        process = popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, ValueError) as error:
        state["gap"] = f"spawn_failed:{type(error).__name__}"
        out.write({"kind": "gap", "mono_ns": mono_ns(), "reason": state["gap"]})
        return None
    watchdog = threading.Timer(seconds + WATCHDOG_MARGIN_S, _kill, args=(process,))
    watchdog.daemon = True
    watchdog.start()
    try:
        try:
            process.stdin.write(text)
            process.stdin.close()
        except (OSError, ValueError):
            pass  # a dead session is reported by the exit code below
        _consume(process.stdout, out, state, mono_ns)
        code = process.wait()
    except KeyboardInterrupt:
        _kill(process)
        state["gap"] = "interrupted"
        code = process.poll()
    finally:
        watchdog.cancel()
    if state["gap"] is None and (code != 0 or not state["ended"]):
        state["gap"] = f"ssh_exit_{code}" if code != 0 else "stream_ended_early"
        out.write({"kind": "gap", "mono_ns": mono_ns(), "reason": state["gap"]})
    return code


def _kill(process) -> None:
    with contextlib.suppress(OSError):
        process.kill()


def _consume(stream, out: _Writer, state: dict, mono_ns: Callable[[], int]) -> None:
    """Group the marker text into ticks and write one record per tick."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    head: dict | None = None

    def flush() -> None:
        if head is None:
            return
        record = {**head, **parse_tick(sections)}
        out.write(record)
        state["ticks"] += 1
        if record["self_cpu_ticks"] and record["pi_uptime_s"] is not None:
            state["last"] = (record["pi_uptime_s"], record["self_cpu_ticks"])
            state["first"] = state["first"] or state["last"]

    for raw in stream:
        line = raw.rstrip("\n")
        if not line.startswith("@"):
            if current is not None:
                current.append(line)
            continue
        name, _, argument = line[1:].partition(" ")
        if name == "tick":
            flush()
            index, _, uptime = argument.partition(" ")
            head = {
                "kind": "tick",
                "mono_ns": mono_ns(),
                "n": _to_int(index),
                "pi_uptime_s": _to_float(uptime),
            }
            sections, current = {}, None
        elif name == "hello":
            state["hz"] = _to_int(argument.split()[-1]) or 100
            out.write({"kind": "hello", "clk_tck": state["hz"], "mono_ns": mono_ns()})
        elif name == "end":
            flush()
            head, current = None, None
            state["ended"] = True
        elif head is not None:
            sections[name] = [argument] if name == "freq" else []
            current = sections[name]
    flush()


# --- command line (registered by cli.py as `r2`) ----------------------------------------


def command(args: argparse.Namespace) -> int:
    """Run the sampler, or with --check judge a saved file.

    A lost or failed ssh session is a gap in the data, never a failure here; only a
    bad argument or an existing output file ends with 2.
    """
    out = Path(args.out)
    if args.check:
        print(json.dumps(check_budget(out / OUTPUT_NAME)))
        return 0
    try:
        argv = ssh_argv(
            args.host,
            args.camera_ip,
            args.seconds,
            process=args.process,
            interface=args.interface,
            ssh=args.ssh,
            options=args.ssh_option,
        )
        out.mkdir(parents=True, exist_ok=True)
        summary = run(argv, out, host=args.host, camera_ip=args.camera_ip, seconds=args.seconds)
    except (ValueError, FileExistsError) as error:
        print(f"r2: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps({k: summary[k] for k in ("ticks", "gap", "ssh_exit", "budget")}))
    return 0


def add_subcommand(sub) -> None:
    r2 = sub.add_parser("r2", help="read-only Pi sampler over ssh (measurement only)")
    r2.add_argument("host", help="ssh host (user@name); keys come from --ssh-option")
    r2.add_argument("camera_ip")
    r2.add_argument("out", help="evidence directory; r2-sampler.jsonl is created in it")
    r2.add_argument("--seconds", type=int, required=True)
    r2.add_argument("--process", default="frame-live")
    r2.add_argument("--interface", default="wlan0")
    r2.add_argument("--ssh", default="ssh")
    r2.add_argument("--ssh-option", action="append", default=[])
    r2.add_argument("--check", action="store_true", help="judge the sampler budget of out/")
    r2.set_defaults(handler=command)
