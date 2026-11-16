"""C-13 intake: what lands in pending/, what is refused, and what approval means."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vade.errors import VadeError
from vade.skill import proposal as proposal_module
from vade.skill.proposal import ProposalService

from .helpers import make_loader, skill_text, write_skill


class FakeConfig:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeBus:
    def __init__(self):
        self.proposed: list[dict] = []

    def emit_skill_proposed(self, **payload):
        self.proposed.append(payload)
        return 1


def make_service(tmp_path, **config):
    root = tmp_path / "skills"
    root.mkdir(exist_ok=True)
    cfg = FakeConfig(**config)
    loader = make_loader(root, config=cfg)
    return ProposalService(loader=loader, config=cfg), loader, root


def create(service, name="code-review", **overrides):
    params = {"name": name, "action": "create", "content": skill_text(name)}
    params.update(overrides)
    return service.submit(params)


def test_create_lands_in_pending_and_is_never_injected(tmp_path):
    service, loader, root = make_service(tmp_path)

    result = create(service)

    assert result["status"] == "pending"
    assert result["revision"] == 0
    pending = root / "pending" / "code-review"
    assert (pending / "SKILL.md").is_file()
    meta = json.loads((pending / "meta.json").read_text(encoding="utf-8"))
    assert meta["origin"] == "proposal"
    assert meta["status"] == "pending"
    assert meta["base_revision"] == 0
    assert meta["sha256"] == result["sha256"]
    # The whole point: a proposal is invisible to prompts and to the catalog.
    assert loader.catalog() == ()
    assert loader.snapshot()["entries"] == []
    assert [s.name for s in loader.list_all(include_pending=True)] == ["code-review"]
    assert all(s.status == "pending" for s in loader.list_all(include_pending=True))


def test_derived_from_and_sender_are_recorded(tmp_path):
    service, _loader, root = make_service(tmp_path)

    create(service, derived_from={"session_id": "s1", "turn_ids": ["t1"], "model": "m"})

    meta = json.loads((root / "pending" / "code-review" / "meta.json").read_text(encoding="utf-8"))
    assert meta["derived_from"] == {"session_id": "s1", "turn_ids": ["t1"], "model": "m"}


def test_create_an_existing_skill_is_a_conflict(tmp_path):
    service, _loader, root = make_service(tmp_path)
    write_skill(root, "code-review")
    service.loader.reload()

    with pytest.raises(VadeError) as caught:
        create(service)

    assert caught.value.code == "TOOL_CONFLICT"
    assert "already exists" in caught.value.message
    assert not (root / "pending" / "code-review").exists()


def test_patch_with_a_stale_base_revision_names_the_real_one(tmp_path):
    service, _loader, root = make_service(tmp_path)
    directory = write_skill(root, "code-review")
    (directory / "meta.json").write_text(json.dumps({"revision": 3, "status": "active"}), encoding="utf-8")
    service.loader.reload()

    with pytest.raises(VadeError) as caught:
        service.submit({"name": "code-review", "action": "patch",
                        "content": skill_text("code-review"), "base_revision": 2})

    assert caught.value.code == "TOOL_CONFLICT"
    assert "3" in caught.value.message          # the caller re-reads instead of guessing
    assert not (root / "pending").exists() or not any((root / "pending").iterdir())


def test_patch_with_the_right_revision_lands_in_pending(tmp_path):
    service, _loader, root = make_service(tmp_path)
    directory = write_skill(root, "code-review")
    (directory / "meta.json").write_text(json.dumps({"revision": 3, "status": "active"}), encoding="utf-8")
    service.loader.reload()

    result = service.submit({"name": "code-review", "action": "patch",
                             "content": skill_text("code-review", description="v4"),
                             "base_revision": 3})

    assert result["base_revision"] == 3
    meta = json.loads((root / "pending" / "code-review" / "meta.json").read_text(encoding="utf-8"))
    assert meta["revision"] == 3
    assert meta["action"] == "patch"


def test_patch_without_an_active_skill_is_not_found(tmp_path):
    service, _loader, _root = make_service(tmp_path)

    with pytest.raises(VadeError) as caught:
        service.submit({"name": "code-review", "action": "patch",
                        "content": skill_text("code-review"), "base_revision": 1})

    assert caught.value.code == "NOT_FOUND"


def test_a_second_proposal_supersedes_the_old_pending_file(tmp_path):
    service, _loader, root = make_service(tmp_path)
    create(service)

    create(service, content=skill_text("code-review", description="second"))

    pending = root / "pending" / "code-review"
    assert (pending / "SKILL.md.superseded").is_file()
    assert (pending / "meta.json.superseded").is_file()
    assert "second" in (pending / "SKILL.md").read_text(encoding="utf-8")
    assert [p.name for p in (root / "pending").iterdir()] == ["code-review"]


def test_a_failed_write_leaves_the_previous_proposal_untouched(tmp_path, monkeypatch):
    """The old code moved the old files aside *before* writing: one ENOSPC and
    the proposal was gone, with `state()` reporting `missing` for a name that had
    a proposal a second earlier."""
    service, loader, root = make_service(tmp_path)
    first = skill_text("code-review", description="first")
    create(service, content=first)

    real = proposal_module._atomic_write

    def broken(path, data, **kwargs):
        if path.name == "meta.json":
            raise OSError(28, "No space left on device")
        return real(path, data, **kwargs)

    monkeypatch.setattr(proposal_module, "_atomic_write", broken)
    with pytest.raises(OSError):
        create(service, content=skill_text("code-review", description="second"))
    monkeypatch.undo()

    pending = root / "pending" / "code-review"
    assert (pending / "SKILL.md").read_text(encoding="utf-8") == first
    assert not (pending / "SKILL.md.superseded").exists()
    assert not list(pending.glob(".staging-*"))
    assert service.state("code-review") == "pending"
    assert json.loads((pending / "meta.json").read_text(encoding="utf-8"))["sha256"] == \
        proposal_module._sha(first)


def test_a_failed_swap_restores_the_previous_proposal(tmp_path, monkeypatch):
    service, _loader, root = make_service(tmp_path)
    first = skill_text("code-review", description="first")
    create(service, content=first)
    old_meta = (root / "pending" / "code-review" / "meta.json").read_text(encoding="utf-8")

    real_replace = os.replace

    def flaky(src, dst, *args, **kwargs):
        # Only the staging -> pending promotion of the second file fails.
        if Path(dst).name == "meta.json" and ".staging-" in str(src):
            raise OSError(5, "Input/output error")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        create(service, content=skill_text("code-review", description="second"))
    monkeypatch.undo()

    pending = root / "pending" / "code-review"
    assert (pending / "SKILL.md").read_text(encoding="utf-8") == first
    assert (pending / "meta.json").read_text(encoding="utf-8") == old_meta
    assert not list(pending.glob("*.superseded"))     # the swap was undone
    assert not list(pending.glob(".staging-*"))


def test_approve_does_not_promote_superseded_files(tmp_path):
    service, loader, root = make_service(tmp_path)
    create(service)
    create(service, content=skill_text("code-review", description="second"))
    assert (root / "pending" / "code-review" / "SKILL.md.superseded").is_file()

    service.review.approve("code-review")

    active = root / "code-review"
    assert sorted(p.name for p in active.iterdir() if p.is_file()) == ["SKILL.md", "meta.json"]
    assert not list(active.glob("*.superseded"))
    assert any(root.joinpath(".trash").iterdir())     # parked, restorable
    assert "second" in (active / "SKILL.md").read_text(encoding="utf-8")


def test_a_secret_in_the_content_is_refused(tmp_path):
    service, _loader, root = make_service(tmp_path)

    with pytest.raises(VadeError) as caught:
        create(service, content=skill_text("code-review", body_suffix="key: sk-abcdefghijklmnop"))

    assert caught.value.code == "INVALID_ARGUMENT"
    assert not (root / "pending" / "code-review").exists()


def test_a_private_key_is_refused(tmp_path):
    service, _loader, root = make_service(tmp_path)

    with pytest.raises(VadeError) as caught:
        # 运行时拼装：仓库不留 PEM 私钥头字面量，运行时值不变
        create(service, content=skill_text("code-review", body_suffix="-----BEGIN " + "RSA " + "PRIVATE KEY-----"))

    assert caught.value.code == "INVALID_ARGUMENT"
    assert not (root / "pending" / "code-review").exists()


def test_a_traversal_name_is_permission_denied(tmp_path):
    service, _loader, root = make_service(tmp_path)

    for name in ("../../core.toml", "../escape", "a/b", ".hidden"):
        with pytest.raises(VadeError) as caught:
            service.validate({"name": name, "action": "create", "content": skill_text("x")})
        assert caught.value.code == "PERMISSION_DENIED", name
    assert not (tmp_path / "core.toml").exists()


def test_every_review_entry_point_refuses_a_path_component(tmp_path):
    """The guard is not intake-only: reject/approve/state used to build a path
    straight from the name, and `reject` is a rename of whatever it resolves to."""
    service, loader, root = make_service(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "SKILL.md").write_text(skill_text("victim"), encoding="utf-8")

    for name in ("../../victim", "../victim", "a/b", ".hidden", ".."):
        for call in (
            lambda n=name: service.review.approve(n),
            lambda n=name: service.review.reject(n),
            lambda n=name: service.review.rollback(n, 1),
            lambda n=name: service.state(n),
            lambda n=name: service.meta(n),
        ):
            with pytest.raises(VadeError) as caught:
                call()
            assert caught.value.code == "PERMISSION_DENIED", name

    assert (victim / "SKILL.md").is_file()
    assert not (root / ".rejected").exists()

    # ... and the loader, which the admin API calls without this wrapper.
    for call in (lambda: loader.reject("../../victim"),
                 lambda: loader.approve("../../victim"),
                 lambda: loader.uninstall("../../victim")):
        with pytest.raises(VadeError) as caught:
            call()
        assert caught.value.code == "PERMISSION_DENIED"
    assert (victim / "SKILL.md").is_file()


def test_a_symlinked_pending_dir_is_permission_denied(tmp_path):
    """`realpath` containment: a link at `pending/<name>` must not be written
    through, even though the name itself is perfectly legal."""
    service, _loader, root = make_service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "pending").mkdir(parents=True)
    os.symlink(outside, root / "pending" / "code-review")

    with pytest.raises(VadeError) as caught:
        create(service)

    assert caught.value.code == "PERMISSION_DENIED"
    assert list(outside.iterdir()) == []


def test_a_reserved_directory_cannot_be_proposed_or_reviewed(tmp_path):
    """`pending` matches C-13's name pattern *and* is the pending root itself."""
    service, loader, root = make_service(tmp_path)
    create(service, name="other")

    with pytest.raises(VadeError) as caught:
        create(service, name="pending")
    assert caught.value.code == "PERMISSION_DENIED"
    assert not (root / "pending" / "pending").exists()

    # The old approve("pending") renamed `skills/pending` — every other
    # proposal included — into `.trash` before failing on the moved source.
    with pytest.raises(VadeError) as caught:
        loader.approve("pending")
    assert caught.value.code == "PERMISSION_DENIED"
    assert (root / "pending" / "other" / "SKILL.md").is_file()


