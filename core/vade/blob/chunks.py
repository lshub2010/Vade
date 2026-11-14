"""Received-interval arithmetic for resumable uploads (RFC-0004 §4.3).

The upload session stores a list of half-open `[start, end)` byte ranges, not a
single high-water mark: parts may arrive out of order (three parallel PUTs is
the normal case), and progress must be monotonic even when a part is retried.

Every accepted part is aligned to `part_size` except possibly the last one, so
the invariants below are simple enough to state:

* ranges are sorted, disjoint and merged;
* `0 <= start < end <= size`;
* a part write must be either byte-identical to an existing part (idempotent
  retry) or completely disjoint from every existing range (no partial overlap,
  no rewriting bytes the server already accepted under a different digest).
"""

from __future__ import annotations

from typing import Iterable, Sequence

Range = tuple[int, int]

__all__ = [
    "Range",
    "normalize",
    "overlaps",
    "add",
    "confirmed_bytes",
    "offset_next",
    "summary",
    "missing_ranges",
    "covers",
    "gaps",
]


def normalize(ranges: Iterable[Sequence[int]]) -> list[Range]:
    """Sort, drop empties and merge touching/overlapping intervals."""
    items = sorted((int(a), int(b)) for a, b in ranges if int(b) > int(a))
    merged: list[Range] = []
    for start, end in items:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def overlaps(ranges: Iterable[Sequence[int]], start: int, end: int) -> bool:
    for a, b in ranges:
        if start < b and a < end:
            return True
    return False


def add(ranges: Iterable[Sequence[int]], start: int, end: int) -> list[Range]:
    return normalize(list(ranges) + [(start, end)])


def confirmed_bytes(ranges: Iterable[Sequence[int]]) -> int:
    return sum(b - a for a, b in ranges)


def offset_next(ranges: Iterable[Sequence[int]], size: int) -> int:
    """First byte the server still needs (== size when the file is complete)."""
    cursor = 0
    for start, end in normalize(ranges):
        if start > cursor:
            break
        cursor = max(cursor, end)
    return min(cursor, size)


def covers(ranges: Iterable[Sequence[int]], size: int) -> bool:
    merged = normalize(ranges)
    return bool(merged) and merged[0][0] == 0 and merged[-1][1] >= size and confirmed_bytes(merged) == size


def gaps(ranges: Iterable[Sequence[int]], size: int) -> list[Range]:
    """Missing intervals, used for `data.expected` and for progress UIs."""
    out: list[Range] = []
    cursor = 0
    for start, end in normalize(ranges):
        if start > cursor:
            out.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < size:
        out.append((cursor, size))
    return out


def missing_ranges(ranges: Iterable[Sequence[int]], size: int) -> list[Range]:
    return gaps(ranges, size)


def summary(ranges: Iterable[Sequence[int]], size: int) -> dict[str, object]:
    merged = normalize(ranges)
    return {
        "received": [[a, b] for a, b in merged],
        "confirmed_bytes": confirmed_bytes(merged),
        "offset_next": offset_next(merged, size),
    }
