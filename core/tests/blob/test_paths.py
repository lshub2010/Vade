"""Identifiers, on-disk paths and the two traversal routes (RFC-0004 §4.1, §8.4, A14).

The invariant under test is one sentence: **the only caller-supplied string
that reaches the filesystem is a validated 64-hex digest**, and the only other
one (`upload_id`) names a temp file after matching the id regex. `name` never
reaches a path at all.
"""

from __future__ import annotations

import hashlib

import pytest

from .images import png
from vade.blob.ids import (
    blob_path,
    blob_relpath,
    is_id,
    require_file_id,
    require_upload_id,
    thumb_path,
)
from vade.blob.ids import tmp_path as temp_path
from vade.errors import VadeError

SHA = "abcdef0123456789" * 4  # 64 hex, deterministic


@pytest.mark.parametrize(
    "value",
    [
        "blb_" + "0" * 26,
        "upl_" + "z" * 26,
        "blb_01m7bqtscdy11zhnfp5z70mw2q",
    ],
)
def test_id_shapes_the_server_accepts(value):
    assert is_id(value) is True
    if value.startswith("blb_"):
        assert require_file_id(value) == value
    else:
        assert require_upload_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "blb_" + "0" * 25,  # one short
        "blb_" + "0" * 27,  # one long
        "blb_" + "A" * 26,  # uppercase is a different id space
        "upl_" + "0" * 25 + "-",  # not base32
        "blu_" + "0" * 26,  # RFC regex typo, not an interface prefix
        "blb_" + "0" * 26 + "\n",  # `$` would let this through
        "blb/" + "0" * 26,
        "../../etc/passwd",
        "/etc/passwd",
        "blb_%2e%2e%2f" + "0" * 13,
        "",
        None,
        7,
    ],
)
def test_id_shapes_the_server_refuses(value):
    assert is_id(value) is False
    with pytest.raises(VadeError) as excinfo:
        require_file_id(value)
    # 404, never "bad format": distinguishing the two is a format oracle.
    assert excinfo.value.code == "NOT_FOUND"


def test_blob_path_is_two_levels_of_digest_only(tmp_path):
    assert blob_relpath(SHA) == f"ab/cd/{SHA}"
    path = blob_path(tmp_path, SHA)
    assert path == tmp_path.resolve() / "ab" / "cd" / SHA
    assert path.name == SHA
    assert thumb_path(tmp_path, SHA) == tmp_path.resolve() / "ab" / "cd" / f"{SHA}.thumb"


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "..",
        "/" + "a" * 63,
        "a" * 63 + "/",
        "A" * 64,
        "a" * 64 + "\n",
        "",
        None,
    ],
)
def test_path_builders_refuse_anything_but_a_digest(tmp_path, bad):
    for build in (blob_path, thumb_path):
        with pytest.raises(VadeError) as excinfo:
            build(tmp_path, bad)
        assert excinfo.value.code == "INTERNAL"


def test_tmp_path_stays_inside_the_store(tmp_path):
    safe = temp_path(tmp_path, "upl_" + "0" * 26)
    assert safe == tmp_path.resolve() / "tmp" / ("upl_" + "0" * 26 + ".part")
    assert safe.resolve().is_relative_to(tmp_path.resolve())
    for bad in ("upl_../../outside", "upl_" + "0" * 25, "blb_" + "0" * 26):
        with pytest.raises(VadeError):
            temp_path(tmp_path, bad)


def test_traversal_in_the_url_is_404(env):
    """A14: `/ui/blob/../../core.toml` and an encoded `..` never reach the disk."""
    for path in (
        "/ui/blob/../../core.toml",
        "/ui/blob/" + "blb_%2e%2e%2f" + "0" * 13,
        "/ui/blob/..%2f..%2fcore.toml",
        "/ui/blob/" + "0" * 64,
        "/ui/blob/",
    ):
        response = env.client.get(path, headers=env.headers())
        assert response.status == 404, path
        assert response.json["code"] == "NOT_FOUND", path


def test_the_display_name_never_reaches_the_path(env):
    data = png()
    digest = hashlib.sha256(data).hexdigest()
    name = "evil\r\nX-Injected: 1.png"
    done = env.upload(data, name=name)
    assert done.status == 201, done.json

    stored = env.service.store.get_blob(done.json["file_id"])
    assert stored is not None
    assert "\r" not in stored.name and "\n" not in stored.name
    # Bytes live at the digest path, not at anything derived from `name`.
    on_disk = blob_path(env.service.root, digest)
    assert on_disk.exists() and on_disk.read_bytes() == data

    response = env.client.get(f"/ui/blob/{stored.file_id}", headers=env.headers())
    assert response.status == 200
    disposition = response.header("content-disposition") or ""
    assert "\r" not in disposition and "\n" not in disposition
    assert "filename*=UTF-8''" in disposition

    second = env.upload(png(width=4, height=4), width=4, height=4, name="截图 01.png")
    assert second.status == 201, second.json
    fetched = env.client.get(f"/ui/blob/{second.json['file_id']}", headers=env.headers())
    assert "filename*=UTF-8''%E6%88%AA%E5%9B%BE%2001.png" in (
        fetched.header("content-disposition") or ""
    )