def test_a_bad_name_is_a_conflict(tmp_path):
    service, _loader, _root = make_service(tmp_path)

    with pytest.raises(VadeError) as caught:
        create(service, name="Code_Review")

    assert caught.value.code == "TOOL_CONFLICT"


def test_quota_counts_active_plus_pending(tmp_path):
    service, _loader, root = make_service(tmp_path, **{"skills.active_limit": 2})
    write_skill(root, "one")
    write_skill(root, "two")
    service.loader.reload()

    with pytest.raises(VadeError) as caught:
        create(service, name="three")

    assert caught.value.code == "OVERLOADED"


def test_quota_does_not_count_the_name_being_replaced(tmp_path):
    service, _loader, root = make_service(tmp_path, **{"skills.active_limit": 2})
    create(service, name="one")
    create(service, name="two")

    create(service, name="one")          # superseding does not grow the set

    assert len(list((root / "pending").iterdir())) == 2


def test_oversized_body_is_manifest_invalid(tmp_path):
    service, _loader, root = make_service(tmp_path)
    big = skill_text("code-review") + ("x" * 9000)

    with pytest.raises(VadeError) as caught:
        create(service, content=big)

    assert caught.value.code == "MANIFEST_INVALID"
    assert not (root / "pending" / "code-review").exists()


