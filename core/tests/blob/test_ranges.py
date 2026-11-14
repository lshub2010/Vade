"""Half-open interval arithmetic for resumable uploads (RFC-0004 §4.3).

Three parallel PUTs are the normal case, so progress must be an interval table,
not a high-water mark: merge overlaps, keep out-of-order writes, and answer
"what is the first byte I still need" without assuming the client starts at 0.
"""

from __future__ import annotations

import pytest

from vade.blob import chunks

SIZE = 1000


def test_normalize_sorts_merges_and_drops_empty_intervals():
    assert chunks.normalize([]) == []
    assert chunks.normalize([(500, 600), (0, 100), (100, 150)]) == [(0, 150), (500, 600)]
    # touching intervals merge (0-100 + 100-200 is one contiguous run)
    assert chunks.normalize([(0, 100), (100, 200)]) == [(0, 200)]
    # fully swallowed intervals disappear
    assert chunks.normalize([(0, 500), (100, 200), (250, 300)]) == [(0, 500)]
    # zero-length intervals carry no bytes
    assert chunks.normalize([(10, 10), (20, 30)]) == [(20, 30)]


def test_add_is_idempotent_and_does_not_assume_order():
    first = chunks.add([], 100, 200)
    assert first == [(100, 200)]
    assert chunks.add(first, 0, 100) == [(0, 200)]
    assert chunks.add(first, 150, 250) == [(100, 250)]
    assert chunks.add(chunks.add(first, 150, 250), 100, 200) == [(100, 250)]
    # a late first part merges from the left without rewriting the tail
    assert chunks.add([(1024, 2048), (2048, 3072)], 0, 1024) == [(0, 3072)]


def test_offset_next_is_the_end_of_the_contiguous_prefix():
    assert chunks.offset_next([], SIZE) == 0
    assert chunks.offset_next([(0, 400)], SIZE) == 400
    # a hole stops the cursor even though later bytes are present
    assert chunks.offset_next([(0, 400), (500, 900)], SIZE) == 400
    assert chunks.offset_next([(500, 900)], SIZE) == 0
    # out-of-range tails are clamped to the declared size
    assert chunks.offset_next([(0, 1000)], SIZE) == 1000
    assert chunks.offset_next([(0, 5000)], SIZE) == 1000


def test_gaps_describes_what_is_missing_at_both_ends():
    assert chunks.gaps([], SIZE) == [(0, SIZE)]
    assert chunks.gaps([(0, SIZE)], SIZE) == []
    assert chunks.gaps([(0, 400), (600, SIZE)], SIZE) == [(400, 600)]
    assert chunks.gaps([(400, 600)], SIZE) == [(0, 400), (600, SIZE)]
    # a gap of one byte is still a gap, and touching runs produce none
    assert chunks.gaps([(0, 999)], SIZE) == [(999, SIZE)]
    assert chunks.gaps([(0, 500), (500, SIZE)], SIZE) == []


def test_covers_demands_exact_full_coverage():
    assert chunks.covers([(0, SIZE)], SIZE) is True
    assert chunks.covers([(500, SIZE), (0, 500)], SIZE) is True
    assert chunks.covers([], SIZE) is False
    assert chunks.covers([(0, SIZE - 1)], SIZE) is False
    # one byte too many is not "complete", it is a contradictory interval table
    assert chunks.covers([(0, SIZE + 1)], SIZE) is False
    assert chunks.covers([(0, 999), (999, 1001)], SIZE) is False


@pytest.mark.parametrize(
    "ranges",
    [
        [],
        [(0, 1)],
        [(500, 700), (0, 500)],
        [(0, 300), (700, 900)],
    ],
)
def test_summary_is_consistent_with_the_primitives(ranges):
    summary = chunks.summary(ranges, SIZE)
    merged = chunks.normalize(ranges)
    assert summary["received"] == [list(pair) for pair in merged]
    assert summary["confirmed_bytes"] == chunks.confirmed_bytes(merged)
    assert summary["offset_next"] == chunks.offset_next(merged, SIZE)
    # in-range writes never confirm more bytes than the file has
    assert chunks.confirmed_bytes(merged) <= SIZE


def test_overlaps_is_half_open():
    assert chunks.overlaps([(100, 200)], 199, 300) is True
    assert chunks.overlaps([(100, 200)], 0, 100) is False  # touching, not overlapping
    assert chunks.overlaps([(100, 200)], 200, 300) is False
    assert chunks.overlaps([(100, 200), (300, 400)], 250, 350) is True
    assert chunks.overlaps([], 0, 10) is False
