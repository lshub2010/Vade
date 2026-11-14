"""Regression cases for the review rework (B1/B2/B3, 应4-应6).

Each case pins one finding so a later refactor cannot quietly undo it:

1. **B1 refcounts** — two devices may own the same digest; one expiring says
   nothing about the other, so the bytes may only go with the *last* row.
2. **B2 scheduling** — the periodic sweeper really fires on `gc_interval_s`
   without anyone calling `sweep()`, and `stop_sweeper()` leaves no live task.
3. **B3 ordering** — every refusal at `begin` leaves `blobs/tmp/` untouched,
   which only holds while the quota gate runs *before* the sparse file.
4. **应4 image mime** — `kind:"file"` with an `image/*` mime is still an image:
   the byte ceiling and the metadata strip apply to it too.
5. **应5 Windows unlink** — an open handle must not turn `sweep()` / `abort()`
   into a 500, and a part mid-write must not be collected under its writer.
6. **应6 bounded decode** — a PNG with a perfect IHDR and garbage IDAT is
   refused, not stored on the strength of its header.
7. **应6 (async path)** — `acomplete` answers exactly like `complete`.

Groups 2 and 7 are async, so they drive the service directly: the ASGI helpers
in `conftest` wrap `asyncio.run`, which cannot be called from a running loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import time
import zlib

import pytest

from vade.blob.auth import Principal
from vade.blob.ids import blob_path, tmp_path

from .images import PNG_SIG, png

# --------------------------------------------------------------------- helpers
FILE_QUOTA = 10_000  # tmp area is a quarter of it (2 500)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_payload(data: bytes, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "image",
        "sha256": _digest(data),
        "size": len(data),
        "mime": "image/png",
        "name": "a.png",
        "width": 8,
        "height": 8,
    }
    payload.update(extra)
    return payload


def _open_session(service, principal, data: bytes, **extra: object) -> tuple[str, dict[str, object]]:
    """begin + every part, no `complete`: the state both complete paths start from."""
    payload = _image_payload(data, **extra)
    status, body = service.begin(principal, payload)
    assert status == 201, body
    upload_id = str(body["upload_id"])
    part_size = int(body["part_size"])
    for offset in range(0, len(data), part_size):
        status, part = service.put_part(principal, upload_id, offset, data[offset : offset + part_size])
        assert status == 200, part
    return upload_id, payload


def _upload_as(env, data: bytes, token: str, **extra: object):
    """Full upload under an explicit device token (`Env.complete` uses `env.token`)."""
    payload = _image_payload(data, **extra)
    begun = env.begin(payload, token=token)
    assert begun.status == 201, begun.json
    upload_id = begun.json["upload_id"]
    part_size = int(begun.json["part_size"])
    for offset in range(0, len(data), part_size):
        part = env.put_part(upload_id, offset, data[offset : offset + part_size], token=token)
        assert part.status == 200, part.json
    previous, env.token = env.token, token
    try:
        return env.complete(upload_id, {"sha256": payload["sha256"], "size": len(data)})
    finally:
        env.token = previous


def _png_with_garbage_idat(width: int = 8, height: int = 8) -> bytes:
    """Valid signature, valid IHDR, IDAT whose bytes are not a zlib stream."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIG
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", b"\xff" * 64)  # CM=15: not deflate
        + chunk(b"IEND", b"")
    )


# ------------------------------------------------------------------ 1. B1 dedupe
def test_two_devices_sharing_a_digest_keep_the_bytes_until_the_last_row_goes(env):
    data = png()
    first = env.upload(data)
    assert first.status == 201, first.json
    sha = first.json["sha256"]
    first_id = first.json["file_id"]

    second = _upload_as(env, data, env.other_token("dev-2"))
    assert second.status == 201, second.json  # dedupe is per device, so a new row
    second_id = second.json["file_id"]
    assert second_id != first_id
    assert second.json["sha256"] == sha
    assert env.service.store.ref_count(sha) == 2

    path = blob_path(env.service.root, sha)
    assert path.exists()
    assert env.service.pin([second_id]) == [second_id]

    env.clock.advance(int(env.service.settings.blob_ttl_seconds) + 1)
    summary = env.service.sweep()
    assert summary["deleted"] >= 1
    # dev-1's row expired; dev-2 pinned the same bytes, so the file stays.
    assert env.service.store.get_blob(first_id) is None
    assert env.service.store.ref_count(sha) == 1
    assert path.exists()

    alive = env.client.get(f"/ui/blob/{second_id}", headers={"authorization": f"Bearer {env.other_token('dev-2')}"})
    assert alive.status == 200, alive.json
    assert alive.body == data

    env.service.unpin([first_id, second_id])  # first_id is already gone: no-op
    env.clock.advance(int(env.service.settings.blob_ttl_seconds) + 1)
    env.service.sweep()
    assert env.service.store.get_blob(second_id) is None
    assert env.service.store.ref_count(sha) == 0
    assert not path.exists()