def test_missing_section_is_manifest_invalid(tmp_path):
    service, _loader, _root = make_service(tmp_path)
    content = skill_text("code-review").replace("## Pitfalls", "## Traps")

    with pytest.raises(VadeError) as caught:
        create(service, content=content)

    assert caught.value.code == "MANIFEST_INVALID"


def test_frontmatter_name_must_match_the_proposal_name(tmp_path):
    service, _loader, _root = make_service(tmp_path)

    with pytest.raises(VadeError) as caught:
        service.submit({"name": "other-name", "action": "create", "content": skill_text("code-review")})

    assert caught.value.code == "MANIFEST_INVALID"


def test_patch_requires_base_revision_at_the_schema_level(tmp_path):
    service, _loader, _root = make_service(tmp_path)

    with pytest.raises(VadeError) as caught:
        service.submit({"name": "code-review", "action": "patch", "content": skill_text("code-review")})

    assert caught.value.code == "MANIFEST_INVALID"


def test_actor_may_not_travel_in_the_payload(tmp_path):
    service, _loader, _root = make_service(tmp_path)

    with pytest.raises(VadeError) as caught:
        create(service, actor="admin")

    assert caught.value.code == "PERMISSION_DENIED"


def test_approve_is_c3b_s_move_and_publishes_the_skill(tmp_path):
    service, loader, root = make_service(tmp_path)
    create(service)

    approved = service.review.approve("code-review", actor="device-1")

    assert approved.revision == 1
    assert service.state("code-review") == "approved"
    assert [s.name for s in loader.catalog()] == ["code-review"]
    assert (root / ".history" / "code-review" / "1.md").is_file()
    assert not (root / "pending" / "code-review").exists()
    assert [r["event"] for r in service.audit_records() if r["event"].endswith("approved")]


