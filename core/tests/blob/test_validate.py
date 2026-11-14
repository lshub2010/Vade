"""Server-side revalidation of uploaded bytes (RFC-0004 §5.3, §8.3).

`begin` only sees what the client *claims*. Everything that matters is decided
at `complete`, where the store re-measures the file, re-sniffs its magic bytes
and walks the container headers. The cases below are the four ways that gate
has to say no — and the three types it must still say yes to.
"""

from __future__ import annotations

import pytest

from .images import exe_payload, jpeg, png, webp_lossless

SHA = "ab" * 32  # 64 hex: validation never reached, the request dies first


def test_a_png_that_is_really_a_zip_is_refused_at_complete(env):
    data = b"PK\x03\x04" + b"\x00" * 60
    done = env.upload(data, mime="image/png", name="invoice.png")
    assert done.status == 400, done.json
    assert done.json["code"] == "INVALID_ARGUMENT"
    assert done.json["data"]["field"] == "mime"
    assert done.json["data"]["sniffed"] == "application/zip"
    # A refused upload leaves no row and no file behind.
    assert env.service.store.all_blobs() == []


def test_oversized_geometry_is_refused_before_a_byte_is_accepted(env, make_env):
    common = {"kind": "image", "sha256": SHA, "size": 100, "mime": "image/png", "name": "big.png"}

    too_long = env.begin({**common, "width": 4096, "height": 8})
    assert too_long.status == 413, too_long.json
    assert too_long.json["data"]["field"] == "pixels"
    assert too_long.json["data"]["limit"] == 2048
    assert env.service.store.open_uploads() == []  # nothing was reserved on disk

    # The pixel budget is a separate ceiling: raise the edge cap and a square
    # that fits the edge still blows the decode budget.
    wide = make_env(image_max_edge=8192)
    too_many = wide.begin({**common, "width": 4096, "height": 4096})
    assert too_many.status == 413, too_many.json
    assert too_many.json["data"]["field"] == "pixels"
    assert too_many.json["data"]["limit"] == 4_194_304
    assert too_many.json["data"]["actual"] == 16_777_216


@pytest.mark.parametrize(
    "payload",
    [
        exe_payload(),
        b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 56,
    ],
    ids=["pe", "elf"],
)
def test_executables_are_501_even_when_declared_as_files(env, payload):
    done = env.upload(payload, kind="file", mime="application/octet-stream", name="a.bin")
    assert done.status == 501, done.json
    assert done.json["code"] == "UNSUPPORTED"
    assert done.json["data"]["field"] == "mime"
    assert env.service.store.all_blobs() == []


@pytest.mark.parametrize(
    "mime, data",
    [
        ("image/png", png()),
        ("image/jpeg", jpeg()),
        ("image/webp", webp_lossless()),
    ],
    ids=["png", "jpeg", "webp"],
)
def test_the_three_v1_image_types_pass_revalidation(env, mime, data):
    done = env.upload(data, mime=mime)
    assert done.status == 201, done.json
    assert done.json["mime"] == mime
    assert (done.json["width"], done.json["height"]) == (8, 8)

    fetched = env.client.get(f"/ui/blob/{done.json['file_id']}", headers=env.headers())
    assert fetched.status == 200
    assert fetched.body == data