# ------------------------------------------------------------------- 2. B2 scheduler
@pytest.mark.asyncio
async def test_the_periodic_sweeper_collects_and_stop_leaves_no_task(make_env):
    env = make_env(gc_interval_s=0.05)
    principal = Principal(device_id="dev-1", kind="device", max_bytes=env.service.settings.max_bytes)
    data = png()
    upload_id, payload = _open_session(env.service, principal, data)
    status, blob = env.service.complete(principal, upload_id, {"sha256": payload["sha256"], "size": len(data)})
    assert status == 201, blob
    file_id, sha = blob["file_id"], blob["sha256"]
    path = blob_path(env.service.root, sha)
    assert path.exists()

    await env.service.start_sweeper()
    task = env.service._gc_task
    assert task is not None and not task.done()

    env.clock.advance(int(env.service.settings.blob_ttl_seconds) + 1)
    deadline = time.monotonic() + 5.0
    while env.service.store.get_blob(file_id) is not None and time.monotonic() < deadline:
        await asyncio.sleep(0.02)

    assert env.service.store.get_blob(file_id) is None  # nobody called sweep()
    assert not path.exists()
    assert env.service.store.ref_count(sha) == 0

    await env.service.stop_sweeper()
    assert task.done()
    assert env.service._gc_task is None


# -------------------------------------------------------------------- 3. B3 ordering
def test_a_refused_begin_never_leaves_a_part_file_behind(make_env):
    # 1) the per-file ceiling: refused before anything is allocated
    too_big = make_env(max_bytes=FILE_QUOTA, quota_bytes=FILE_QUOTA)
    over = too_big.begin(
        {"kind": "file", "sha256": "a" * 64, "size": FILE_QUOTA + 1, "mime": "application/pdf", "name": "a.pdf"}
    )
    assert over.status == 413, over.json
    assert list((too_big.service.root / "tmp").iterdir()) == []

    # 2) the device quota: a committed blob counts, and the refusal must not
    #    create the sparse `.part` first (the file would never be reclaimed).
    quota = make_env(max_bytes=FILE_QUOTA, quota_bytes=FILE_QUOTA)
    committed = quota.upload(b"x" * 2_000, kind="file", mime="text/plain", name="a.txt")
    assert committed.status == 201, committed.json
    over = quota.begin({"kind": "file", "sha256": "b" * 64, "size": 8_001, "mime": "application/pdf", "name": "b.pdf"})
    assert over.status == 413, over.json
    assert over.json["data"]["reason"] == "quota"
    assert list((quota.service.root / "tmp").iterdir()) == []

    # 3) the temp area: one open session is enough pressure. The refused begin
    #    must add no file of its own.
    tmp_area = make_env(max_bytes=FILE_QUOTA, quota_bytes=FILE_QUOTA)
    reserved = tmp_area.begin(
        {"kind": "file", "sha256": "c" * 64, "size": 2_000, "mime": "application/pdf", "name": "c.pdf"}
    )
    assert reserved.status == 201, reserved.json
    over = tmp_area.begin(
        {"kind": "file", "sha256": "d" * 64, "size": 3_000, "mime": "application/pdf", "name": "d.pdf"}
    )
    assert over.status == 413, over.json
    assert over.json["data"]["reason"] == "tmp_quota"
    assert [p.name for p in (tmp_area.service.root / "tmp").iterdir()] == [f"{reserved.json['upload_id']}.part"]

    aborted = tmp_area.client.delete(
        f"/ui/blob/upload/{reserved.json['upload_id']}", headers=tmp_area.headers()
    )
    assert aborted.status == 204, aborted.json
    assert list((tmp_area.service.root / "tmp").iterdir()) == []


