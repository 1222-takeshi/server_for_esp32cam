"""Classify the stretched published intervals (v2 spec D8). Report only: no F/A/pass use.

Input is the evidence directory of a live run: `frames.jsonl` (R3, one record per
received frame, native monotonic clock), optionally `r2-sampler.jsonl` (R2) and
`native.json` (to place the R2 ticks on the native clock). Output is
`interval-classification.json` in the same directory.

Every interval D = published_{k+1} - published_k over 250 ms gets a main class by the
registered order arrival -> inference -> other, and the flags that matched are all
kept. `camera` and `cpu_contention` are separate flags, never a main class. The
thresholds below are fixed at registration (cause-isolation-plan section 4.1).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import common, interval_gate

STRETCHED_US = 250_000
NOMINAL_US = 200_000
ARRIVAL_SPAN_US = NOMINAL_US  # first byte -> last byte longer than the capture interval
ARRIVAL_START_GAP_US = NOMINAL_US + 100_000  # first-byte to first-byte of adjacent frames
COMPUTE_US = 200_000
QUEUE_WAIT_US = 100_000
CAMERA_GAP_US = NOMINAL_US + 50_000  # captured_us gap
CPU_WAIT_NS = 50_000_000  # summed run-queue wait of the process threads in the interval
R2_EDGE_S = 0.5  # an R2 tick further than this from the interval edge does not cover it
HOLD_ONSET_US = 500_000
FRAMES = "frames.jsonl"
R2 = "r2-sampler.jsonl"
NATIVE = "native.json"
OUTPUT = "interval-classification.json"
PRIMARY = ("arrival", "inference", "other")


def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    """Parsed objects and the count of lines that could not be read."""
    records, bad = [], 0
    with open(path) as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                bad += 1
    return records, bad


def clock_offset_us(report: object) -> float | None:
    """`native_us = pi_uptime_us + offset` from the report's host clock witness, or None.

    The witness brackets one moment on CLOCK_MONOTONIC (Pi uptime) and on the RAW clock
    whose reading at native time zero is `host_clock_epoch_ns`.
    """
    try:
        mono_low, mono_high, raw_low, raw_high = report["host_clock_witness"]["begin"]
        epoch = report["host_clock_epoch_ns"]
        if any(type(v) is not int for v in (mono_low, mono_high, raw_low, raw_high, epoch)):
            return None
        return ((raw_low + raw_high) / 2 - epoch - (mono_low + mono_high) / 2) / 1000
    except (KeyError, TypeError, ValueError):
        return None


def _completed(frame: dict) -> int | None:
    value = frame.get("last_byte_us")
    return value if value is not None else frame.get("received_us")


def _inside(value: int | None, low: int, high: int) -> bool:
    return value is not None and low <= value <= high


def _gap(first: int | None, second: int | None) -> int | None:
    return None if first is None or second is None else second - first


def _previous(frames: list[dict], key: str) -> dict[int, dict]:
    """Each frame (by id) -> the frame before it when ordered by `key`; frames lacking it skipped."""
    ordered = sorted((f for f in frames if f.get(key) is not None), key=lambda f: f[key])
    return {id(f): ordered[i - 1] for i, f in enumerate(ordered) if i}


def _stall_reasons(frames: list[dict], low: int, high: int) -> tuple[list[str], list[str]]:
    """Arrival and camera reasons from the frames whose reception ended in [low, high]."""
    before_start = _previous(frames, "first_byte_us")
    before_capture = _previous(frames, "captured_us")
    arrival: list[str] = []
    camera: list[str] = []
    for frame in frames:
        if not _inside(_completed(frame), low, high):
            continue
        sequence = frame.get("sequence")
        span = _gap(frame.get("first_byte_us"), frame.get("last_byte_us"))
        if span is not None and span > ARRIVAL_SPAN_US:
            arrival.append(f"receive_span:{sequence}:{span}")
        prior = before_start.get(id(frame))
        start_gap = _gap(prior and prior.get("first_byte_us"), frame.get("first_byte_us"))
        if start_gap is not None and start_gap > ARRIVAL_START_GAP_US:
            arrival.append(f"first_byte_gap:{sequence}:{start_gap}")
        earlier = before_capture.get(id(frame))
        capture_gap = _gap(earlier and earlier.get("captured_us"), frame.get("captured_us"))
        if capture_gap is not None and capture_gap > CAMERA_GAP_US:
            camera.append(f"capture_gap:{sequence}:{capture_gap}")
    return arrival, camera


def _inference_reasons(frames: list[dict], low: int, high: int) -> list[str]:
    """Slow compute (finished in the interval) or a long wait for the worker (begun in it)."""
    reasons = []
    for frame in frames:
        sequence = frame.get("sequence")
        compute = frame.get("compute_us")
        if compute is not None and compute >= COMPUTE_US:
            if _inside(frame.get("finished_us"), low, high):
                reasons.append(f"compute:{sequence}:{compute}")
        wait = _gap(frame.get("received_us"), frame.get("begun_us"))
        if wait is not None and wait >= QUEUE_WAIT_US and _inside(frame.get("begun_us"), low, high):
            reasons.append(f"queue_wait:{sequence}:{wait}")
    return reasons


def _tcp_context(frames: list[dict], low: int, high: int) -> dict | None:
    """The kernel's view as the frames saw it: retransmit growth and the worst timers."""
    shots = [
        f["tcp"]
        for f in sorted(frames, key=lambda f: _completed(f) or 0)
        if isinstance(f.get("tcp"), dict) and _inside(_completed(f), low, high)
    ]
    if not shots:
        return None
    totals = [s["total_retrans"] for s in shots if s.get("total_retrans") is not None]
    return {
        "total_retrans_delta": totals[-1] - totals[0] if totals else None,
        "max_lost": max(s.get("lost") or 0 for s in shots),
        "max_rto_us": max(s.get("rto_us") or 0 for s in shots),
        "max_rcv_rtt_us": max(s.get("rcv_rtt_us") or 0 for s in shots),
    }


