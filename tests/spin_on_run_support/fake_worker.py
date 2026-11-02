"""Stand-in for frame-worker used by the dry-run tests (no model, no device)."""

import json
import os
import sys

with open(os.environ["FAKE_GOLDEN"]) as handle:
    golden = [json.loads(line) for line in handle]
mode = os.environ.get("FAKE_MODE", "ok")
spin = os.environ["ESP32CAM_RUST_BENCH_SPIN_ON_RUN"] == "1"
count = 84 if mode == "short" else 85
wall = ticks = 0
for i in range(count):
    row = dict(golden[i])
    if mode == "mismatch_b" and spin and i == 30:
        row["raw_sha256"] = "different"
    if mode == "mismatch_a" and not spin and i == 30:
        row["raw_sha256"] = "different"
    base = 150_000 + (i % 10) * 100
    infer = base if i not in (10, 11) else (150_000 if spin else 300_000)
    wall += infer + 500 + (40_000 if i else 0)
    ticks += 15
    row.update(
        {
            "infer_us": infer,
            "total_us": infer + 500,
            "wall_us": wall,
            "usage": {"cpu_ticks": ticks, "hwm_kib": 100_000, "rss_kib": 1},
            "spin_on_run": spin,
            "intra_op_spinning": spin,
            "force_spinning_stop": "1" if spin else None,
        }
    )
    print(json.dumps(row), flush=True)
if mode == "stderr":
    print("boom", file=sys.stderr)