# ------------------------------------------------------------------ 4. 应4 image mime
def test_a_six_mib_png_sent_as_kind_file_still_hits_the_image_ceiling(env):
    data = PNG_SIG + b"\x00" * (6 * 1024 * 1024 - len(PNG_SIG))
    response = env.upload(data, kind="file", mime="image/png", name="big.png")
    assert response.status != 201, response.json
    assert response.status == 413, response.json
    assert response.json["data"]["limit"] == env.service.settings.image_max_bytes
    assert env.service.store.open_uploads() == []


def test_an_exif_png_sent_as_kind_file_is_still_refused(env):
    data = png(exif=True)
    response = _upload_as(env, data, env.token, kind="file", name="e.png")
    assert response.status == 501, response.json
    assert response.json["code"] == "UNSUPPORTED"
    assert response.json["data"]["field"] == "exif"


# ----------------------------------------------------------------- 5. 应5 unlink
def test_an_open_temp_handle_does_not_turn_sweep_or_abort_into_a_500(env):
    data = png()
    begun = env.begin(_image_payload(data))
    assert begun.status == 201, begun.json
    upload_id = begun.json["upload_id"]
    assert env.put_part(upload_id, 0, data).status == 200

    # abort while a writer still holds the temp file: Windows refuses the
    # unlink, and abort must still answer 204 (the row goes, the file is
    # collected by reconcile later).
    with open(tmp_path(env.service.root, upload_id), "r+b"):
        aborted = env.client.delete(f"/ui/blob/upload/{upload_id}", headers=env.headers())
    assert aborted.status == 204, aborted.json
    assert env.service.store.get_upload(upload_id) is None

    # sweep while a writer holds the temp file: same story, no exception.
    other = png(pad_idat=1)
    begun = env.begin(_image_payload(other))
    assert begun.status == 201, begun.json
    upload_id = begun.json["upload_id"]
    assert env.put_part(upload_id, 0, other).status == 200
    env.clock.advance(int(env.service.settings.idle_seconds) + 1)
    env.token = env.service.issue_token("dev-1")["token"]
    with open(tmp_path(env.service.root, upload_id), "r+b"):
        summary = env.service.sweep()  # must not raise
    assert summary["deleted"] >= 1
    assert env.service.store.get_upload(upload_id) is None


def test_an_upload_with_a_part_in_flight_is_not_swept_from_under_the_writer(env):
    data = png()
    begun = env.begin(_image_payload(data))
    assert begun.status == 201, begun.json
    upload_id = begun.json["upload_id"]
    assert env.put_part(upload_id, 0, data).status == 200
    temp = tmp_path(env.service.root, upload_id)

    env.service._inflight[upload_id] = 1  # a part is being written right now
    try:
        env.clock.advance(int(env.service.settings.idle_seconds) + 1)
        env.service.sweep()
        assert env.service.store.get_upload(upload_id) is not None
        assert temp.exists()
    finally:
        env.service._inflight.pop(upload_id, None)

    env.token = env.service.issue_token("dev-1")["token"]
    assert env.client.delete(f"/ui/blob/upload/{upload_id}", headers=env.headers()).status == 204
    assert env.service.store.get_upload(upload_id) is None


# ------------------------------------------------------------- 6. 应6 bounded decode
def test_a_png_with_a_good_header_and_garbage_idat_is_refused(env):
    response = env.upload(_png_with_garbage_idat(), mime="image/png", name="garbage.png", width=8, height=8)
    assert response.status == 501, response.json
    assert response.json["code"] == "UNSUPPORTED"
    assert response.json["data"]["field"] == "mime"
    assert env.service.store.all_blobs() == []