class R2Index:
    """R2 ticks placed on the native clock; answers what happened during an interval."""

    def __init__(self, ticks: list[dict], offset_us: float | None):
        self.offset_us = offset_us
        usable = [t for t in ticks if t.get("kind") == "tick" and t.get("pi_uptime_s") is not None]
        self.ticks = sorted(usable, key=lambda t: t["pi_uptime_s"])

    def available(self) -> bool:
        return bool(self.ticks) and self.offset_us is not None

    def _bracket(self, low: int, high: int) -> list[dict]:
        """Ticks from the last one at or before `low` to the first at or after `high`."""
        edge = R2_EDGE_S * 1_000_000
        native = [(t["pi_uptime_s"] * 1_000_000 + self.offset_us, t) for t in self.ticks]
        starts = [at for at, _t in native if at <= low]
        ends = [at for at, _t in native if at >= high]
        if not starts or not ends or low - starts[-1] > edge or ends[0] - high > edge:
            return []
        return [t for at, t in native if starts[-1] <= at <= ends[0]]

    def context(self, low: int, high: int) -> dict | None:
        """Cpu wait and network/clock facts around the interval; None when not covered."""
        if not self.available():
            return None
        ticks = self._bracket(low, high)
        if len(ticks) < 2:
            return None
        freqs = [t["freq_khz"] for t in ticks if t.get("freq_khz")]
        throttled = [t["throttled"] for t in ticks if t.get("throttled") is not None]
        return {
            "sched_wait_ns": _sched_wait(ticks[0], ticks[-1]),
            "tcp_retrans_segs_delta": _delta(ticks, "snmp", "RetransSegs"),
            "tcp_ofo_queue_delta": _delta(ticks, "netstat", "TCPOFOQueue"),
            "ss_retrans_total_delta": _ss_delta(ticks),
            "ss_lastrcv_ms_max": max(_ss_values(ticks, "lastrcv_ms"), default=None),
            "freq_khz_min": min(freqs, default=None),
            "throttled_any": any(throttled) if throttled else None,
        }


def _sched_wait(first: dict, last: dict) -> int | None:
    """Run-queue wait gained by the process threads; a thread new in `last` counts in full."""
    start, end = first.get("sched"), last.get("sched")
    if not end:
        return None
    total = 0
    for tid, (_run, wait, _slices) in end.items():
        base = start[tid][1] if start and tid in start else 0
        total += max(0, wait - base)
    return total


def _delta(ticks: list[dict], key: str, name: str) -> int | None:
    values = [t[key][name] for t in ticks if t.get(key) and t[key].get(name) is not None]
    return values[-1] - values[0] if len(values) >= 2 else None


def _ss_values(ticks: list[dict], name: str) -> list[int]:
    return [
        conn[name] for t in ticks for conn in (t.get("ss") or [])[:1] if conn.get(name) is not None
    ]


def _ss_delta(ticks: list[dict]) -> int | None:
    values = _ss_values(ticks, "retrans_total")
    return values[-1] - values[0] if len(values) >= 2 else None


