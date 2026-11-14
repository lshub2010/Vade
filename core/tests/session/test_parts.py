"""`ui/submit` content parts: RFC-0004 §6.1 shape, §6.2 migration, §6.4 order.

These tests drive the real `SessionLog` + real `SQLiteStore` + real
`BlobService` (rows inserted directly — `stat()` is what submit calls, and it
never touches the filesystem). The point is the *ladder*: which error wins when
a message is wrong in two ways at once, because that is what a client's
`data.field` highlighting depends on.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time

import pytest

from vade.blob.ids import new_blob_id
from vade.blob.service import BlobService
from vade.blob.settings import AttachSettings
from vade.blob.store import BlobRecord
from vade.errors import VadeError
from vade.loop.engine import TurnOutcome
from vade.session.manager import SessionLog
from vade.session.models import Message
from vade.session.parts import PartLimits, parts_text
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore
from vade.transport.connection import RpcError

SESSION = "s-parts"


# --------------------------------------------------------------------- harness
async def _echo(turn, content):
    return TurnOutcome(assistant_text="ok", stopped_reason="stop")


def _log(tmp_path, *, blobs=None, limits: PartLimits | None = None) -> SessionLog:
    log = SessionLog(
        SQLiteStore(tmp_path / "sessions.db"),
        replay=ReplayBuffer(maxlen=200),
        blobs=blobs,
        limits=limits,
    )
    log.set_executor(_echo)
    log.create_session(session_id=SESSION)
    return log


def _service(tmp_path, *, read_scope: str = "core", scan_enabled: bool = False) -> BlobService:
    return BlobService(
        tmp_path / "data",
        AttachSettings(read_scope=read_scope, scan_enabled=scan_enabled),
    )


def _add_blob(
    service: BlobService,
    *,
    device_id: str = "dev-1",
    size: int = 180_000,
    mime: str = "image/jpeg",
    name: str = "IMG_0042.jpg",
    kind: str = "image",
    width: int | None = 800,
    height: int | None = 600,
    pinned: bool = False,
    expires_at: float | None = None,
    scan: str = "clean",
    state: str = "ok",
) -> BlobRecord:
    digest = hashlib.sha256(f"{new_blob_id()}".encode()).hexdigest()
    record = BlobRecord(
        file_id=new_blob_id(),
        device_id=device_id,
        sha256=digest,
        size=size,
        mime=mime,
        name=name,
        kind=kind,
        width=width if kind == "image" else None,
        height=height if kind == "image" else None,
        pinned=pinned,
        expires_at=expires_at if expires_at is not None else time.time() + 3600,
        created_at=time.time(),
        scan=scan,
        state=state,
    )
    return service.store.add_blob(record)


def _image(record: BlobRecord, **overrides) -> dict:
    part = {
        "type": "image",
        "file_id": record.file_id,
        "mime": record.mime,
        "name": record.name,
        "size": record.size,
        "width": record.width or 800,
        "height": record.height or 600,
        "sha256": record.sha256,
    }
    part.update(overrides)
    return part


def _file(record: BlobRecord, **overrides) -> dict:
    part = {
        "type": "file",
        "file_id": record.file_id,
        "mime": record.mime,
        "name": record.name,
        "size": record.size,
    }
    part.update(overrides)
    return part


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- happy paths
def test_pure_text_submit_stores_one_text_part(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        turn, pos = await log.submit(
            SESSION, [{"type": "text", "text": "这张报错是什么意思？"}], "c-1"
        )
        assert pos == 1
        assert turn.user_text == "这张报错是什么意思？"
        message = log.store.load(SESSION).messages[0]
        assert message.parts == ({"type": "text", "text": "这张报错是什么意思？"},)
        assert message.content == "这张报错是什么意思？"
        assert message.to_dict()["parts"] == [{"type": "text", "text": "这张报错是什么意思？"}]
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


def test_text_and_single_image_keep_order_and_project(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        image = _add_blob(service)
        log = _log(tmp_path, blobs=service)
        turn, _ = await log.submit(
            SESSION,
            [{"type": "text", "text": "看看这个"}, _image(image)],
            "c-1",
            device_id="dev-1",
        )
        assert turn.user_text == "看看这个\n[image: IMG_0042.jpg]"
        stored = log.store.load(SESSION).messages[0]
        assert [p["type"] for p in stored.parts] == ["text", "image"]
        assert stored.parts[1]["file_id"] == image.file_id
        # §4.7: submit pins the blob in the same breath as the message.
        assert service.store.get_blob(image.file_id).pinned is True
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


def test_pure_image_turn_is_accepted(tmp_path):
    """The old `content has no text block` refusal is gone (§2.2 / A1)."""
    async def scenario():
        service = _service(tmp_path)
        image = _add_blob(service)
        log = _log(tmp_path, blobs=service)
        turn, _ = await log.submit(SESSION, [_image(image)], "c-1", device_id="dev-1")
        assert turn.user_text == "[image: IMG_0042.jpg]"
        message = log.store.load(SESSION).messages[0]
        assert message.parts[0]["type"] == "image"
        assert message.content == "[image: IMG_0042.jpg]"
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


def test_snapshot_carries_parts_in_submitted_order(tmp_path):
    """A11: image + text + file, snapshot order == submitted order."""
    async def scenario():
        service = _service(tmp_path)
        first = _add_blob(service)
        second = _add_blob(service, name="after.png", mime="image/png")
        doc = _add_blob(
            service, kind="file", mime="application/pdf", name="spec.pdf", size=902_144,
            width=None, height=None,
        )
        log = _log(tmp_path, blobs=service)
        content = [_image(first), {"type": "text", "text": "两页的对比"}, _file(doc), _image(second)]
        await log.submit(SESSION, content, "c-1", device_id="dev-1")
        snapshot = log.snapshot(SESSION)
        parts = snapshot["messages"][0]["parts"]
        assert [p["type"] for p in parts] == ["image", "text", "file", "image"]
        assert [p.get("file_id") for p in parts] == [first.file_id, None, doc.file_id, second.file_id]
        assert snapshot["messages"][0]["content"] == parts_text(parts)
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


def test_file_part_reaches_the_model_as_a_text_hint(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        doc = _add_blob(
            service, kind="file", mime="application/pdf", name="spec.pdf", size=1024,
            width=None, height=None,
        )
        log = _log(tmp_path, blobs=service)
        await log.submit(SESSION, [{"type": "text", "text": "读一下"}, _file(doc)], "c-1")
        message = log.store.load(SESSION).messages[0]
        payload = message.to_llm()
        assert isinstance(payload["content"], list)
        assert payload["content"][0] == {"type": "text", "text": "读一下"}
        assert payload["content"][1] == {"type": "text", "text": "[file: spec.pdf · application/pdf]"}
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


# ------------------------------------------------------------------ §6.4 order
def test_unknown_part_type_reports_the_type_field(tmp_path):
    """Step 3 fires before step 4, so a bad type wins over a bogus file_id."""
    async def scenario():
        service = _service(tmp_path)
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(
                SESSION,
                [{"type": "widget", "file_id": "not-an-id"}],
                "c-1",
                device_id="dev-1",
            )
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data["field"] == "content[0].type"
        await log.close()

    _run(scenario())


def test_missing_type_on_a_plain_block_points_at_content_zero(tmp_path):
    """`[{"foo":"bar"}]` used to be refused without a pointer (§6.3 table)."""
    async def scenario():
        log = _log(tmp_path)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [{"foo": "bar"}], "c-1")
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data["field"] == "content[0].type"
        await log.close()

    _run(scenario())


def test_idempotent_retry_returns_the_turn_without_revalidating(tmp_path):
    """§6.4 step 2: idempotency before validation, or a retry invents a failure."""
    async def scenario():
        service = _service(tmp_path)
        image = _add_blob(service)
        log = _log(tmp_path, blobs=service)
        first, _ = await log.submit(SESSION, [{"type": "text", "text": "hi"}], "same")
        # The retry carries a file_id that no longer resolves: still the same turn.
        again, _ = await log.submit(
            SESSION, [_image(image, file_id="blb_" + "z" * 26)], "same", device_id="dev-1"
        )
        assert again.id == first.id
        assert len(log.store.load(SESSION).turns) == 1
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


def test_parts_count_over_the_soft_limit_is_refused_before_file_lookup(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        log = _log(tmp_path, blobs=service)
        content = [{"type": "text", "text": f"t{i}"} for i in range(11)]
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, content, "c-1")
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data["field"] == "content"
        assert exc.value.data["limit"] == 10
        await log.close()

    _run(scenario())


def test_parts_count_over_the_hard_cap_fails_as_shape(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        content = [{"type": "text", "text": f"t{i}"} for i in range(21)]
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, content, "c-1")
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data["field"] == "content"
        assert exc.value.data["limit"] == 20
        await log.close()

    _run(scenario())


def test_content_that_is_not_an_array_is_refused(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, "just text", "c-1")  # type: ignore[arg-type]
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data["field"] == "content"
        await log.close()

    _run(scenario())


def test_audio_part_is_unsupported_with_a_reason(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        clip = _add_blob(service, kind="audio", mime="audio/ogg", name="memo.ogg", width=None, height=None)
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(
                SESSION,
                [{"type": "audio", "file_id": clip.file_id, "mime": "audio/ogg",
                  "name": "memo.ogg", "size": clip.size}],
                "c-1",
            )
        assert exc.value.code == "UNSUPPORTED"
        assert exc.value.data["reason"] == "audio_disabled"
        assert exc.value.data["field"] == "content[0].type"
        await log.close()

    _run(scenario())


def test_whitespace_only_text_has_no_renderable_part(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [{"type": "text", "text": "   \n  "}], "c-1")
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data["field"] == "content"
        assert "renderable" in exc.value.message
        await log.close()

    _run(scenario())


# ----------------------------------------------------------- file_id (step 4)
def test_malformed_file_id_is_not_found_with_the_part_pointer(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(
                SESSION,
                [{"type": "text", "text": "x"}, _image(_add_blob(service), file_id="blb_short")],
                "c-1",
            )
        assert exc.value.code == "NOT_FOUND"
        assert exc.value.data["field"] == "content[1].file_id"
        await log.close()

    _run(scenario())


def test_unknown_file_id_is_not_found(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        image = _add_blob(service)
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(
                SESSION, [_image(image, file_id="blb_" + "a" * 26)], "c-1", device_id="dev-1"
            )
        assert exc.value.code == "NOT_FOUND"
        assert exc.value.data["field"] == "content[0].file_id"
        await log.close()

    _run(scenario())


def test_file_id_owned_by_another_device_is_denied(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        foreign = _add_blob(service, device_id="dev-2")
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(foreign)], "c-1", device_id="dev-1")
        assert exc.value.code == "PERMISSION_DENIED"
        assert exc.value.data["field"] == "content[0].file_id"
        assert log.store.load(SESSION).turns == ()
        await log.close()

    _run(scenario())


def test_read_scope_device_hides_another_devices_blob(tmp_path):
    async def scenario():
        service = _service(tmp_path, read_scope="device")
        foreign = _add_blob(service, device_id="dev-2")
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(foreign)], "c-1", device_id="dev-1")
        assert exc.value.code == "PERMISSION_DENIED"
        assert exc.value.data["field"] == "content[0].file_id"
        await log.close()

    _run(scenario())


def test_expired_file_id_is_not_found(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        stale = _add_blob(service, expires_at=time.time() - 60)
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(stale)], "c-1", device_id="dev-1")
        assert exc.value.code == "NOT_FOUND"
        assert exc.value.data["field"] == "content[0].file_id"
        await log.close()

    _run(scenario())


def test_pinned_blob_is_not_subject_to_ttl(tmp_path):
    """A blob already pinned by an earlier message stays attachable forever."""
    async def scenario():
        service = _service(tmp_path)
        pinned = _add_blob(service, pinned=True, expires_at=time.time() - 60)
        log = _log(tmp_path, blobs=service)
        _, pos = await log.submit(SESSION, [_image(pinned)], "c-1", device_id="dev-1")
        assert pos == 1
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


def test_missing_blob_contents_is_not_found(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        gone = _add_blob(service, state="missing")
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(gone)], "c-1", device_id="dev-1")
        assert exc.value.code == "NOT_FOUND"
        assert exc.value.data["field"] == "content[0].file_id"
        await log.close()

    _run(scenario())


def test_pending_scan_is_unsupported_not_missing(tmp_path):
    async def scenario():
        service = _service(tmp_path, scan_enabled=True)
        pending = _add_blob(service, scan="pending")
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(pending)], "c-1", device_id="dev-1")
        assert exc.value.code == "UNSUPPORTED"
        assert exc.value.data["reason"] == "scan_pending"
        assert exc.value.data["field"] == "content[0].file_id"
        await log.close()

    _run(scenario())


def test_infected_blob_is_not_found(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        bad = _add_blob(service, scan="infected")
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(bad)], "c-1", device_id="dev-1")
        assert exc.value.code == "NOT_FOUND"
        assert exc.value.data["field"] == "content[0].file_id"
        await log.close()

    _run(scenario())


def test_image_part_without_a_blob_service_is_refused(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        image = _add_blob(service)
        log = _log(tmp_path)  # attachments disabled: no blob store wired
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(image)], "c-1", device_id="dev-1")
        assert exc.value.code == "UNSUPPORTED"
        assert exc.value.data["reason"] == "attachments_disabled"
        await log.close()

    _run(scenario())


def test_attachments_disabled_refuses_image_parts(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        image = _add_blob(service)
        log = _log(tmp_path, blobs=service, limits=PartLimits(enabled=False))
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(image)], "c-1", device_id="dev-1")
        assert exc.value.code == "UNSUPPORTED"
        assert exc.value.data["reason"] == "attachments_disabled"
        # Text still works with attachments switched off.
        _, pos = await log.submit(SESSION, [{"type": "text", "text": "hi"}], "c-2")
        assert pos == 1
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


# -------------------------------------------------------------- §6.4 step 5/3
def test_declared_image_size_over_the_cap_is_payload_too_large(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        big = _add_blob(service, size=6 * 1024 * 1024)
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(big)], "c-1", device_id="dev-1")
        assert exc.value.code == "PAYLOAD_TOO_LARGE"
        assert exc.value.data["field"] == "content[0].size"
        assert exc.value.data["limit"] == 5242880
        await log.close()

    _run(scenario())


def test_existing_file_is_checked_before_its_size(tmp_path):
    """Step 4 before step 5: a 6 MiB ghost is NOT_FOUND, not too large."""
    async def scenario():
        service = _service(tmp_path)
        big = _add_blob(service, size=6 * 1024 * 1024)
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(
                SESSION, [_image(big, file_id="blb_" + "b" * 26)], "c-1", device_id="dev-1"
            )
        assert exc.value.code == "NOT_FOUND"
        await log.close()

    _run(scenario())


def test_text_over_the_budget_is_payload_too_large(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        with pytest.raises(VadeError) as exc:
            await log.submit(
                SESSION, [{"type": "text", "text": "x" * 32001}], "c-1"
            )
        assert exc.value.code == "PAYLOAD_TOO_LARGE"
        assert exc.value.data["field"] == "content[0].text"
        await log.close()

    _run(scenario())


def test_image_mime_outside_the_whitelist_is_unsupported(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        gif = _add_blob(service, mime="image/gif")
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(gif)], "c-1", device_id="dev-1")
        assert exc.value.code == "UNSUPPORTED"
        assert exc.value.data["field"] == "content[0].mime"
        await log.close()

    _run(scenario())


def test_image_dimensions_above_the_edge_cap_are_refused(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        wide = _add_blob(service, width=2049, height=100)
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [_image(wide)], "c-1", device_id="dev-1")
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data["field"] == "content[0].width"
        assert exc.value.data["limit"] == 2048
        await log.close()

    _run(scenario())


def test_missing_required_image_field_names_it(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        image = _add_blob(service)
        part = _image(image)
        part.pop("sha256")
        log = _log(tmp_path, blobs=service)
        with pytest.raises(VadeError) as exc:
            await log.submit(SESSION, [part], "c-1", device_id="dev-1")
        assert exc.value.code == "INVALID_ARGUMENT"
        assert exc.value.data["field"] == "content[0].sha256"
        await log.close()

    _run(scenario())


# --------------------------------------------------------------- §6.2 compat
def _legacy_db(path, *, content: str = "旧消息 hello") -> None:
    """A pre-RFC-0004 database: `messages` has no `parts` column."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE sessions(
                id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, title TEXT,
                seq_counter INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'open'
            );
            CREATE TABLE messages(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT,
                role TEXT NOT NULL, content TEXT NOT NULL,
                seq INTEGER NOT NULL DEFAULT 0, name TEXT, tool_call_id TEXT,
                created_at INTEGER NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO sessions(id, created_at) VALUES(?, ?)", (SESSION, 1))
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content, seq, created_at)"
            " VALUES(?,?,?,?,?,?)",
            ("m-old", SESSION, "user", content, 1, 1),
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_row_reads_back_without_a_parts_column(tmp_path):
    path = tmp_path / "sessions.db"
    _legacy_db(path)
    store = SQLiteStore(path)
    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(messages)")}
    assert "parts" in columns  # migrated on open
    session = store.load(SESSION)
    message = session.messages[0]
    assert message.parts == ()
    assert message.content == "旧消息 hello"
    # Read-time synthesis, no rewrite (§6.3).
    assert message.to_dict()["parts"] == [{"type": "text", "text": "旧消息 hello"}]
    store.close()