def test_approve_refuses_a_file_that_changed_after_intake(tmp_path):
    service, loader, root = make_service(tmp_path)
    create(service)
    pending = root / "pending" / "code-review" / "SKILL.md"
    pending.write_text(pending.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")

    with pytest.raises(VadeError) as caught:
        service.review.approve("code-review")

    assert caught.value.code == "TOOL_CONFLICT"
    assert loader.catalog() == ()
    assert (root / "pending" / "code-review").is_dir()


def test_reject_moves_the_pending_directory(tmp_path):
    service, loader, root = make_service(tmp_path)
    create(service)

    service.review.reject("code-review", reason="not useful")

    assert not (root / "pending" / "code-review").exists()
    assert list((root / ".rejected").iterdir())
    assert loader.catalog() == ()
    assert service.state("code-review") == "rejected"


def test_a_rejection_is_matched_by_name_not_by_prefix(tmp_path):
    """`.rejected/foo-bar-1712…` used to answer `state("foo")` with `rejected`."""
    service, _loader, root = make_service(tmp_path)
    create(service, name="foo-bar")
    service.review.reject("foo-bar")

    assert service.state("foo-bar") == "rejected"
    assert service.state("foo") == "missing"
    assert service.state("foo-b") == "missing"
    assert service.state("foo-bar-baz") == "missing"


def test_reject_then_resubmit_then_approve_is_not_rejected(tmp_path):
    """`.rejected/` is append-only: after a later approval the old rejection
    directory is still on disk and must not win."""
    service, loader, root = make_service(tmp_path)
    create(service)
    service.review.reject("code-review")
    assert service.state("code-review") == "rejected"

    create(service, content=skill_text("code-review", description="second"))
    assert service.state("code-review") == "pending"

    service.review.approve("code-review")

    assert service.state("code-review") == "approved"
    assert list((root / ".rejected").iterdir())      # the history is still there
    assert [s.name for s in loader.catalog()] == ["code-review"]


def test_an_active_skill_outranks_an_older_rejection(tmp_path):
    """Precedence is pending -> approved -> rejected -> missing: a rejection only
    describes a name with nothing active, which is the only actionable case."""
    service, _loader, root = make_service(tmp_path)
    create(service)
    service.review.approve("code-review")
    create(service, content=skill_text("code-review", description="v2"), base_revision=1,
           action="patch")
    service.review.reject("code-review")

    assert service.state("code-review") == "approved"


def test_rollback_goes_through_the_loader(tmp_path):
    service, loader, root = make_service(tmp_path)
    create(service)
    service.review.approve("code-review")
    before = (root / "code-review" / "SKILL.md").read_text(encoding="utf-8")
    (root / "code-review" / "SKILL.md").write_text(skill_text("code-review", description="v2"),
                                                   encoding="utf-8")
    loader.reload()
    assert loader.get("code-review").revision == 1

    # History rev1 is what approve wrote; rolling back is a new revision.
    restored = service.review.rollback("code-review", 1)

    assert restored.revision == 2
    assert (root / "code-review" / "SKILL.md").read_text(encoding="utf-8") == before


def test_skill_proposed_carries_metadata_only(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    loader = make_loader(root)
    bus = FakeBus()
    service = ProposalService(loader=loader, bus=bus)

    create(service)

    assert bus.proposed == [{"name": "code-review", "origin": "proposal",
                             "revision": 1, "base_revision": 0}]
    assert "content" not in bus.proposed[0]


def test_fallback_contract_matches_the_frozen_schema():
    schema = json.loads(proposal_module.SCHEMA_PATH.read_text(encoding="utf-8"))
    block = schema["x-vade"]

    assert block["contract"] == "C-13"
    for key in ("name_pattern", "content_max_chars", "body_max_chars", "body_max_lines",
                "required_sections"):
        assert proposal_module._FALLBACK[key] == block[key], key
    contract = proposal_module.compile_contract()
    assert contract.name_pattern == block["name_pattern"]
    assert contract.body_max_chars == 8192


def test_base_revision_semantics_are_written_down():
    schema = json.loads(proposal_module.SCHEMA_PATH.read_text(encoding="utf-8"))
    semantics = schema["x-vade"]["base_revision"]

    assert semantics["field"] == "base_revision"
    assert "must equal the active revision" in semantics["patch"]
    assert "TOOL_CONFLICT" in semantics["patch"]
    assert semantics["create"].startswith("must be absent or 0")
