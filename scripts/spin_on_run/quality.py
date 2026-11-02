"""Exact comparison of the twelve quality fields against the golden rows."""

from __future__ import annotations

import json

from .common import QUALITY_FIELDS, ROWS_PER_RUN


def parse_line(line: str | bytes) -> dict:
    """Parse one result line keeping floats as hex strings (bit-exact, typed)."""
    value = json.loads(
        line,
        parse_float=lambda text: ("float", float(text).hex()),
        parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)),
    )
    if not isinstance(value, dict):
        raise ValueError("result line is not an object")
    return value


def strict_equal(left: object, right: object) -> bool:
    """Equality that distinguishes bool/int/float and compares floats by hex."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(strict_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            strict_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def mismatch(row: dict, golden: dict) -> str | None:
    """Name of the first differing quality field, or None."""
    for field in QUALITY_FIELDS:
        if field not in row or field not in golden or not strict_equal(row[field], golden[field]):
            return field
    return None


def load_golden(text: str) -> list[dict]:
    rows = [parse_line(line) for line in text.splitlines() if line.strip()]
    if len(rows) != ROWS_PER_RUN:
        raise ValueError("golden must have 85 rows")
    for row in rows:
        if any(field not in row for field in QUALITY_FIELDS):
            raise ValueError("golden row misses a quality field")
    return rows


def first_mismatch(rows: list[dict], golden: list[dict]) -> tuple[int, str] | None:
    """`(row index, field)` of the first difference; a short run is a mismatch too."""
    for index, expected in enumerate(golden):
        if index >= len(rows):
            return index, "<missing row>"
        field = mismatch(rows[index], expected)
        if field:
            return index, field
    return (len(golden), "<extra row>") if len(rows) > len(golden) else None
