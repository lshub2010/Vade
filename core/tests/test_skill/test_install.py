"""Install path: preview token, pending quarantine, approve, rollback, zip safety.

The install tests double as the "zero execution" proof: every rejected archive
is checked for leaked files, and every installed file is checked for exec bits.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import zipfile
from pathlib import Path

import pytest

from vade.errors import VadeError
from vade.skill.loader import MAX_SKILL_FILE_BYTES, InstallSource, SkillLoader

from .helpers import make_loader, skill_text, write_skill, zip_bytes, zip_with_skill


def _text(name: str = "kb-guide", **kwargs) -> str:
    return skill_text(name, **kwargs)


def test_preview_install_lands_in_pending(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    preview = loader.preview(InstallSource("text", text=_text()))
    assert preview.valid is True
    assert preview.conflict == ""
    assert len(preview.preview_token) == 64
    state = loader.install(InstallSource("text", text=_text()), preview.preview_token)
    assert state.status == "pending" and state.origin == "install"
    assert (tmp_path / "pending" / "kb-guide" / "SKILL.md").is_file()
    assert loader.list_all() == []
    assert [s.name for s in loader.list_all(include_pending=True)] == ["kb-guide"]
    assert loader.get("kb-guide") is None


def test_install_requires_a_valid_one_time_token(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    source = InstallSource("text", text=_text())
    with pytest.raises(VadeError) as excinfo:
        loader.install(source, "")
    assert excinfo.value.code == "INVALID_ARGUMENT"

    token = loader.preview(source).preview_token
    loader.install(source, token)
    with pytest.raises(VadeError) as excinfo:
        loader.install(source, token)  # replay
    assert excinfo.value.code == "INVALID_ARGUMENT"


def test_token_is_bound_to_the_content(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    token = loader.preview(InstallSource("text", text=_text())).preview_token
    changed = _text(description="different description")
    with pytest.raises(VadeError) as excinfo:
        loader.install(InstallSource("text", text=changed), token)
    assert "does not match" in excinfo.value.message
    assert not (tmp_path / "pending" / "kb-guide").exists()


def test_token_expires(tmp_path: Path) -> None:
    now = {"t": 1000.0}
    loader = SkillLoader(tmp_path, tool_snapshot=[], clock=lambda: now["t"])
    loader.reload()
    source = InstallSource("text", text=_text())
    token = loader.preview(source).preview_token
    now["t"] += 61
    with pytest.raises(VadeError) as excinfo:
        loader.install(source, token)
    assert "expired" in excinfo.value.message


def test_invalid_skill_gets_no_token(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    preview = loader.preview(InstallSource("text", text=_text(sections={"When to Use": "a"})))
    assert preview.valid is False
    assert preview.preview_token == ""
    assert "Procedure" in preview.missing_sections


def test_conflict_is_reported(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    source = InstallSource("text", text=_text())
    loader.install(source, loader.preview(source).preview_token)
    assert loader.preview(source).conflict == "pending"
    loader.approve("kb-guide")
    assert loader.preview(source).conflict == "active"


def test_approve_activates_and_writes_history(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    source = InstallSource("text", text=_text())
    loader.install(source, loader.preview(source).preview_token)
    state = loader.approve("kb-guide")
    assert state.status == "active" and state.revision == 1
    assert (tmp_path / "kb-guide" / "SKILL.md").is_file()
    assert (tmp_path / ".history" / "kb-guide" / "1.md").is_file()
    assert not (tmp_path / "pending" / "kb-guide").exists()
    assert "kb-guide" in loader.render(_parts("@kb-guide")).activated_skills  # same process


def test_update_never_touches_the_active_copy(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    v1 = _text(description="version one")
    loader.install(InstallSource("text", text=v1), loader.preview(InstallSource("text", text=v1)).preview_token)
    loader.approve("kb-guide")
    before = (tmp_path / "kb-guide" / "SKILL.md").read_bytes()
    v2 = _text(description="version two")
    loader.install(InstallSource("text", text=v2), loader.preview(InstallSource("text", text=v2)).preview_token)
    assert (tmp_path / "kb-guide" / "SKILL.md").read_bytes() == before
    state = loader.approve("kb-guide")
    assert state.revision == 2 and state.description == "version two"


def test_each_install_stamps_its_own_pending_revision(tmp_path: Path) -> None:
    """P1-1: the pending revision identifies bytes, it is not the active one.

    Two installs of one name used to carry the same revision (the active one),
    so `approve(revision=N)` stopped naming a proposal and the admin gate let
    the later, unreviewed bytes through.
    """
    loader = make_loader(tmp_path)
    for description, expected in (("one", 0), ("two", 1)):
        text = _text(description=description)
        state = loader.install(InstallSource("text", text=text),
                               loader.preview(InstallSource("text", text=text)).preview_token)
        assert state.status == "pending"
        assert state.revision == expected

    # The stamp is in meta.json, so a restart reports the same identity.
    restarted = make_loader(tmp_path)
    assert restarted.pending("kb-guide").revision == 1
    approved = restarted.approve("kb-guide")
    assert approved.revision == 1 and approved.description == "two"


def test_approve_and_reject_require_the_revision_they_were_shown(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    first = _text(description="first")
    loader.install(InstallSource("text", text=first),
                   loader.preview(InstallSource("text", text=first)).preview_token)
    assert loader.approve("kb-guide").revision == 1

    for description in ("second", "third"):
        text = _text(description=description)
        loader.install(InstallSource("text", text=text),
                       loader.preview(InstallSource("text", text=text)).preview_token)
    assert loader.pending("kb-guide").revision == 3

    with pytest.raises(VadeError) as excinfo:
        loader.approve("kb-guide", expect_revision=1)      # the active revision
    assert excinfo.value.code == "TOOL_CONFLICT"
    with pytest.raises(VadeError) as excinfo:
        loader.reject("kb-guide", "stale", expect_revision=2)   # superseded proposal
    assert excinfo.value.code == "TOOL_CONFLICT"
    assert loader.pending("kb-guide") is not None, "a refused CAS must not move anything"
    assert loader.get("kb-guide").description == "first"

    promoted = loader.approve("kb-guide", expect_revision=3)
    assert promoted.revision == 3 and promoted.description == "third"

    with pytest.raises(VadeError) as excinfo:
        loader.reject("kb-guide", "again", expect_revision=3)   # already approved
    assert excinfo.value.code == "NOT_FOUND"


def test_reject_with_the_shown_revision_moves_that_proposal(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    for description in ("second", "third"):
        text = _text(description=description)
        loader.install(InstallSource("text", text=text),
                       loader.preview(InstallSource("text", text=text)).preview_token)

    loader.reject("kb-guide", "not useful", expect_revision=1)
    assert loader.pending("kb-guide") is None
    rejected = list((tmp_path / ".rejected").iterdir())
    assert len(rejected) == 1
    assert "description: third" in (rejected[0] / "SKILL.md").read_text(encoding="utf-8")


def test_concurrent_approvals_with_one_revision_promote_exactly_once(tmp_path: Path) -> None:
    """The CAS and the move are one critical section: whoever loses the race
    either sees a conflict or no pending proposal — never a second promotion."""
    loader = make_loader(tmp_path)
    text = _text()
    loader.install(InstallSource("text", text=text),
                   loader.preview(InstallSource("text", text=text)).preview_token)
    barrier = threading.Barrier(6)
    won: list[int] = []
    lost: list[str] = []

    def attempt() -> None:
        barrier.wait()
        try:
            won.append(loader.approve("kb-guide", expect_revision=0).revision)
        except VadeError as exc:
            lost.append(exc.code)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert won == [1]
    assert lost == ["NOT_FOUND"] * 5
    assert loader.get("kb-guide").revision == 1
    assert sorted(p.name for p in (tmp_path / ".history" / "kb-guide").iterdir()) == ["1.md"]


def test_rollback_restores_an_older_revision_as_a_new_one(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    for description in ("one", "two"):
        text = _text(description=description)
        loader.install(InstallSource("text", text=text), loader.preview(InstallSource("text", text=text)).preview_token)
        loader.approve("kb-guide")
    assert loader.get("kb-guide").revision == 2
    restored = loader.rollback("kb-guide", 1)
    assert restored.revision == 3
    assert restored.description == "one"
    assert (tmp_path / ".history" / "kb-guide" / "3.md").is_file()
    with pytest.raises(VadeError) as excinfo:
        loader.rollback("kb-guide", 99)
    assert excinfo.value.code == "NOT_FOUND"


def test_uninstall_moves_to_trash_and_is_restorable(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    write_skill(tmp_path, "kb-guide")
    loader.reload()
    loader.uninstall("kb-guide")
    assert loader.get("kb-guide") is None
    trash = list((tmp_path / ".trash").iterdir())
    assert len(trash) == 1 and (trash[0] / "SKILL.md").is_file()


def test_reject_moves_pending_to_rejected(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    source = InstallSource("text", text=_text())
    loader.install(source, loader.preview(source).preview_token)
    loader.reject("kb-guide", "not wanted")
    assert not (tmp_path / "pending" / "kb-guide").exists()
    assert list((tmp_path / ".rejected").iterdir())


def test_rejected_ttl_purges_stale_entries_on_reload(tmp_path: Path) -> None:
    """C-11 / design §16.5: `.rejected/<name>-<ts>/` is cleaned after 7 days.

    The clock starts at **rejection**, not at intake: a proposal can sit in
    `pending/` for weeks, so `reject()` stamps the moved directory's mtime
    (a bare rename would carry the old pending mtime over). A fresh rejection
    must survive the same pass.
    """
    loader = make_loader(tmp_path)
    for name in ("kb-guide", "other-guide"):
        source = InstallSource("text", text=_text(name))
        loader.install(source, loader.preview(source).preview_token)
    old = 1_700_000_000
    for name in ("kb-guide", "other-guide"):
        os.utime(tmp_path / "pending" / name, (old, old))

    loader.reject("kb-guide", "not wanted")
    loader.reject("other-guide", "not wanted either")

    stale = next(p for p in (tmp_path / ".rejected").iterdir() if p.name.startswith("kb-guide-"))
    assert stale.stat().st_mtime > old + 86_400, "reject() must stamp the rejection time"

    os.utime(stale, (old, old))
    loader.reload()

    assert not stale.exists()
    survivors = [p.name for p in (tmp_path / ".rejected").iterdir()]
    assert len(survivors) == 1 and survivors[0].startswith("other-guide-")


def test_install_refuses_a_reserved_name(tmp_path: Path) -> None:
    """C-11: "Reserved install directories are not legal names" — `pending` fits
    the name pattern and is also the pending root."""
    loader = make_loader(tmp_path)
    source = InstallSource("text", text=_text("pending"))

    with pytest.raises(VadeError) as excinfo:
        loader.install(source, loader.preview(source).preview_token)

    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert not (tmp_path / "pending" / "pending").exists()


def test_approve_refuses_pending_content_changed_after_intake(tmp_path: Path) -> None:
    """docs/agents/24 §T5: the admin API calls `SkillLoader.approve` directly, so
    the digest check cannot live only in the C-13 proposal wrapper."""
    loader = make_loader(tmp_path)
    source = InstallSource("text", text=_text())
    loader.install(source, loader.preview(source).preview_token)
    pending = tmp_path / "pending" / "kb-guide" / "SKILL.md"
    pending.write_text(pending.read_text(encoding="utf-8") + "\nedited after intake\n",
                       encoding="utf-8")

    with pytest.raises(VadeError) as excinfo:
        loader.approve("kb-guide")

    assert excinfo.value.code == "TOOL_CONFLICT"
    assert "resubmit" in excinfo.value.message
    assert not (tmp_path / "kb-guide").exists()
    assert pending.is_file()
    assert loader.get("kb-guide") is None
    assert not (tmp_path / ".history").exists()


def test_approve_without_a_recorded_digest_still_works(tmp_path: Path) -> None:
    """A pending directory from before `meta.json` carried `sha256` has nothing to
    compare against; refusing it would strand every older install."""
    loader = make_loader(tmp_path)
    source = InstallSource("text", text=_text())
    loader.install(source, loader.preview(source).preview_token)
    meta_path = tmp_path / "pending" / "kb-guide" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("sha256")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    state = loader.approve("kb-guide")

    assert state.status == "active" and (tmp_path / "kb-guide" / "SKILL.md").is_file()


def test_approve_parks_superseded_files_in_trash(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    source = InstallSource("text", text=_text())
    loader.install(source, loader.preview(source).preview_token)
    (tmp_path / "pending" / "kb-guide" / "SKILL.md.superseded").write_text("old", encoding="utf-8")

    loader.approve("kb-guide")

    assert not list((tmp_path / "kb-guide").glob("*.superseded"))
    assert any(p.name.startswith("kb-guide-SKILL.md.superseded") for p in (tmp_path / ".trash").iterdir())


def test_pending_ttl_expiry_on_reload(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    source = InstallSource("text", text=_text())
    loader.install(source, loader.preview(source).preview_token)
    stale = 1_700_000_000
    os.utime(tmp_path / "pending" / "kb-guide", (stale, stale))
    loader.reload()
    assert not (tmp_path / "pending" / "kb-guide").exists()
    assert list((tmp_path / ".trash").iterdir())


def test_set_status_validates_and_persists(tmp_path: Path) -> None:
    loader = make_loader(tmp_path)
    write_skill(tmp_path, "kb-guide")
    loader.reload()
    loader.set_status("kb-guide", "disabled")
    meta = json.loads((tmp_path / "kb-guide" / "meta.json").read_text())
    assert meta["status"] == "disabled"
    assert loader.get("kb-guide").status == "disabled"
    with pytest.raises(VadeError) as excinfo:
        loader.set_status("kb-guide", "pending")
    assert excinfo.value.code == "INVALID_ARGUMENT"


# ------------------------------------------------------------------ zip safety
def _zip_install(tmp_path: Path, payload: bytes) -> SkillLoader:
    loader = make_loader(tmp_path)
    loader.preview(InstallSource("zip", data=payload))  # raises on the bad ones
    return loader


@pytest.mark.parametrize("bad", ["setup.sh", "package.json", "evil.py", "run.exe", "Makefile", "app.js"])
def test_executable_or_installer_entries_are_rejected(tmp_path: Path, bad: str) -> None:
    payload = zip_with_skill(_text(), extra={bad: "#!/bin/sh\necho pwned\n"})
    with pytest.raises(VadeError) as excinfo:
        _zip_install(tmp_path, payload)
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert not (tmp_path / "pending").exists()
    assert list(tmp_path.rglob(bad)) == []


def test_zip_with_symlink_entry_is_rejected(tmp_path: Path) -> None:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("kb-guide/SKILL.md", _text())
        info = zipfile.ZipInfo("kb-guide/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(VadeError) as excinfo:
        _zip_install(tmp_path, buffer.getvalue())
    assert "symlink" in excinfo.value.message


def test_zip_with_exec_bit_is_rejected(tmp_path: Path) -> None:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("kb-guide/SKILL.md", _text())
        info = zipfile.ZipInfo("kb-guide/notes.md")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(info, "notes")
    with pytest.raises(VadeError) as excinfo:
        _zip_install(tmp_path, buffer.getvalue())
    assert "executable" in excinfo.value.message


@pytest.mark.parametrize("entry", ["../evil.md", "/etc/evil.md", "C:/evil.md", "a/../../evil.md"])
def test_zip_traversal_and_absolute_paths_are_rejected(tmp_path: Path, entry: str) -> None:
    payload = zip_bytes({entry: "x", "kb-guide/SKILL.md": _text()})
    with pytest.raises(VadeError) as excinfo:
        _zip_install(tmp_path, payload)
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert not (tmp_path.parent / "evil.md").exists()


def test_zip_needs_exactly_one_skill_md(tmp_path: Path) -> None:
    with pytest.raises(VadeError):
        _zip_install(tmp_path, zip_bytes({"a/SKILL.md": _text("aa"), "b/SKILL.md": _text("bb")}))
    with pytest.raises(VadeError):
        _zip_install(tmp_path, zip_bytes({"README.md": "no skill"}))


def test_zip_entry_and_size_limits(tmp_path: Path) -> None:
    too_many = {f"kb-guide/references/{i}.md": "x" for i in range(40)}
    too_many["kb-guide/SKILL.md"] = _text()
    with pytest.raises(VadeError) as excinfo:
        _zip_install(tmp_path, zip_bytes(too_many))
    assert "entries" in excinfo.value.message

    huge = zip_bytes({"kb-guide/SKILL.md": _text(), "kb-guide/big.dat": "x" * (5 * 1024 * 1024 + 1)})
    with pytest.raises(VadeError) as excinfo:
        _zip_install(tmp_path, huge)
    assert "expands" in excinfo.value.message


def test_valid_zip_with_references_installs_and_is_not_executable(tmp_path: Path) -> None:
    payload = zip_with_skill(_text(), extra={"references/schema.md": "# schema\n", "references/sample.json": "{}"})
    loader = _zip_install(tmp_path, payload)
    state = loader.install(InstallSource("zip", data=payload),
                           loader.preview(InstallSource("zip", data=payload)).preview_token)
    assert state.status == "pending"
    root = tmp_path / "pending" / "kb-guide"
    assert (root / "references" / "schema.md").is_file()
    assert (root / "references" / "sample.json").is_file()
    for path in root.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) & 0o111 == 0, path
    if os.name != "nt":  # POSIX mode bits are a no-op on Windows (chmod does nothing)
        assert stat.S_IMODE((tmp_path / "pending").stat().st_mode) == 0o700


def _parts(message: str):
    from vade.loop.prompt import PromptParts

    return PromptParts(persona="p", tool_block="t", user_message=message)


def test_text_install_has_the_same_1mb_ceiling_as_a_zip_entry(tmp_path: Path) -> None:
    """A raw text install has no archive header; the ceiling is enforced anyway."""
    loader = make_loader(tmp_path)
    huge = _text() + "\n" + "x" * (MAX_SKILL_FILE_BYTES + 1)
    with pytest.raises(VadeError) as excinfo:
        loader.preview(InstallSource("text", text=huge))
    assert "1MB" in excinfo.value.message
    assert not (tmp_path / "pending").exists()


def test_directory_install_refuses_a_symlinked_source(tmp_path: Path) -> None:
    """The walk must not start outside the tree the caller pointed at."""
    outside = tmp_path / "outside"
    write_skill(outside, "kb-guide")
    link = tmp_path / "linked"
    os.symlink(outside, link)
    loader = make_loader(tmp_path / "skills")
    with pytest.raises(VadeError) as excinfo:
        loader.preview(InstallSource("path", path=link))
    assert "symlink" in excinfo.value.message


def test_directory_install_skips_symlinked_subdirectories(tmp_path: Path) -> None:
    source = write_skill(tmp_path, "src")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "notes.md").write_text("outside the skill\n", encoding="utf-8")
    os.symlink(secret, source / "references")
    loader = make_loader(tmp_path / "skills")
    preview = loader.preview(InstallSource("path", path=source))
    assert preview.valid is True
    assert preview.files == (), "a symlinked directory must contribute no entries"