# -------------------------------------------------------------- 7. async complete
@pytest.mark.asyncio
async def test_acomplete_answers_exactly_like_complete(make_env):
    env = make_env()
    principal = Principal(device_id="dev-1", kind="device", max_bytes=env.service.settings.max_bytes)
    slow, slow_payload = _open_session(env.service, principal, png())
    fast, fast_payload = _open_session(env.service, principal, png(width=9, height=8), width=9, height=8)

    status_a, body_a = await env.service.acomplete(principal, slow, {"sha256": slow_payload["sha256"], "size": slow_payload["size"]})
    status_b, body_b = env.service.complete(principal, fast, {"sha256": fast_payload["sha256"], "size": fast_payload["size"]})

    assert status_a == status_b == 201
    assert set(body_a) == set(body_b)
    assert body_a["size"] == slow_payload["size"]
    assert body_b["size"] == fast_payload["size"]
    assert body_a["sha256"] == slow_payload["sha256"]
    assert body_b["sha256"] == fast_payload["sha256"]
    assert (body_a["width"], body_a["height"]) == (8, 8)
    assert (body_b["width"], body_b["height"]) == (9, 8)


# ---------------------------------------- 8. dedupe must not return a dead row
def test_a_missing_row_is_revived_when_the_same_content_is_uploaded_again(env):
    """Bytes lost → `reconcile()` marks the row → the re-upload must heal it.

    The old code answered 201 with the *stale* row, so `GET` returned 410 for a
    file whose bytes were back on disk: "re-upload to recover" silently failed.
    """
    data = png()
    first = env.upload(data, name="first.png")
    assert first.status == 201, first.json
    file_id, sha = first.json["file_id"], first.json["sha256"]
    path = blob_path(env.service.root, sha)
    assert path.exists()

    path.unlink()  # the bytes are gone (crash, operator, disk loss)
    assert env.service.reconcile() == (0, 1)
    assert env.service.store.get_blob(file_id).state == "missing"
    gone = env.client.get(f"/ui/blob/{file_id}", headers=env.headers())
    assert gone.status == 410, gone.json

    env.clock.advance(120)
    again = env.upload(data, name="revived.png")
    assert again.status == 201, again.json
    assert again.json["file_id"] == file_id  # the same row, not a second one

    # The symptom the review found: the 201 above used to point at a row `GET`
    # answered 410 for, even though the bytes were back on disk.
    fetched = env.client.get(f"/ui/blob/{file_id}", headers=env.headers())
    assert fetched.status == 200, fetched.json
    assert fetched.body == data
    assert again.json["name"] == "revived.png"

    row = env.service.store.get_blob(file_id)
    assert row.state == "ok"
    assert row.scan == "clean"
    assert row.name == "revived.png"
    assert row.expires_at == pytest.approx(env.service.now() + env.service.settings.blob_ttl_seconds)
    assert env.service.store.ref_count(sha) == 1  # reviving takes no second reference


def test_an_infected_row_is_not_revived_by_a_re_upload(env):
    """Quarantined content stays gone; the honest answer is a refusal."""
    data = png()
    first = env.upload(data)
    assert first.status == 201, first.json
    file_id, sha = first.json["file_id"], first.json["sha256"]
    path = blob_path(env.service.root, sha)

    # Exactly what `run_pending_scans` does when the scanner exits 1.
    env.service.store.set_scan(file_id, "infected")
    path.unlink()
    assert not path.exists()

    response = env.upload(data)
    assert response.status == 403, response.json
    assert response.json["code"] == "PERMISSION_DENIED"
    assert response.json["data"] == {"field": "sha256", "reason": "infected"}

    assert not path.exists()  # the bytes we just put back were dropped again
    row = env.service.store.get_blob(file_id)
    assert row.scan == "infected" and row.state == "ok"  # not revived
    assert env.service.store.open_uploads() == []  # no session left holding quota
    assert env.client.get(f"/ui/blob/{file_id}", headers=env.headers()).status == 404


# ------------------------------------------- 9. a locked temp file is not a 500
def test_a_part_that_cannot_open_its_temp_file_is_not_a_500(env):
    """`OSError` (Windows lock, replaced path) → 404, never a stack trace.

    A directory in place of the `.part` makes the open fail with an `OSError`
    on every platform (`PermissionError` on Windows, `IsADirectoryError`
    elsewhere), which is exactly the shape a concurrent abort/AV lock produces.
    """
    data = png()
    begun = env.begin(_image_payload(data))
    assert begun.status == 201, begun.json
    upload_id = begun.json["upload_id"]
    temp = tmp_path(env.service.root, upload_id)
    temp.unlink()
    temp.mkdir()

    response = env.put_part(upload_id, 0, data)

    assert response.status == 404, response.json
    assert response.json["code"] == "NOT_FOUND"
    assert response.json["data"]["field"] == "upload_id"
