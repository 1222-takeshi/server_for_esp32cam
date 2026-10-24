"""Reproduce the Rust clock fixtures from the immutable Python reference API."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script.frame_protocol import CameraFrame, ClockSample, ClockWindow  # noqa: E402

FIXTURE = ROOT / "rust/frame-worker/fixtures/clock.jsonl"


def add(window, args):
    try:
        window.add(ClockSample.from_exchange(*args))
        return True
    except ValueError:
        return False


def render():
    maximum = 2**64 - 1
    cases = [
        ([], (1, 1), 1),
        ([(1, 1_000, 5_000, 5_100)], (1, 1_050), 5_200),
        ([(1, 1_000, 5_000, 5_100)], (1, 900), 5_200),
        ([(1, 1_000, 5_000, 5_100)], (2, 1_050), 5_200),
        ([(1, 1_000, 5_000, 5_100)], (1, 2_000), 5_200),
        ([(1, 1_000, 5_000, 5_100)], (1, 1_000), 5_099),
        ([(1, 1_000, 5_000, 5_100)], (1, 1_000), 10_005_100),
        ([(1, 1_000, 5_000, 5_100)], (1, 1_000), 10_005_101),
        ([(1, 1, 1, 1), (1, 100, 200, 200)], (1, 1), 300),
        ([(1, 1_000, 5_000, 5_010), (1, 1_100, 5_020, 5_900)], (1, 1_050), 5_950),
        ([(1, 1, 1, 1), (2, 2, 2, 2)], (1, 1), 3),
        ([(1, 2, 2, 2), (2, 1, 1, 1)], (1, 2), 3),
        ([(1, i, i, i) for i in range(1, 11)], (1, 1), 11),
        ([(1, maximum, 1, 1)], (1, 1), 1),
        ([(1, 1, maximum, maximum)], (1, maximum), maximum),
        ([(maximum, maximum, maximum, maximum)], (maximum, maximum), maximum),
        ([(1, 1, 1, 1_000_001)], (1, 1), 1_000_001),
        ([(1, 1, 1, 1_000_002)], (1, 1), 1_000_002),
        ([(0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 2, 1)], (1, 1), 2),
    ]
    # Explicit arithmetic keeps fixture inputs independent of Python's PRNG API.
    seed = 558

    def choose(low, high):
        nonlocal seed
        seed = (seed * 6364136223846793005 + 1) & (2**64 - 1)
        return low + seed % (high - low + 1)
    for _ in range(32):
        device, host = choose(1, 10**12), choose(1, 10**12)
        exchanges = [(1, device + i * 50_000, host + i * 50_000,
                      host + i * 50_000 + choose(0, 1_000_000)) for i in range(9)]
        cases.append((exchanges, (1, device + choose(-100_000, 500_000)), host + 1_500_000))
    rows = []
    for exchanges, (boot, captured), now in cases:
        window = ClockWindow()
        accepted = [add(window, args) for args in exchanges]
        bounds = window.age_bounds_us(CameraFrame(boot, 1, captured, captured, b""), now)
        rows.append(json.dumps({"exchanges": exchanges, "accepted": accepted,
                                    "frame": [boot, captured], "now": now,
                                    "expected": None if bounds is None else list(map(str, bounds))},
                               separators=(",", ":")))
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Explicitly regenerate reference fixtures")
    args = parser.parse_args()
    content = render()
    if args.write:
        FIXTURE.write_text(content)
    elif FIXTURE.read_text() != content:
        raise SystemExit("Rust clock fixture differs from Python reference")
    print(f"Clock reference: {len(content.splitlines())} cases verified")