def test_migration_is_reentrant(tmp_path):
    path = tmp_path / "sessions.db"
    _legacy_db(path)
    for _ in range(3):  # reopening must never fail or duplicate the column
        store = SQLiteStore(path)
        rows = store._conn.execute("PRAGMA table_info(messages)").fetchall()
        assert [row[1] for row in rows].count("parts") == 1
        assert store.load(SESSION).messages[0].content == "旧消息 hello"
        store.close()


def test_legacy_to_llm_payload_is_byte_identical(tmp_path):
    path = tmp_path / "sessions.db"
    _legacy_db(path)
    store = SQLiteStore(path)
    payload = store.load(SESSION).messages[0].to_llm()
    assert payload == {"role": "user", "content": "旧消息 hello"}
    store.close()


def test_legacy_row_appears_in_the_snapshot_with_a_synthesised_part(tmp_path):
    path = tmp_path / "sessions.db"
    _legacy_db(path)
    log = SessionLog(SQLiteStore(path), replay=ReplayBuffer())
    snapshot = log.snapshot(SESSION)
    assert snapshot["messages"][0]["parts"] == [{"type": "text", "text": "旧消息 hello"}]
    assert snapshot["messages"][0]["content"] == "旧消息 hello"
    log.store.close()


def test_stored_content_equals_the_projection(tmp_path):
    """§6.2 double-write: `content` and `parts` can never disagree on disk."""
    async def scenario():
        service = _service(tmp_path)
        image = _add_blob(service)
        log = _log(tmp_path, blobs=service)
        await log.submit(SESSION, [{"type": "text", "text": "hi"}, _image(image)], "c-1")
        message = log.store.load(SESSION).messages[0]
        assert message.content == parts_text(message.parts)
        raw = log.store._conn.execute(
            "SELECT content, parts FROM messages WHERE id=?", (message.id,)
        ).fetchone()
        assert raw["content"] == parts_text(message.parts)
        assert raw["parts"] and image.file_id in raw["parts"]
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


