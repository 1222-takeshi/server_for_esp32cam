"""Check reproducible mode traces against the existing Python controller."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.frame_protocol import CameraFrame  # noqa: E402
from script.operating_mode import ModeError, OperatingMode  # noqa: E402

FIXTURE = ROOT / "rust/frame-worker/fixtures/mode.jsonl"


def event(op, **args):
    return {"op": op, **args}


def scenarios():
    hello, ack = event("hello"), event("ack")
    tracking = [hello, ack, event("request", mode="tracking"), ack]
    eco = [hello, ack, *[event("frame", at=i*5_000_000, seq=i) for i in (1, 2, 3)]]
    return [
        [event("request", mode="tracking"), event("hello", boot=0), hello, hello,
         event("request", version=0), event("request", version=10), event("request", mode="tracking", ready=False)],
        [hello, event("command"), event("command", at=999_999), event("command", at=1_000_000),
         event("ack", at=4_999_999), event("command")],
        [hello, event("ack", at=5_000_000), event("frame", seq=1)],
        [hello, event("ack", boot=2), event("ack", version=10), event("ack", applied=0), ack,
         event("ack", applied=101), event("frame", seq=1, captured=100), event("frame", seq=2), ack],
        [*tracking, event("frame", at=1_000_000, seq=1),
         event("frame", at=1_500_000, seq=2), event("frame", at=2_000_001, seq=3), ack],
        [*tracking, event("frame", at=1_000_001, seq=1)],
        [*tracking, event("burst", count=110, step=190_000), event("tick", at=21_000_000), ack],
        [*tracking, event("burst", count=49, step=200_000), event("tick", at=10_200_000)],
        [*tracking, event("frame", seq=1), event("request", mode="eco"),
         event("frame", seq=2, version=12), ack, event("frame", seq=3)],
        [*tracking, event("frame", seq=1, success=False), event("ready", value=True), ack,
         *[event("frame", at=i*5_000_000, seq=i+1) for i in (1, 2, 3)]],
        [*eco, ack, event("tick", at=22_000_001), event("frame", at=30_000_000, seq=4),
         event("frame", at=35_000_000, seq=5), event("frame", at=40_000_000, seq=6)],
        [*eco, event("tick", at=32_000_001), event("ready", value=True),
         *[event("frame", at=i*5_000_000, seq=i) for i in (7, 8, 9)]],
        [*eco, event("tick", at=32_000_001),
         *[event("frame", at=i*5_000_000, seq=i) for i in (7, 8, 9)]],
        [*eco, event("protect", reason="disk_low"), event("protect", reason="frames_expired"),
         event("ready", value=True), event("frame", at=20_000_000, seq=4),
         event("request"), ack, event("frame", at=25_000_000, seq=5)],
        [*tracking, event("frame", seq=1), event("tick", at=500_001), event("ready", value=True), ack,
         *[event("frame", at=i*5_000_000, seq=i+1) for i in (1, 2, 3)]],
        [*tracking, event("disconnect"), event("disconnect"), event("hello", boot=2, floor=100),
         event("ack", boot=2), event("frame", boot=1, seq=1), event("frame", boot=2, seq=2)],
        [event("hello", floor=2**53-1), event("disconnect")],
        [event("hello", floor=2**53-2), ack, event("request", mode="tracking"), event("disconnect")],
    ]


def apply(mode, action, time):
    args = dict(action)
    op = args.pop("op")
    time[0] = args.pop("at", time[0])
    args.setdefault("version", mode.version)
    args.setdefault("boot", 1)
    args.setdefault("mode", mode.requested if op == "ack" else "eco")
    try:
        if op == "hello":
            mode.hello(args["boot"], args.get("floor", 0))
        elif op == "ack":
            return args, mode.acknowledge(args["boot"], args["version"], args["mode"], args.get("applied", 100))
        elif op == "request":
            mode.request(args["mode"], args["version"], ready=args.get("ready", True))
        elif op in {"frame", "burst"}:
            accepted = 0
            for i in range(args.get("count", 1)):
                time[0] += args.get("step", 0)
                seq = args.get("seq", 1) + i
                frame = CameraFrame(args["boot"], seq, args.get("captured", 100+seq),
                                    args.get("captured", 100+seq), b"", args["version"])
                if not args.get("success", True):
                    def publish():
                        raise RuntimeError("test publication failure")
                else:
                    def publish():
                        return None
                accepted += mode.publish_result(frame, publish)
            return args, accepted
        elif op == "protect":
            mode.protect(args["reason"])
        elif op == "disconnect":
            mode.disconnect()
        elif op == "ready":
            mode.eco_recovery_ready = args["value"]
        elif op == "command":
            return args, mode.command()
        elif op == "tick":
            mode.tick()
        else:
            raise ValueError(op)
    except ModeError as error:
        return args, {"error": error.status}
    except RuntimeError:
        return args, {"error": 503}
    return args, None


def render():
    rows = []
    for actions in scenarios():
        time = [0]
        mode = OperatingMode(clock=lambda time=time: time[0] / 1_000_000)
        mode.version = 10
        trace = []
        for action in actions:
            at = action.get("at", time[0])
            args, result = apply(mode, action, time)
            trace.append({"op": action["op"], "at": at, "args": args,
                          "result": result, "status": mode.status()})
        rows.append(json.dumps(trace, separators=(",", ":")))
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    content = render()
    if args.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(content)
    elif FIXTURE.read_text() != content:
        raise SystemExit("Rust mode fixture differs from Python reference")
    print(f"Mode reference: {len(content.splitlines())} traces verified")
