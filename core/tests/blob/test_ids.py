"""Identity and path rules (RFC-0004 §4.1 / §8.4)."""

from __future__ import annotations

import re

import pytest

from vade.blob.ids import (
    ID_RE,
    blob_path,
    blob_relpath,
    is_id,
    new_blob_id,
    new_upload_id,
    normalize_sha256,
    require_file_id,
    require_upload_id,
    sanitize_name,
    thumb_path,
)
from vade.blob.ids import tmp_path as temp_path
from vade.errors import VadeError

SHA = "ab" * 32


def test_generated_ids_match_the_contract_regex():
    for _ in range(300):
        blob = new_blob_id()
        upload = new_upload_id()
        assert re.match(r"^(?:blb|upl)_[0-9a-z]{26}$", blob) is not None
        assert re.match(r"^(?:blb|upl)_[0-9a-z]{26}$", upload) is not None
        assert blob.startswith("blb_") and upload.startswith("upl_")
        assert len(blob) == 30


def test_ids_are_unique():
    values = {new_blob_id() for _ in range(2000)}
    assert len(values) == 2000


@pytest.mark.parametrize(
    "value",
    [
        "",
        "blb_",
        "blb_" + "A" * 26,  # uppercase is not accepted
        "blb_" + "0" * 25,
        "blb_" + "0" * 27,
        "blx_" + "0" * 26,
        "../../" + "0" * 22,
        "blb_" + "0" * 25 + "/",
        "blb_" + "0" * 25 + "-",
        "blb_" + "0" * 26 + "\n",  # `$` would accept this; `\Z` does not
        None,
        42,
        b"blb_" + b"0" * 26,
    ],
)
def test_the_regex_rejects_every_other_shape(value):
    assert is_id(value) is False


def test_wrong_prefix_is_not_found_not_invalid():
    # One code for "bad shape" and "does not exist": no format oracle.
    with pytest.raises(VadeError) as excinfo:
        require_file_id("upl_" + "0" * 26)
    assert excinfo.value.code == "NOT_FOUND"
    assert excinfo.value.data == {"field": "file_id"}

    with pytest.raises(VadeError) as upload:
        require_upload_id("blb_" + "0" * 26)
    assert upload.value.code == "NOT_FOUND"


def test_blob_path_is_derived_only_from_the_digest(tmp_path):
    path = blob_path(tmp_path, SHA)
    assert path == tmp_path.resolve() / "ab" / "ab" / SHA
    assert blob_relpath(SHA) == f"ab/ab/{SHA}"
    assert thumb_path(tmp_path, SHA).name == f"{SHA}.thumb"


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "a" * 63,
        "A" * 64,
        "a" * 63 + "g",
        "a" * 63 + "/",
        "/" + "a" * 63,
        "",
    ],
)
def test_blob_path_refuses_anything_that_is_not_a_digest(tmp_path, bad):
    with pytest.raises(VadeError) as excinfo:
        blob_path(tmp_path, bad)
    assert excinfo.value.code == "INTERNAL"


def test_temp_path_cannot_escape_the_store(tmp_path):
    with pytest.raises(VadeError):
        temp_path(tmp_path, "upl_../../outside")
    safe = temp_path(tmp_path, "upl_" + "0" * 26)
    assert safe.parent.name == "tmp"
    assert safe.parent.parent == tmp_path.resolve()


def test_sha256_is_normalised_but_not_repaired():
    assert normalize_sha256("AB" * 32) == "ab" * 32
    for bad in ("ab" * 31, "zz" * 32, None, 5, "ab" * 32 + "\n"):
        with pytest.raises(VadeError) as excinfo:
            normalize_sha256(bad)
        assert excinfo.value.code == "INVALID_ARGUMENT"
        assert excinfo.value.data == {"field": "sha256"}


def test_display_name_is_stripped_and_bounded():
    assert sanitize_name("IMG_0042.jpg") == "IMG_0042.jpg"
    assert "\r" not in sanitize_name("evil\r\nX-Injected: 1.jpg")
    assert "\n" not in sanitize_name("evil\r\nX-Injected: 1.jpg")
    assert sanitize_name("a" * 400) == "a" * 255
    assert sanitize_name("../etc/passwd") == ".._etc_passwd"
    assert sanitize_name("", default="attachment") == "attachment"
    assert sanitize_name(None, default="attachment") == "attachment"


def test_contract_regex_constant_matches_the_interface():
    # RFC-0004 §4.1 names the prefixes `blb_` / `upl_` and then prints the shape
    # as `^bl[ub]_…` (which spells `blu_`). The prefixes are what the wire
    # carries, so the regex is the union of them and nothing else.
    assert ID_RE.pattern == r"^(?:blb|upl)_[0-9a-z]{26}\Z"
    assert ID_RE.match("blb_" + "0" * 26) and ID_RE.match("upl_" + "0" * 26)
    assert ID_RE.match("blu_" + "0" * 26) is None
    assert ID_RE.match("blb_" + "0" * 27) is None
    assert ID_RE.match("blb_" + "0" * 26 + "\n") is None
