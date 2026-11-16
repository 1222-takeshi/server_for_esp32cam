import hashlib
from pathlib import Path

import pytest

from scripts.spin_on_run import manifest

REGISTERED = {
    "plan.md": "d0a25cbc8009135a6e1186740f105cc1ebd5ae6e5e9d35697490bafcf55c8955",
    "plan.json": "5da016646d2fb619926e9c6f07edb49fea7f30cbcaa95395ae11c3f09f13f87d",
}
REGISTERED_DIR = (
    Path.home() / ".local/share/esp32cam-training/hold-stability-573-20261028/spin-on-run-v2-plan"
)


def test_pinned_plan_hashes_are_the_registered_v2_values():
    assert manifest.PLAN == REGISTERED


@pytest.mark.parametrize("name", sorted(REGISTERED))
def test_pinned_hash_matches_the_registered_file_when_present(name):
    path = REGISTERED_DIR / name
    if not path.is_file():
        pytest.skip(f"registered plan file not present: {path}")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest.PLAN[name]
