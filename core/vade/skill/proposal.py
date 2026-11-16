"""Skill proposal intake and review (C-13 + C3b's loader).

A proposal is a suggestion, not an install. This module can only ever write to
`skills/pending/<name>/`; injection, `skill_catalog`, `approve`, `reject` and
`rollback` stay in C3b's `SkillLoader`. Two implementations of approve would
mean the proposal path and the install path each trust their own layout, and one
of them would never be approvable.

What is enforced here (order matters, first failure wins, nothing lands):

1. payload shape            -> MANIFEST_INVALID      (C-13)
2. path component           -> PERMISSION_DENIED     (a traversal, precedence over naming)
3. name pattern / conflict  -> TOOL_CONFLICT
4. patch base_revision      -> TOOL_CONFLICT (message carries the actual revision)
5. quota active+pending     -> OVERLOADED
6. secret scan              -> INVALID_ARGUMENT
7. frontmatter / body size  -> MANIFEST_INVALID

Step 2 is not intake-only: `meta`, `state`, `approve`, `reject` and `rollback`
all run the same `_target_dir` guard before they touch a path, because every
one of them turns a caller-supplied name into `skills/.../name`. A guard that
only covers `submit` protects the one caller that already validates its payload.

`base_revision` is the revision the author wrote against: the ACTIVE revision in
`<data_dir>/skills/<name>/meta.json`. A pending proposal never advances it, so
two proposals against revision 3 both say 3 and the second supersedes the first
instead of stacking on an unreviewed file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .._jsonschema import SchemaError, validate
from ..errors import VadeError
from ..logging import get_logger
from .contract import load_contract, validate_frontmatter
from .model import SkillParseError, SkillState, parse_document

__all__ = [
    "SCHEMA_PATH", "ProposalContract", "SkillProposal", "ProposalService",
    "ProposalReview", "STATES", "install",
]

log = get_logger("vade.skill.proposal")

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "skill-proposal.schema.json"

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
SUPERSEDED = "superseded"
MISSING = "missing"
STATES = (PENDING, APPROVED, REJECTED, SUPERSEDED)

#: Mirror of `x-vade` when core runs without the repo's `schema/` (installed
#: core). `test_proposal.py::test_fallback_matches_schema` keeps it honest.
_FALLBACK: dict[str, Any] = {
    "contract": "C-13",
    "version": "1.0.0",
    "name_pattern": r"^[a-z][a-z0-9-]{1,31}$",
    "content_max_chars": 16384,
    "body_max_chars": 8192,
    "body_max_lines": 200,
    "required_sections": ["When to Use", "Procedure", "Pitfalls", "Verification"],
}

_SECRETS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sk- key", re.compile(r"sk-[A-Za-z0-9_\-]{4,}")),
    ("private key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
)

#: Mirror of `skills.reserved_names` in the loader contract. `pending` matches
#: C-13's name pattern and is also the pending root itself: a proposal by that
#: name would land in `skills/pending/pending`, and approving it would rename
#: `skills/pending` (every other proposal) into `.trash`.
_RESERVED_NAMES = frozenset({"pending", ".history", ".trash", ".rejected"})

_COMPONENT_TOKENS: tuple[tuple[str, str], ...] = (
    ("/", "a slash"), ("\\", "a backslash"), ("\x00", "a NUL"),
)


def _bad_component(name: Any) -> str:
    """Why `name` cannot be one directory under `skills/pending/` ('' = fine).

    A name is a single path component or it is nothing: `..` anywhere (not just
    as a segment) is refused so `foo..bar` and `../../core.toml` fail the same
    way, and dots are refused in the leading position so `.`/`..hidden` cannot
    name a dot-directory the loader skips.
    """
    if not isinstance(name, str) or not name:
        return "name is empty"
    if name.startswith("."):
        return "name may not start with a dot"
    if ".." in name:
        return "name may not contain '..'"
    for token, label in _COMPONENT_TOKENS:
        if token in name:
            return f"name may not contain {label}"
    if name in _RESERVED_NAMES:
        return f"name {name!r} is reserved for the skill store"
    return ""


@dataclass(frozen=True, slots=True)
class ProposalContract:
    version: str
    name_pattern: str
    content_max_chars: int
    body_max_chars: int
    body_max_lines: int
    required_sections: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillProposal:
    """One accepted proposal, as recorded on disk."""

    name: str
    action: str
    content: str
    base_revision: int
    revision: int
    derived_from: Mapping[str, Any] = field(default_factory=dict)
    received_at: int = 0
    received_from: str = ""
    origin: str = "proposal"
    sha256: str = ""

    @property
    def status(self) -> str:
        return PENDING


def compile_contract() -> ProposalContract:
    data = _x_vade()
    return ProposalContract(
        version=str(data.get("version", "1.0.0")),
        name_pattern=str(data.get("name_pattern", _FALLBACK["name_pattern"])),
        content_max_chars=int(data.get("content_max_chars", _FALLBACK["content_max_chars"])),
        body_max_chars=int(data.get("body_max_chars", _FALLBACK["body_max_chars"])),
        body_max_lines=int(data.get("body_max_lines", _FALLBACK["body_max_lines"])),
        required_sections=tuple(data.get("required_sections") or _FALLBACK["required_sections"]),
    )


def _schema() -> Mapping[str, Any] | None:
    if not SCHEMA_PATH.exists():
        return None
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _x_vade() -> Mapping[str, Any]:
    schema = _schema()
    if schema is None:
        return dict(_FALLBACK)
    block = dict(schema.get("x-vade") or {})
    if str(block.get("contract")) != "C-13":
        raise ValueError(f"{SCHEMA_PATH}: x-vade.contract must be C-13")
    return block


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProposalService:
    """`skill/proposal` intake. One per app; `events.install()` wires it."""

    def __init__(
        self,
        *,
        app: Any = None,
        loader: Any = None,
        bus: Any = None,
        config: Any = None,
        skills_dir: Path | str | None = None,
        clock: Any = time.time,
    ) -> None:
        self._app = app
        self._loader = loader
        self.bus = bus
        self._config = config
        self._skills_dir = Path(skills_dir) if skills_dir is not None else None
        self._clock = clock
        self.contract = compile_contract()
        self.review = ProposalReview(self)
        self._audit_records: list[dict[str, Any]] = []

    # ------------------------------------------------------------- providers
    @property
    def loader(self) -> Any:
        if self._loader is not None:
            return self._loader
        return getattr(self._app, "skill_loader", None)

    @property
    def skills_dir(self) -> Path:
        if self._skills_dir is not None:
            return self._skills_dir
        loader = self.loader
        if loader is not None:
            return Path(loader.skills_dir)
        data_dir = Path("./data")
        config = self._config or getattr(self._app, "config", None)
        if config is not None:
            data_dir = Path(str(config.get("core.data_dir", "./data")))
        return data_dir / "skills"

    @property
    def pending_root(self) -> Path:
        return self.skills_dir / "pending"

    @property
    def limit(self) -> int:
        config = self._config or getattr(self._app, "config", None)
        value = None
        if config is not None and callable(getattr(config, "get", None)):
            value = config.get("skills.active_limit")
        return int(value if value is not None else 64)

    def handlers(self) -> dict[str, Any]:
        return {"skill/proposal": self.on_submit}

    def audit_records(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self._audit_records]

    def _audit(self, event: str, **fields: Any) -> None:
        record = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
        self._audit_records.append(record)
        log.info(event, extra={"event": event, **record})

    # ------------------------------------------------------------------ RPC
    async def on_submit(self, connection: Any, params: Mapping[str, Any] | None) -> Any:
        from ..transport.connection import RpcError

        try:
            return self.submit(params or {}, connection=connection)
        except VadeError as exc:
            return RpcError(exc.code, exc.message)
        except Exception:  # C-04: message only, never a traceback over the wire
            return RpcError("INTERNAL", "skill proposal failed")

    # -------------------------------------------------------------- validate
    def validate(self, params: Mapping[str, Any], *, received_from: str = "") -> SkillProposal:
        payload = dict(params or {})
        if "actor" in payload:
            # Accepting it would let a caller name someone else as the reviewer.
            raise VadeError("PERMISSION_DENIED",
                            "actor is taken from the authenticated connection, not from the payload")

        # Name rules run before the schema on purpose: C-13 also encodes the name
        # pattern, but a traversal has to be reported as a permission problem and a
        # bad name as a conflict, not as a generic "payload does not validate".
        raw_name = payload.get("name")
        if isinstance(raw_name, str) and raw_name:
            self._target_dir(raw_name)           # PERMISSION_DENIED on a path component
            self._validate_name(raw_name)        # TOOL_CONFLICT on a bad shape

        self._validate_schema(payload)
        name = str(payload["name"])
        target = self._target_dir(name)
        action = str(payload["action"])
        base_revision = self._base_revision(payload, action, name)
        self._validate_quota(name)
        self._scan_secrets(str(payload["content"]))
        self._validate_content(name, str(payload["content"]))
        revision = base_revision
        return SkillProposal(
            name=name,
            action=action,
            content=str(payload["content"]),
            base_revision=base_revision,
            revision=revision,
            derived_from=dict(payload.get("derived_from") or {}),
            received_at=int(self._clock() * 1000),
            received_from=received_from,
            origin="proposal",
            sha256=_sha(str(payload["content"])),
        )

    def _validate_schema(self, payload: Mapping[str, Any]) -> None:
        schema = _schema()
        if schema is None:
            required = ("name", "action", "content")
            missing = [key for key in required if key not in payload]
            if missing:
                raise VadeError("MANIFEST_INVALID", f"missing field {missing[0]!r}")
            if payload.get("action") not in ("create", "patch"):
                raise VadeError("MANIFEST_INVALID", "action must be create or patch")
            if payload.get("action") == "patch" and "base_revision" not in payload:
                raise VadeError("MANIFEST_INVALID", "patch requires base_revision")
            return
        try:
            validate(payload, schema)
        except SchemaError as exc:
            raise VadeError("MANIFEST_INVALID", str(exc)) from exc

    def _validate_name(self, name: str) -> None:
        if re.fullmatch(self.contract.name_pattern, name) is None:
            raise VadeError("TOOL_CONFLICT",
                            f"invalid skill name {name!r}; expected {self.contract.name_pattern}")

    @staticmethod
    def _active(loader: Any, name: str) -> Any:
        getter = getattr(loader, "get", None)
        if not callable(getter):
            return None
        return getter(name)

    def _base_revision(self, payload: Mapping[str, Any], action: str, name: str) -> int:
        loader = self.loader
        active = self._active(loader, name)
        current = int(getattr(active, "revision", 0) or 0)
        if action == "create":
            if active is not None:
                raise VadeError("TOOL_CONFLICT",
                                f"skill {name!r} already exists at revision {current}; use action=patch")
            return 0
        if active is None:
            raise VadeError("NOT_FOUND", f"no active skill named {name!r} to patch")
        base = int(payload.get("base_revision", -1))
        if base != current:
            # The actual revision goes in the message: the caller must re-read and
            # re-propose, not guess which number will be accepted.
            raise VadeError("TOOL_CONFLICT",
                            f"base_revision {base} does not match active revision {current} for {name!r}")
        return current

    def _validate_quota(self, name: str) -> None:
        loader = self.loader
        lister = getattr(loader, "list_all", None)
        if not callable(lister):
            return
        try:
            existing = lister(include_pending=True)
        except TypeError:
            existing = lister()
        # Replacing the same pending name does not grow the set.
        count = sum(1 for item in existing if getattr(item, "name", "") != name)
        if count >= self.limit:
            raise VadeError("OVERLOADED",
                            f"active + pending skills would exceed {self.limit}")

    @staticmethod
    def _scan_secrets(content: str) -> None:
        for kind, pattern in _SECRETS:
            if pattern.search(content):
                # Stricter than the loader on purpose: a manual on disk may quote
                # a fake key, but text arriving from a model does not get the benefit.
                raise VadeError("INVALID_ARGUMENT", f"proposal content contains a {kind}")

    def _validate_content(self, name: str, content: str) -> None:
        if len(content) > self.contract.content_max_chars:
            raise VadeError("MANIFEST_INVALID",
                            f"content exceeds {self.contract.content_max_chars} chars")
        try:
            parsed = parse_document(content, required_sections=self.contract.required_sections)
        except SkillParseError as exc:
            raise VadeError("MANIFEST_INVALID", f"SKILL.md is not parseable: {exc}") from exc
        try:
            validate_frontmatter(parsed.frontmatter)
        except SchemaError as exc:
            raise VadeError("MANIFEST_INVALID", str(exc)) from exc
        declared = str(parsed.frontmatter.get("name") or "")
        if declared != name:
            raise VadeError("MANIFEST_INVALID",
                            f"frontmatter name {declared!r} does not match proposal name {name!r}")
        if parsed.missing_sections:
            raise VadeError("MANIFEST_INVALID",
                            "missing section: " + ", ".join(parsed.missing_sections))
        if len(parsed.body) > self.contract.body_max_chars:
            raise VadeError("MANIFEST_INVALID",
                            f"body exceeds {self.contract.body_max_chars} chars")
        if len(parsed.body.splitlines()) > self.contract.body_max_lines:
            raise VadeError("MANIFEST_INVALID",
                            f"body exceeds {self.contract.body_max_lines} lines")

    # ------------------------------------------------------------------ paths
    def _target_dir(self, name: str) -> Path:
        """`skills/pending/<name>`, or PERMISSION_DENIED before anything else.

        Every entry point that turns a name into a path calls this first: the
        intake (`submit`/`validate`), the read side (`meta`, `state`) and the
        review side (`approve`, `reject`, `rollback`). `reject("../../x")` used
        to reach `SkillLoader.reject`, which is a rename of whatever directory
        the name resolved to.
        """
        reason = _bad_component(name)
        if reason:
            raise VadeError("PERMISSION_DENIED", f"proposal {reason}")
        target = self.pending_root / name
        self._assert_inside(target, self.pending_root)
        return target

    @staticmethod
    def _assert_inside(target: Path, root: Path) -> None:
        """`realpath` containment: a symlinked leaf must not point outside."""
        try:
            root_real = os.path.realpath(root)
            target_real = os.path.realpath(target)
        except OSError as exc:  # pragma: no cover - unreadable data dir
            raise VadeError("PERMISSION_DENIED",
                            f"cannot resolve the pending path: {type(exc).__name__}") from exc
        if target_real == root_real or not target_real.startswith(root_real.rstrip(os.sep) + os.sep):
            raise VadeError("PERMISSION_DENIED", "proposal path escapes skills/pending/")

    # ----------------------------------------------------------------- submit
    def submit(self, params: Mapping[str, Any], *, connection: Any = None,
               received_from: str | None = None) -> dict[str, Any]:
        """Validate first, then write. Any failure leaves the disk untouched."""
        sender = received_from if received_from is not None else self._identity(connection)
        proposal = self.validate(params, received_from=sender)
        target = self._target_dir(proposal.name)
        self._write(proposal, target)
        self._reload()
        payload = {
            "name": proposal.name,
            "action": proposal.action,
            "status": PENDING,
            "revision": proposal.revision,
            "base_revision": proposal.base_revision,
            "sha256": proposal.sha256,
        }
        self._audit("skill.proposal", name=proposal.name, action=proposal.action,
                    revision=proposal.revision, base_revision=proposal.base_revision,
                    sha8=proposal.sha256[:8], received_from=sender or None,
                    bytes=len(proposal.content.encode("utf-8")), ok=True)
        if self.bus is not None:
            # Metadata only: the body must never ride a UI event.
            self.bus.emit_skill_proposed(
                name=proposal.name, origin=proposal.origin,
                revision=proposal.revision + 1, base_revision=proposal.base_revision,
            )
        return payload

    def _supersede(self, target: Path) -> None:
        """One pending file per name: the old one is kept as `*.superseded`.

        Only ever called *after* the replacement is fully written (`_write`):
        the old bytes stay readable at `SKILL.md.superseded` for the whole
        operation, so no window exists where this proposal has neither a
        complete old nor a complete new file. `SkillLoader.approve` parks these
        files in `.trash` before the directory is promoted, so they never reach
        `skills/<name>/`.
        """
        if not target.is_dir():
            return
        for filename in ("SKILL.md", "meta.json"):
            path = target / filename
            if path.is_file() or path.is_symlink():
                os.replace(path, target / f"{filename}.superseded")

    def _write(self, proposal: SkillProposal, target: Path) -> None:
        """Land the new snapshot, then retire the old one.

        Order matters twice over:

        1. the new bytes are written *and fsynced* into `.staging-*` before the
           previous files move to `*.superseded` — a failed write therefore
           leaves the old proposal exactly where it was;
        2. the swap is undone if it fails part-way, so a half-written pair
           (`SKILL.md` new, `meta.json` old) never becomes the pending state.

        The first version of this method did the rename first: one ENOSPC wiped
        a proposal no reviewer had seen yet, and `state()` then reported
        `missing` for a name that had a proposal a second earlier.
        """
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target, 0o700)
        self._assert_inside(target, self.pending_root)
        for stale in target.glob(".staging-*"):     # a crash left one behind
            shutil.rmtree(stale, ignore_errors=True)
        meta = {
            "name": proposal.name,
            "revision": proposal.revision,
            "status": PENDING,
            "origin": proposal.origin,
            "action": proposal.action,
            "base_revision": proposal.base_revision,
            "derived_from": dict(proposal.derived_from) or None,
            "received_at": proposal.received_at,
            "received_from": proposal.received_from or None,
            "sha256": proposal.sha256,
        }
        payload: dict[str, bytes] = {
            "SKILL.md": proposal.content.encode("utf-8"),
            "meta.json": (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        }
        staging = target / f".staging-{secrets.token_hex(4)}"
        staging.mkdir(mode=0o700)
        try:
            for filename, data in payload.items():
                _atomic_write(staging / filename, data)
            moved: list[str] = []
            try:
                self._supersede(target)
                for filename in payload:
                    os.replace(staging / filename, target / filename)
                    moved.append(filename)
            except OSError:
                # Covers a failure in either half: `_supersede` moving the old
                # files aside, or the swap bringing the new ones in.
                self._restore(target, payload, moved)
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _restore(target: Path, payload: Mapping[str, bytes], moved: Iterable[str]) -> None:
        """Undo a failed swap: the old snapshot wins, the new bytes are dropped."""
        for filename in payload:
            backup = target / f"{filename}.superseded"
            live = target / filename
            try:
                if backup.exists():
                    os.replace(backup, live)
                elif filename in moved and live.exists():
                    live.unlink()
            except OSError as exc:      # pragma: no cover - restore is best effort
                log.error("skill proposal restore failed",
                          extra={"event": "skill.proposal.restore_failed",
                                 "file": filename, "error": type(exc).__name__})

    def _reload(self) -> None:
        reload = getattr(self.loader, "reload", None)
        if callable(reload):
            reload()

    @staticmethod
    def _identity(connection: Any) -> str:
        """Who submitted: taken from the authenticated connection, never params."""
        if connection is None:
            return ""
        for attribute in ("instance_id", "module_id", "connection_id", "id"):
            value = getattr(connection, attribute, None)
            if value:
                return str(value)
        crypto = getattr(connection, "crypto", None)
        fingerprint = getattr(crypto, "remote_fingerprint", None) if crypto is not None else None
        return f"fp:{str(fingerprint)[:16]}" if fingerprint else ""

    # -------------------------------------------------------------- review api
    def meta(self, name: str) -> dict[str, Any]:
        path = self._target_dir(name) / "meta.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(data) if isinstance(data, Mapping) else {}

    def _rejected_at(self, name: str) -> int | None:
        """Newest rejection stamp for exactly `name`, or None.

        `.rejected/<name>-<epoch>[-<n>]` is what `SkillLoader._move_to` writes,
        so the name is matched with both delimiters intact. `startswith(name +
        '-')` used to make `foo` inherit `foo-bar`'s rejection — the two skills
        are unrelated and only one of them was ever reviewed.
        """
        directory = self.skills_dir / ".rejected"
        if not directory.is_dir():
            return None
        pattern = re.compile(rf"^{re.escape(name)}-(\d+)(?:-\d+)?$")
        stamps = [int(match.group(1))
                  for match in (pattern.fullmatch(entry.name) for entry in directory.iterdir())
                  if match is not None]
        return max(stamps) if stamps else None

    def state(self, name: str) -> str:
        """`pending` while the directory exists, then what the loader decided.

        Precedence: pending -> approved -> rejected -> missing. The active skill
        is checked *before* a rejection on purpose: `.rejected/` is append-only,
        so after "reject -> resubmit -> approve" the old rejection directory is
        still there and answering from it would report a live skill as rejected
        forever. A rejection therefore only describes a name that has nothing
        active — which is also the only case where it is actionable.
        """
        target = self._target_dir(name)
        if target.is_dir():
            return PENDING
        if self._active(self.loader, name) is not None:
            return APPROVED
        if self._rejected_at(name) is not None:
            return REJECTED
        return MISSING


class ProposalReview:
    """pending -> approved | rejected. The moves are C3b's; this decides *when*.

    `superseded` is written by intake (a newer proposal for the same name), never
    by a reviewer: a reviewer must not be able to erase a pending proposal that
    is still on screen.
    """

    def __init__(self, service: ProposalService) -> None:
        self.service = service

    def state(self, name: str) -> str:
        return self.service.state(name)

    def approve(self, name: str, *, actor: str = "admin") -> SkillState:
        service = self.service
        target = service._target_dir(name)      # PERMISSION_DENIED before any read
        meta = service.meta(name)
        if not meta:
            raise VadeError("NOT_FOUND", f"no pending proposal named {name!r}")
        path = target / "SKILL.md"
        try:
            current = _sha(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise VadeError("NOT_FOUND", f"pending SKILL.md for {name!r} is gone") from exc
        if meta.get("sha256") and current != meta.get("sha256"):
            # Edited after intake: that is a new proposal, not an approval. An
            # approval would otherwise bless bytes no reviewer ever saw.
            # `SkillLoader.approve` repeats the check for callers that skip this
            # wrapper (the admin API calls the loader directly).
            raise VadeError("TOOL_CONFLICT",
                            f"pending content for {name!r} changed since intake; resubmit the proposal")
        approved = service.loader.approve(name)
        service._audit("skill.proposal.approved", name=name, revision=approved.revision,
                       actor=actor, ok=True)
        return approved

    def reject(self, name: str, *, reason: str = "", actor: str = "admin") -> None:
        service = self.service
        target = service._target_dir(name)      # PERMISSION_DENIED before any move
        if not target.is_dir():
            raise VadeError("NOT_FOUND", f"no pending proposal named {name!r}")
        service.loader.reject(name, reason)
        service._audit("skill.proposal.rejected", name=name, actor=actor,
                       reason=reason or None, ok=True)

    def rollback(self, name: str, revision: int, *, actor: str = "admin") -> SkillState:
        service = self.service
        service._target_dir(name)               # the name reaches `skills/<name>/`
        state = service.loader.rollback(name, int(revision))
        service._audit("skill.proposal.rolled_back", name=name, revision=state.revision,
                       from_revision=int(revision), actor=actor, ok=True)
        return state


def install(app: Any) -> None:
    """Publish `skill/proposal` without requiring `vade.events` to be installed.

    Normally `events.install()` builds the service (it owns the bus); this keeps
    the package usable on its own and stays idempotent.
    """
    if getattr(app, "skill_proposals", None) is not None:
        return
    bus = getattr(app, "event_bus", None)
    service = ProposalService(app=app, bus=bus)
    app.skill_proposals = service
    providers = getattr(app, "handler_providers", None)
    if providers is None:
        providers = []
        app.handler_providers = providers
    if not any(getattr(p, "__self__", None) is service for p in providers):
        providers.append(service.handlers)
    handlers = getattr(app, "session_handlers", None)
    if isinstance(handlers, dict):
        for name, handler in service.handlers().items():
            handlers.setdefault(name, handler)