def classify(frames: list[dict], r2: R2Index | None = None) -> dict:
    """Every stretched interval of the published frames with its flags and main class."""
    published = sorted(
        (f for f in frames if f.get("published_us") is not None), key=lambda f: f["published_us"]
    )
    intervals = []
    for first, second in zip(published, published[1:], strict=False):
        low, high = first["published_us"], second["published_us"]
        if high - low <= STRETCHED_US:
            continue
        arrival, camera = _stall_reasons(frames, low, high)
        inference = _inference_reasons(frames, low, high)
        context = r2.context(low, high) if r2 else None
        waited = None if context is None else context["sched_wait_ns"]
        crossed = (first.get("boot_id"), first.get("generation")) != (
            second.get("boot_id"),
            second.get("generation"),
        )
        intervals.append(
            {
                "from_sequence": first.get("sequence"),
                "to_sequence": second.get("sequence"),
                "from_us": low,
                "to_us": high,
                "d_us": high - low,
                "cross_generation": crossed,
                "hold_onset": crossed and high - low > HOLD_ONSET_US,
                "primary": "arrival" if arrival else "inference" if inference else "other",
                "flags": {
                    "arrival": bool(arrival),
                    "inference": bool(inference),
                    "camera": bool(camera),
                    "cpu_contention": None if waited is None else waited >= CPU_WAIT_NS,
                },
                "reasons": [*arrival, *inference, *camera],
                "tcp": _tcp_context(frames, low, high),
                "r2": context,
            }
        )
    return {
        "intervals_gt250": len(intervals),
        "by_primary": {name: sum(1 for i in intervals if i["primary"] == name) for name in PRIMARY},
        "flag_counts": {
            "arrival": sum(i["flags"]["arrival"] for i in intervals),
            "inference": sum(i["flags"]["inference"] for i in intervals),
            "arrival_and_inference": sum(
                i["flags"]["arrival"] and i["flags"]["inference"] for i in intervals
            ),
            "camera": sum(i["flags"]["camera"] for i in intervals),
            "cpu_contention": sum(i["flags"]["cpu_contention"] is True for i in intervals),
            "cpu_contention_not_judged": sum(
                i["flags"]["cpu_contention"] is None for i in intervals
            ),
        },
        "intervals": intervals,
    }


def _split_header(records: list[dict]) -> tuple[dict | None, list[dict]]:
    if records and "records" in records[0] and "clock" in records[0]:
        return records[0], records[1:]
    return None, records


def _load_r2(root: Path) -> tuple[R2Index | None, dict]:
    if not (root / R2).exists():
        return None, {"present": False, "aligned": False, "note": "r2_missing"}
    ticks, _bad = _read_jsonl(root / R2)
    offset = None
    if (root / NATIVE).exists():
        try:
            offset = clock_offset_us(json.loads((root / NATIVE).read_text()))
        except (OSError, ValueError):
            offset = None
    index = R2Index(ticks, offset)
    note = {"present": True, "aligned": offset is not None, "clock_offset_us": offset}
    if offset is None:
        note["note"] = "no_clock_witness: cpu_contention not judged"
    return (index if index.available() else None), note


def _unclassified_count(root: Path) -> dict:
    """Without frames the stretched intervals can still be counted from native.json."""
    try:
        report = json.loads((root / NATIVE).read_text())
        window = report["measurement"]
        gaps, _ = interval_gate.normal_intervals(
            report["samples"], window["start_us"], window["end_us"]
        )
        count = sum(1 for g in gaps if g > STRETCHED_US)
    except (OSError, ValueError, KeyError, TypeError):
        count = None
    return {"intervals_gt250": count, "by_primary": None, "intervals": []}


def evaluate(evidence_dir: str | Path) -> dict:
    """The `interval-classification.json` body for an evidence directory."""
    root = Path(evidence_dir)
    out: dict = {
        "v": 1,
        "report_only": True,
        "rule": {
            "stretched_us": STRETCHED_US,
            "arrival": "receive span > 200 ms or first-byte gap > 300 ms",
            "inference": "compute >= 200 ms or received->begun wait >= 100 ms",
            "camera_flag": "captured_us gap > 250 ms",
            "cpu_contention_flag": "summed schedstat wait >= 50 ms (needs R2)",
            "order": "arrival, inference, other (flags are kept when several match)",
        },
    }
    if not (root / FRAMES).exists():
        out.update(classifiable=False, reason="frames_jsonl_missing", frames=None)
        out.update(_unclassified_count(root))
        return out
    records, bad = _read_jsonl(root / FRAMES)
    header, frames = _split_header(records)
    out["frames"] = {
        "header": header,
        "records": len(frames),
        "unreadable_lines": bad,
        "lost_records": (header or {}).get("lost_records"),
    }
    r2, out["r2"] = _load_r2(root)
    out.update(classifiable=True, reason=None, **classify(frames, r2))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="interval_classify")
    parser.add_argument("evidence_dir")
    args = parser.parse_args(argv)
    try:
        result = evaluate(args.evidence_dir)
        common.write_json(Path(args.evidence_dir) / OUTPUT, result)
    except FileExistsError:
        print(f"{OUTPUT} already exists", file=sys.stderr)
        return 2
    print(json.dumps({k: result.get(k) for k in ("classifiable", "reason", "by_primary")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
