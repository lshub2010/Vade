"""`ui/submit`: a `pin` that raises must hand the pending-slot claim back.

`submit` claims capacity BEFORE the write so `OVERLOADED` is authoritative
rather than a sample. The claim is consumed by `enqueue`, handed back by a
failed announcement, or handed back by `enqueue` itself — but `_pin` used to
sit OUTSIDE that guard, between `claim()` and the `try`. A pin that raised
(disk full, blob row vanished mid-pin) left the claim outstanding forever:

  * `has_capacity()` was permanently one short, so `max_pending` shrank for the
    life of the queue;
  * `is_idle()` counts claims, so `session/delete` answered `turn_active`
    forever and the session could never be removed — by any client, including
    the one that just saw the submit fail.

These tests use a real `BlobService` (rows inserted directly, exactly like
`test_parts.py`); only `pin` is made to fail, once.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest

from vade.blob.ids import new_blob_id
from vade.blob.service import BlobService
from vade.blob.settings import AttachSettings
from vade.blob.store import BlobRecord
from vade.loop.engine import TurnOutcome
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore

SESSION = "s-pin"


class PinFailsOnce(BlobService):
    """A real `BlobService` whose first `pin` raises before writing a row.

    Subclassing keeps every other path real (resolve, metadata, store) — the
    failure is exactly one layer: the pin the manager calls between its claim
    and its announcement.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pin_calls = 0

    def pin(self, file_ids, *, device_id: str | None = None) -> list[str]:
        self.pin_calls += 1
        if self.pin_calls == 1:
            raise OSError("disk full while pinning blobs")
        return super().pin(file_ids, device_id=device_id)


async def _echo(turn, content):
    return TurnOutcome(assistant_text="ok", stopped_reason="stop")


def _log(tmp_path, service: BlobService) -> SessionLog:
    log = SessionLog(
        SQLiteStore(tmp_path / "sessions.db"),
        replay=ReplayBuffer(maxlen=200),
        blobs=service,
    )
    log.set_executor(_echo)
    log.create_session(session_id=SESSION)
    return log


def _add_blob(service: BlobService) -> BlobRecord:
    record = BlobRecord(
        file_id=new_blob_id(),
        device_id="dev-1",
        sha256=hashlib.sha256(new_blob_id().encode()).hexdigest(),
        size=180_000,
        mime="image/jpeg",
        name="IMG_0042.jpg",
        kind="image",
        width=800,
        height=600,
        pinned=False,
        expires_at=time.time() + 3600,
        created_at=time.time(),
        scan="clean",
        state="ok",
    )
    return service.store.add_blob(record)


def _image(record: BlobRecord) -> dict:
    return {
        "type": "image",
        "file_id": record.file_id,
        "mime": record.mime,
        "name": record.name,
        "size": record.size,
        "width": record.width or 800,
        "height": record.height or 600,
        "sha256": record.sha256,
    }


def test_a_failed_pin_releases_the_claim_and_leaves_the_session(tmp_path) -> None:
    """The report's repro: pin raises once → no claim, idle queue, delete works."""

    async def scenario():
        service = PinFailsOnce(tmp_path / "data", AttachSettings())
        image = _add_blob(service)
        log = _log(tmp_path, service)

        with pytest.raises(OSError, match="disk full"):
            await log.submit(SESSION, [_image(image)], "c-1", device_id="dev-1")

        queue = log._queues[SESSION]
        # The claim is gone: nothing is left holding a pending slot.
        assert queue._claims == {}
        assert queue.is_idle() is True
        assert queue.has_capacity() is True
        # No turn row and no message survived the refusal.
        session = log.store.load(SESSION)
        assert session is not None
        assert session.turns == ()
        assert session.messages == ()
        # The blob never stayed pinned by the failed attempt.
        assert service.store.get_blob(image.file_id).pinned is False

        # The guard that used to answer `turn_active` forever now passes.
        await log.delete_session(SESSION)
        assert log.store.load(SESSION) is None
        assert log._queues.get(SESSION) is None

        await log.close()

    asyncio.run(scenario())


def test_submit_after_a_failed_pin_still_works(tmp_path) -> None:
    """A failed pin must not shrink the queue: the retry takes position 1."""

    async def scenario():
        service = PinFailsOnce(tmp_path / "data", AttachSettings())
        image = _add_blob(service)
        log = _log(tmp_path, service)

        with pytest.raises(OSError):
            await log.submit(SESSION, [_image(image)], "c-1", device_id="dev-1")

        # Not just "is_idle": the slot the failed submit took is really free,
        # so a second submit is not refused and lands where the first would.
        turn, pos = await log.submit(SESSION, [_image(image)], "c-2", device_id="dev-1")
        assert pos == 1
        assert service.pin_calls == 2
        assert service.store.get_blob(image.file_id).pinned is True
        assert log._queues[SESSION]._claims == {}

        await log.wait_idle(SESSION)
        await log.close()

    asyncio.run(scenario())


def test_normal_submit_still_pins_and_enqueues(tmp_path) -> None:
    """The happy path the fix must not disturb: pin, announce, run, go idle."""

    async def scenario():
        service = BlobService(tmp_path / "data", AttachSettings())
        image = _add_blob(service)
        log = _log(tmp_path, service)

        turn, pos = await log.submit(SESSION, [_image(image)], "c-1", device_id="dev-1")
        assert pos == 1
        assert service.store.get_blob(image.file_id).pinned is True
        assert log.store.load(SESSION).turns[0].id == turn.id
        assert log._queues[SESSION]._claims == {}

        await log.wait_idle(SESSION)
        queue = log._queues[SESSION]
        assert queue.is_idle() is True
        # A session that has run to completion is still deletable.
        await log.delete_session(SESSION)
        assert log.store.load(SESSION) is None

        await log.close()

    asyncio.run(scenario())
