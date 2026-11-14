"""Resumable upload over the wire: parts, gaps, resume and abort (RFC-0004 §4.3).

Three parallel PUTs are the normal case, so the server must accept parts in any
order, answer "what do you have" from an interval table, and refuse `complete`
while a hole remains. These four cases are what the mobile client depends on.
"""

from __future__ import annotations

import hashlib

from .images import png

PART = 1024
SIZE = 3072


def _image(size: int = SIZE) -> bytes:
    """A structurally valid PNG padded to exactly `size` bytes."""
    bare = png()
    data = png(pad_idat=size - len(bare))
    assert len(data) == size
    return data


def _begin(env, data: bytes):
    return env.begin(
        {
            "kind": "image",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mime": "image/png",
            "name": "x.png",
            "width": 8,
            "height": 8,
        }
    )


def test_parts_may_arrive_out_of_order_and_still_complete(env):
    data = _image()
    digest = hashlib.sha256(data).hexdigest()
    begun = _begin(env, data)
    assert begun.status == 201, begun.json
    upload_id = begun.json["upload_id"]
    assert upload_id.startswith("upl_")
    assert begun.json["part_size"] == PART
    assert begun.json["received"] == []

    # tail first, then the head, then the hole in the middle
    for offset in (2 * PART, 0, PART):
        part = env.put_part(upload_id, offset, data[offset : offset + PART])
        assert part.status == 200, part.json

    done = env.complete(upload_id, {"sha256": digest, "size": len(data)})
    assert done.status == 201, done.json
    assert done.json["file_id"].startswith("blb_")
    assert done.json["sha256"] == digest
    assert done.json["size"] == SIZE

    fetched = env.client.get(f"/ui/blob/{done.json['file_id']}", headers=env.headers())
    assert fetched.status == 200
    assert fetched.body == data  # offsets were honoured, not append order


def test_complete_refuses_a_hole_and_keeps_the_session(env):
    data = _image()
    digest = hashlib.sha256(data).hexdigest()
    begun = _begin(env, data)
    assert begun.status == 201, begun.json
    upload_id = begun.json["upload_id"]
    for offset in (0, 2 * PART):
        assert env.put_part(upload_id, offset, data[offset : offset + PART]).status == 200

    refused = env.complete(upload_id, {"sha256": digest, "size": len(data)})
    assert refused.status == 400, refused.json
    assert refused.json["code"] == "INVALID_ARGUMENT"
    assert refused.json["data"]["field"] == "received"
    assert refused.json["data"]["missing"] == [[PART, 2 * PART]]

    # The session still exists: a client that lost the answer can resume it.
    head = env.client.head(f"/ui/blob/upload/{upload_id}", headers=env.headers())
    assert head.status == 200, head.json
    assert head.json["received"] == [[0, PART], [2 * PART, SIZE]]
    assert head.json["offset_next"] == PART


def test_head_reports_bytes_not_the_high_water_mark(env):
    data = _image()
    begun = _begin(env, data)
    assert begun.status == 201, begun.json
    upload_id = begun.json["upload_id"]
    assert env.put_part(upload_id, PART, data[PART : 2 * PART]).status == 200

    head = env.client.head(f"/ui/blob/upload/{upload_id}", headers=env.headers())
    assert head.status == 200, head.json
    body = head.json
    assert body["received"] == [[PART, 2 * PART]]
    assert body["confirmed_bytes"] == PART
    assert body["offset_next"] == 0  # a hole at the front means "resume at 0"
    assert body["size"] == SIZE
    assert body["state"] == "open"
    # milliseconds on the wire, seconds in the store
    assert body["expires_at"] > int(env.clock.now * 1000)


def test_abort_is_idempotent_and_kills_the_parts(env):
    data = _image()
    begun = _begin(env, data)
    assert begun.status == 201, begun.json
    upload_id = begun.json["upload_id"]
    assert env.put_part(upload_id, 0, data[:PART]).status == 200
    temp = env.service.root / "tmp" / f"{upload_id}.part"
    assert temp.exists()

    first = env.client.delete(f"/ui/blob/upload/{upload_id}", headers=env.headers())
    assert first.status == 204
    assert first.body == b""
    assert not temp.exists()
    again = env.client.delete(f"/ui/blob/upload/{upload_id}", headers=env.headers())
    assert again.status == 204  # idempotent: unknown is still 204

    late = env.put_part(upload_id, PART, data[PART : 2 * PART])
    assert late.status == 404, late.json
    assert late.json["code"] == "NOT_FOUND"
    assert late.json["data"]["field"] == "upload_id"
    assert env.client.head(f"/ui/blob/upload/{upload_id}", headers=env.headers()).status == 404

    retry = _begin(env, data)
    assert retry.status == 201, retry.json
    assert retry.json["upload_id"] != upload_id  # a fresh session, not the dead one
