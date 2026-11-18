"""SSE delivery is per-subscriber: no holes, no repeats (review P0-1).

The old generator sent `list(self._events)[-50:]` on *every* wake, so a burst
larger than 50 lost everything before the tail, and a quiet stream re-sent its
own history. `admin-ui/src/canvas/state/useTopology.ts` drops `seq <= appliedSeq`,
so the repeats were invisible while the holes became permanent: a dropped
`node.remove` left a ghost node on the canvas until a manual refresh.

These tests drive the real generator concurrently — two live subscribers — and
assert the wire frames, not internal bookkeeping.
"""

from __future__ import annotations

import asyncio



from vade.admin.stream import EventStream


def _ids(frames: list[str]) -> list[int]:
    out: list[int] = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("id: "):
                out.append(int(line[len("id: "):]))
    return out


def _types(frames: list[str]) -> list[str]:
    import json

    out = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):])["type"])
    return out


async def _frames(stream: EventStream, count: int, last_event_id: str | None = None,
                  *, timeout: float = 5.0) -> list[str]:
    """Read exactly `count` data frames, then close the subscription."""
    gen = stream.events(last_event_id)
    frames: list[str] = []
    try:
        while len(frames) < count:
            chunk = await asyncio.wait_for(gen.__anext__(), timeout)
            if chunk.startswith(":"):
                continue
            frames.append(chunk)
    finally:
        await gen.aclose()
    return frames


async def _drain_until_idle(stream: EventStream, last_event_id: str | None = None,
                            *, idle: float = 0.3) -> list[str]:
    """Everything the stream hands over before it goes quiet."""
    gen = stream.events(last_event_id)
    frames: list[str] = []
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(gen.__anext__(), idle)
            except asyncio.TimeoutError:
                return frames
            if not chunk.startswith(":"):
                frames.append(chunk)
    finally:
        await gen.aclose()


async def _wait_for_subscribers(stream: EventStream, count: int) -> None:
    for _ in range(200):
        if len(stream._subscribers) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"only {len(stream._subscribers)} of {count} subscribers registered")


def test_a_burst_reaches_every_subscriber_once_and_in_order():
    async def scenario():
        stream = EventStream(buffer=500, ping_s=30.0)
        first = asyncio.create_task(_frames(stream, 120, "0"))
        second = asyncio.create_task(_frames(stream, 120))
        await _wait_for_subscribers(stream, 2)

        for index in range(120):
            stream.publish("node.upsert", {"id": f"n{index}", "node": {"id": f"n{index}"}})

        one, two = await asyncio.gather(first, second)

        assert len(one) == 120, f"subscriber A got {len(one)}/120 frames"
        assert len(two) == 120, f"subscriber B got {len(two)}/120 frames"
        for name, frames in (("A", one), ("B", two)):
            ids = _ids(frames)
            assert len(ids) == 120, f"{name}: {len(ids)} ids on 120 frames"
            assert ids == sorted(set(ids)), f"{name}: ids are not strictly increasing: {ids}"

    asyncio.run(scenario())


def test_three_publishes_produce_three_frames_not_a_replayed_tail():
    async def scenario():
        stream = EventStream(buffer=500, ping_s=30.0)
        reader = asyncio.create_task(_drain_until_idle(stream, "0", idle=0.3))
        await _wait_for_subscribers(stream, 1)
        for _ in range(3):
            stream.publish("node.upsert", {"id": "n1", "node": {"id": "n1"}})
            # Let the subscriber wake once per publish: that is the shape that
            # made the old "send the last 50" loop re-send the whole tail.
            await asyncio.sleep(0.05)
        frames = await reader

        # The old code re-sent `[-50:]` on every wake: 3 publishes → 8 frames.
        ids = _ids(frames)
        assert len(frames) == 3, f"3 publishes produced {len(frames)} frames: {ids}"
        assert len(set(ids)) == 3, f"duplicate ids on 3 publishes: {ids}"
        assert ids == sorted(ids), ids

    asyncio.run(scenario())


def test_a_fresh_subscriber_is_not_handed_history_it_did_not_ask_for():
    """No `Last-Event-ID` means "from now"; only an explicit cursor replays."""

    async def scenario():
        stream = EventStream(buffer=500, ping_s=5.0)
        for _ in range(3):
            stream.publish("node.upsert", {"id": "n1", "node": {"id": "n1"}})
        assert await _drain_until_idle(stream, None, idle=0.5) == []
        # ... but an explicit cursor still gets the replay.
        replayed = await _frames(stream, 3, "0")
        assert _ids(replayed) == [1, 2, 3]

    asyncio.run(scenario())


def test_a_cursor_that_fell_out_of_the_ring_gets_a_resync_then_the_buffer():
    """A hole must be announced, not papered over with a truncated replay."""

    async def scenario():
        stream = EventStream(buffer=10, ping_s=30.0)
        for index in range(50):
            stream.publish("node.upsert", {"id": f"n{index}", "node": {"id": f"n{index}"}})

        frames = await _frames(stream, 11, "1")
        # The resync frame carries no `id:` line: Last-Event-ID must not jump
        # past the events the client is about to receive again.
        assert not _ids(frames[:1]), frames[0]
        assert _types(frames[:1]) == ["resync"], frames[0]
        assert _types(frames[1:]) == ["node.upsert"] * 10
        assert _ids(frames[1:]) == list(range(41, 51))

    asyncio.run(scenario())


def test_a_cursor_still_inside_the_ring_needs_no_resync():
    async def scenario():
        stream = EventStream(buffer=10, ping_s=30.0)
        for index in range(12):
            stream.publish("node.upsert", {"id": f"n{index}", "node": {"id": f"n{index}"}})
        # Ring holds ids 3..12; a cursor at 2 has lost nothing it can still see.
        frames = await _frames(stream, 10, "2")
        assert _types(frames) == ["node.upsert"] * 10
        assert _ids(frames) == list(range(3, 13))

    asyncio.run(scenario())