def test_new_text_only_message_also_writes_parts(tmp_path):
    """§6.2 double-write: even a text-only message stores `parts`, not NULL.

    NULL is reserved for rows written before RFC-0004 — the two shapes must stay
    distinguishable, otherwise a future reader cannot tell "old row" from
    "message whose parts were lost".
    """
    async def scenario():
        log = _log(tmp_path)
        await log.submit(SESSION, [{"type": "text", "text": "hi"}], "c-1")
        raw = log.store._conn.execute("SELECT content, parts FROM messages").fetchone()
        assert raw["content"] == "hi"
        assert raw["parts"] == '[{"type": "text", "text": "hi"}]'
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


def test_legacy_row_keeps_a_null_parts_column(tmp_path):
    """Reading an old row must not rewrite it (§6.3: synthesised on the way out)."""
    path = tmp_path / "sessions.db"
    _legacy_db(path)
    store = SQLiteStore(path)
    assert store.load(SESSION).messages[0].to_dict()["parts"]
    raw = store._conn.execute("SELECT parts FROM messages").fetchone()
    assert raw["parts"] is None
    store.close()


# ------------------------------------------------------------------- RPC layer
class _Conn:
    device_id = "dev-1"
    connection_id = "conn-1"


def test_rpc_submit_returns_field_pointers(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        rpc = SessionRPC(log)
        ok = await rpc.dispatch(
            _Conn(), "ui/submit",
            {"session_id": SESSION, "client_msg_id": "c-1",
             "content": [{"type": "text", "text": "hi"}]},
        )
        assert isinstance(ok, dict) and ok["queue_pos"] == 1
        bad = await rpc.dispatch(
            _Conn(), "ui/submit",
            {"session_id": SESSION, "client_msg_id": "c-2", "content": [{"foo": "bar"}]},
        )
        assert isinstance(bad, RpcError)
        assert bad.code == "INVALID_ARGUMENT"
        assert bad.data == {"field": "content[0].type"}
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


def test_rpc_submit_without_content_is_a_shape_error(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        rpc = SessionRPC(log)
        bad = await rpc.dispatch(
            _Conn(), "ui/submit", {"session_id": SESSION, "client_msg_id": "c-1"}
        )
        assert isinstance(bad, RpcError)
        assert bad.code == "INVALID_ARGUMENT"
        assert bad.data == {"field": "content"}
        await log.close()

    _run(scenario())


def test_failed_emit_unpins_the_blob(tmp_path):
    """Pinned blobs never expire (§4.7), so a failed write must roll the pin back."""
    async def scenario():
        service = _service(tmp_path)
        image = _add_blob(service)
        log = _log(tmp_path, blobs=service)

        def boom(*_args, **_kwargs):
            raise RuntimeError("disk full")

        log.store.append = boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await log.submit(SESSION, [_image(image)], "c-1", device_id="dev-1")
        assert service.store.get_blob(image.file_id).pinned is False
        assert log.store.load(SESSION).messages == ()
        await log.close()

    _run(scenario())


def test_message_projection_is_the_single_source_for_user_text(tmp_path):
    """`Turn.user_text` and `Message.content` are the same projection (§6.2)."""
    async def scenario():
        service = _service(tmp_path)
        image = _add_blob(service)
        log = _log(tmp_path, blobs=service)
        turn, _ = await log.submit(SESSION, [_image(image)], "c-1", device_id="dev-1")
        message = log.store.load(SESSION).messages[0]
        assert turn.user_text == message.content == "[image: IMG_0042.jpg]"
        assert turn.user_text.strip()
        await log.wait_idle(SESSION)
        await log.close()

    _run(scenario())


def test_message_dataclass_still_accepts_plain_content():
    """`loop/` and `queue.py` construct `Message(content=...)`; that must not break."""
    message = Message(id="m", session_id="s", role="assistant", content="plain")
    assert message.parts == ()
    assert message.to_dict()["parts"] == [{"type": "text", "text": "plain"}]
    assert message.to_llm() == {"role": "assistant", "content": "plain"}
