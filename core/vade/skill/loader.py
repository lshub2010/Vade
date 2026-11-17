"""The skill loader: scan, validate, render, install, version, roll back.

Boundaries this file keeps (docs/agents/23-C3b-skill-loader.md):
  * **Zero execution.** Installing reads bytes and writes text. Nothing is
    imported, evaluated, chmod-ed or run; a zip containing anything executable
    is rejected before a single byte lands under `skills/`.
  * **No hot reload.** A half-written SKILL.md must never be read; `reload()`
    (startup / SIGHUP / after an approve) is the only way new bytes are seen.
  * **No prompt injection channel from outside disk.** Skills come from
    `<data_dir>/skills/*/SKILL.md`, never from a module, the network, or
    `capabilities.prompts`.
  * **Failures never block core startup.** A bad skill is a warning, not an
    exception; a missing schema falls back to the mirrored contract.

The four directories under the skills root are reserved and never scanned:
`pending/` (installs + C3d proposals), `.history/` (revisions), `.trash/`
(uninstall/expiry, restorable), `.rejected/` (rejected proposals).
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import stat
import time
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from ..errors import VadeError
from ..logging import get_logger
from . import audit as skill_audit
from . import render as skill_render
from .contract import Contract, SchemaError, load_contract, validate_frontmatter
from .matcher import select
from .model import (
    SkillMeta,
    SkillParseError,
    SkillState,
    SkillSummary,
    parse_document,
    truncate_body,
)

__all__ = [
    "SkillLoader", "InstallSource", "PreviewResult", "Material", "MAX_SKILL_FILE_BYTES",
    "check_source",
]

log = get_logger("vade.skill")

MAX_SKILL_FILE_BYTES = 1_000_000
MAX_URL_BYTES = 1_048_576
PREVIEW_TTL_S = 60


# --------------------------------------------------------------------- payloads
@dataclass(frozen=True, slots=True)
class InstallSource:
    """Where a skill comes from. `kind` is one of text / path / zip / url."""

    kind: str
    text: str = ""
    path: Path | None = None
    data: bytes = b""
    url: str = ""


@dataclass(frozen=True, slots=True)
class Material:
    text: str
    files: Mapping[str, bytes] = field(default_factory=dict)
    digest: str = ""
    source: str = "text"
    entries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreviewResult:
    frontmatter: Mapping[str, Any]
    body_head: str
    missing_tools: tuple[str, ...]
    conflict: str
    preview_token: str
    valid: bool
    missing_sections: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    name: str = ""


@dataclass(frozen=True, slots=True)
class _Preview:
    name: str
    digest: str
    expires_at: float


# ------------------------------------------------------------------- helpers
def _now_ms() -> int:
    return int(time.time() * 1000)


def _norm_tool(name: str) -> str:
    """`mcp-bridge__s_kb_recall` and `mcp_bridge/s_kb_recall` compare equal."""
    return str(name).strip().lower().replace("/", "__").replace(".", "_").replace("-", "_")


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _bad_name(name: Any, reserved: frozenset[str] | set[str] = frozenset()) -> str:
    """Why `name` cannot be one directory under the skills root ('' = fine).

    A name is a single path component or it is nothing. `..` is refused anywhere
    (not just as a whole segment) so `foo..bar` fails like `../x`; a leading dot
    is refused so a name cannot select a dot-directory the scanner skips; the
    reserved names are refused because `skills/pending` is not a skill.
    """
    if not isinstance(name, str) or not name:
        return "name is empty"
    if name.startswith("."):
        return "name may not start with a dot"
    if ".." in name:
        return "name may not contain '..'"
    for token in ("/", "\\", "\x00"):
        if token in name:
            return f"name may not contain {token!r}"
    if name in reserved:
        return f"{name!r} is reserved for the skill store"
    return ""


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _digest(text: str, files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(text.encode("utf-8"))
    for name in sorted(files):
        digest.update(b"\0" + name.encode("utf-8") + b"\0" + files[name])
    return digest.hexdigest()


def _reject(message: str) -> None:
    raise VadeError("INVALID_ARGUMENT", message)


# --------------------------------------------------------------------- source
def _validate_url(url: str) -> None:
    """SSRF gate, re-run on every redirect hop.

    Rejects http, credentials in the URL, and any host that resolves to a
    loopback / private / link-local / reserved address (including the cloud
    metadata endpoint 169.254.169.254 and `::1`).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        _reject("skill URL must be https")
    if parsed.username or parsed.password:
        _reject("skill URL must not embed credentials")
    host = parsed.hostname
    if not host:
        _reject("skill URL has no host")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            _reject(f"skill URL host does not resolve: {host} ({type(exc).__name__})")
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0].split("%")[0]))
            except ValueError:
                continue
    if not addresses:
        _reject(f"skill URL host does not resolve: {host}")
    for address in addresses:
        if (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast or address.is_unspecified
        ):
            _reject("skill URL points at a private/loopback/link-local address")


def _download(url: str) -> bytes:
    _validate_url(url)
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a test dep, not runtime
        raise VadeError("UNAVAILABLE", "https install requires httpx") from None
    current = url
    with httpx.Client(follow_redirects=False, timeout=15.0) as client:
        for _hop in range(4):
            with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        _reject("redirect without Location")
                    current = str(response.url.join(location)) if not location.startswith("http") else location
                    _validate_url(current)  # every hop, not just the first
                    continue
                if response.status_code != 200:
                    raise VadeError("UNAVAILABLE", f"skill download failed: HTTP {response.status_code}")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_URL_BYTES:
                        _reject("skill download exceeds 1MB")
                    chunks.append(chunk)
                return b"".join(chunks)
        _reject("too many redirects downloading skill")


def _forbidden_entry(name: str, contract: Contract) -> str:
    """Return the reason a zip/dir entry is refused ('' = allowed)."""
    base = PurePosixPath(name).name.lower()
    if base in contract.forbidden_names:
        return f"executable/installer file {base!r}"
    for suffix in contract.forbidden_suffixes:
        if base.endswith(suffix):
            return f"executable file {base!r}"
    return ""


def _check_entry_name(name: str) -> str:
    if "\x00" in name or "\\" in name:
        _reject("zip entry contains a NUL or backslash")
    if re.match(r"^[A-Za-z]:", name):
        _reject(f"zip entry has a Windows drive path: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute():
        _reject(f"zip entry is absolute: {name!r}")
    if any(part == ".." for part in pure.parts):
        _reject(f"zip entry escapes the archive root: {name!r}")
    if not pure.parts or any(part in ("", ".") for part in pure.parts):
        _reject(f"zip entry has an empty path segment: {name!r}")
    return str(pure)


def _read_zip(payload: bytes, contract: Contract) -> tuple[str, dict[str, bytes], tuple[str, ...]]:
    """Validate every entry, then read. Never `extractall`, never chmod."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        _reject("not a valid zip archive")
    infos = [i for i in archive.infolist() if not (i.filename.startswith("__MACOSX/") or i.filename.endswith(".DS_Store"))]
    if len(infos) > contract.max_entries:
        _reject(f"zip has {len(infos)} entries; limit is {contract.max_entries}")
    total = sum(max(0, info.file_size) for info in infos if not info.is_dir())
    if total > contract.max_uncompressed_bytes:
        _reject(f"zip expands to {total} bytes; limit is {contract.max_uncompressed_bytes}")

    skill_md: tuple[str, bytes] | None = None
    files: dict[str, bytes] = {}
    for info in infos:
        name = _check_entry_name(info.filename.rstrip("/"))
        if info.is_dir():
            continue
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            _reject(f"zip entry is a symlink: {name!r}")
        if mode & 0o111:
            _reject(f"zip entry is executable: {name!r}")
        reason = _forbidden_entry(name, contract)
        if reason:
            _reject(f"zip contains {reason}")
        if info.flag_bits & 0x1:
            _reject(f"zip entry is encrypted: {name!r}")
        parts = PurePosixPath(name).parts
        if parts[-1].lower() == "skill.md":
            if len(parts) > contract.max_skill_md_depth:
                _reject(f"SKILL.md is nested too deep ({len(parts)} levels)")
            if skill_md is not None:
                _reject("zip must contain exactly one SKILL.md")
            if info.file_size > MAX_SKILL_FILE_BYTES:
                _reject("SKILL.md is larger than 1MB")
            skill_md = (name, archive.read(info))
            continue
        data = archive.read(info)
        if len(data) > MAX_SKILL_FILE_BYTES:
            _reject(f"zip entry {name!r} is larger than 1MB")
        files[name] = data

    if skill_md is None:
        _reject("zip must contain exactly one SKILL.md")
    try:
        text = skill_md[1].decode("utf-8")
    except UnicodeDecodeError:
        _reject("SKILL.md is not UTF-8")
    # Everything is written relative to the skill directory: `pkg/SKILL.md` +
    # `pkg/references/x.md` must land as `references/x.md`, not `pkg/references/`.
    prefix = PurePosixPath(skill_md[0]).parent
    if str(prefix) != ".":
        stripped: dict[str, bytes] = {}
        for name, data in files.items():
            path = PurePosixPath(name)
            key = str(path.relative_to(prefix)) if path.is_relative_to(prefix) else name
            stripped[key] = data
        files = stripped
    return text, files, tuple(sorted(files))


def _read_directory(root: Path, contract: Contract) -> tuple[str, dict[str, bytes], tuple[str, ...]]:
    # Pre-check before `rglob`: a symlinked install source is refused outright,
    # so the walk can never start outside the tree the caller pointed at.
    if root.is_symlink():
        _reject("directory install source is a symlink")
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        _reject("directory has no SKILL.md")
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() and path.is_dir():
            continue                        # never walk through a symlinked dir
        if path.is_dir():
            continue
        relative = path.relative_to(root)
        if relative.parts[0] in contract.reserved_names or relative.name.startswith("."):
            continue
        if path.is_symlink():
            _reject(f"directory entry is a symlink: {relative.as_posix()!r}")
        name = relative.as_posix()
        reason = _forbidden_entry(name, contract)
        if reason:
            _reject(f"directory contains {reason}")
        data = path.read_bytes()
        total += len(data)
        if total > contract.max_uncompressed_bytes:
            _reject("directory exceeds the 5MB install limit")
        files[name] = data
    text = files.pop("SKILL.md", b"").decode("utf-8", "strict")
    return text, files, tuple(sorted(files))


def materialize(source: InstallSource, contract: Contract) -> Material:
    """Read a source into text + reference files. Pure read: nothing executes."""
    if source.kind == "text":
        text = source.text
        # A raw text install has no archive header to bound it, so it gets the
        # same 1MB ceiling as a SKILL.md inside a zip (§C3b "one size limit").
        if len(text.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
            _reject("skill text is larger than 1MB")
        return Material(text=text, digest=_digest(text, {}), source="text")
    if source.kind == "zip":
        text, files, entries = _read_zip(source.data, contract)
        return Material(text=text, files=files, digest=_digest(text, files), source="zip", entries=entries)
    if source.kind == "url":
        payload = _download(source.url)
        if payload[:4] == b"PK\x03\x04":
            text, files, entries = _read_zip(payload, contract)
            return Material(text=text, files=files, digest=_digest(text, files), source="url", entries=entries)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            _reject("downloaded skill is neither a zip nor UTF-8 markdown")
        return Material(text=text, digest=_digest(text, {}), source="url")
    if source.kind == "path":
        if source.path is None:
            _reject("path install requires a path")
        path = Path(source.path)
        if path.is_dir():
            text, files, entries = _read_directory(path, contract)
            return Material(text=text, files=files, digest=_digest(text, files), source="path", entries=entries)
        if not path.is_file():
            _reject(f"skill path does not exist: {path}")
        payload = path.read_bytes()
        if path.suffix.lower() == ".zip" or payload[:4] == b"PK\x03\x04":
            text, files, entries = _read_zip(payload, contract)
            return Material(text=text, files=files, digest=_digest(text, files), source="path", entries=entries)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            _reject("skill file is not UTF-8")
        return Material(text=text, digest=_digest(text, {}), source="path")
    _reject(f"unknown install source kind {source.kind!r}")


def check_source(text: str, *, contract: Contract) -> tuple[Mapping[str, Any], tuple[str, ...], tuple[str, ...]]:
    """Parse + validate, returning `(frontmatter, missing_sections, problems)`.

    `problems` is empty for a loadable skill. Nothing here touches disk, which
    is what lets `preview()` be honest: it validates the exact bytes.
    """
    try:
        parsed = parse_document(text, required_sections=contract.required_sections)
    except SkillParseError as exc:
        return {}, (), (str(exc),)
    problems: list[str] = []
    try:
        validate_frontmatter(parsed.frontmatter)
    except SchemaError as exc:
        problems.append(str(exc))
    if parsed.missing_sections:
        problems.append("missing section: " + ", ".join(parsed.missing_sections))
    return parsed.frontmatter, parsed.missing_sections, tuple(problems)


# --------------------------------------------------------------------- loader
class SkillLoader:
    """Owns `data/skills/`. One instance per app; `install()` wires the hooks."""

    def __init__(
        self,
        skills_dir: Path | str,
        tool_snapshot: Any = None,
        *,
        contract: Contract | None = None,
        config: Any = None,
        clock: Any = None,
    ) -> None:
        self.skills_dir = Path(skills_dir)
        self.tool_snapshot = tool_snapshot
        self.contract = contract or load_contract()
        self.config = config
        self._clock = clock or time.time
        self.max_activated = int(self.contract.max_activated)
        self.l0_token_budget = int(self._cfg("skills.l0_token_budget", self.contract.l0_token_budget))
        self.body_max_chars = int(self._cfg("skills.body_max_chars", self.contract.body_max_chars))
        self.body_max_lines = int(self.contract.body_max_lines)
        self.body_budget_chars = int(self.contract.body_budget_chars)
        self.pending_ttl_days = int(self._cfg("skills.pending_ttl_days", 7))
        self._states: dict[str, SkillState] = {}
        self._invalid: dict[str, str] = {}
        self._pending: dict[str, SkillState] = {}
        self._previews: dict[str, _Preview] = {}
        self._fail_counts: dict[str, int] = {}
        self._warnings: list[str] = []
        self._secret_hits = 0

    # ---------------------------------------------------------------- config
    def _cfg(self, key: str, default: Any) -> Any:
        getter = getattr(self.config, "get", None)
        if callable(getter):
            value = getter(key)
            if value is not None:
                return value
        return default

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    @property
    def invalid(self) -> Mapping[str, str]:
        return dict(self._invalid)

    @property
    def secret_hits(self) -> int:
        return self._secret_hits

    def _warn(self, message: str) -> None:
        self._warnings.append(message)
        log.warning(message, extra={"code": None, "msg": message})

    def _audit(self, event: str, **fields: Any) -> None:
        """Structured audit line; body text never appears here."""
        log.info(event, extra={"event": event, **{k: v for k, v in fields.items() if v is not None}})

    # ------------------------------------------------------------------ tools
    def _tool_names(self) -> set[str]:
        source = self.tool_snapshot
        if source is None:
            return set()
        if callable(source):
            try:
                source = source()
            except Exception:  # a broken registry must not break a prompt
                return set()
        names = getattr(source, "names", None)
        if callable(names):
            return {_norm_tool(n) for n in names()}
        out: set[str] = set()
        for item in source or ():
            out.add(_norm_tool(getattr(item, "llm_name", item)))
        return out

    def _missing_tools(self, requires: Sequence[str], available: set[str] | None = None) -> tuple[str, ...]:
        names = self._tool_names() if available is None else available
        return tuple(tool for tool in requires if _norm_tool(tool) not in names)

    def _availability(self, state: SkillState, available: set[str] | None = None) -> SkillState:
        missing = self._missing_tools(state.requires_tools, available)
        if not missing:
            return state.with_meta(replace(state.meta, available=True, unavailable_reason=""))
        return state.with_meta(
            replace(state.meta, available=False, unavailable_reason=f"missing tool: {missing[0]}")
        )

    # ----------------------------------------------------------------- reload
    def reload(self) -> None:
        """Full rescan. Called at startup, on SIGHUP, and after approve/rollback."""
        self._states = {}
        self._invalid = {}
        self._pending = {}
        root = self.skills_dir
        root_real = os.path.realpath(root)
        if not root.is_dir():
            return
        self._expire_pending(root)
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if entry.name in self.contract.reserved_names:
                continue
            if not entry.is_dir():
                continue
            if re.fullmatch(self.contract.name_pattern, entry.name) is None:
                self._warn(f"invalid skill directory name: {entry.name}")
                continue
            self._scan_one(entry, root_real, pending=False)
        pending_root = root / "pending"
        if pending_root.is_dir():
            for entry in sorted(pending_root.iterdir(), key=lambda p: p.name):
                if entry.is_dir() and re.fullmatch(self.contract.name_pattern, entry.name):
                    self._scan_one(entry, root_real, pending=True)

    def _scan_one(self, directory: Path, root_real: str, *, pending: bool) -> None:
        skill_md = directory / "SKILL.md"
        if not skill_md.is_file():
            return
        real = os.path.realpath(skill_md)
        if not _within(real, root_real):
            self._warn(f"skill path escapes the skills root: {directory.name}")
            return
        try:
            raw = skill_md.read_bytes()
        except OSError as exc:
            self._warn(f"skill unreadable: {directory.name}: {type(exc).__name__}")
            return
        if len(raw) > MAX_SKILL_FILE_BYTES:
            self._warn(f"skill file too large: {directory.name}")
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._warn(f"skill is not UTF-8: {directory.name}")
            return
        try:
            parsed = parse_document(text, required_sections=self.contract.required_sections)
        except SkillParseError as exc:
            self._warn(f"skill parse failed: {directory.name}: {exc}")
            return
        frontmatter = parsed.frontmatter
        try:
            validate_frontmatter(frontmatter)
        except SchemaError as exc:
            self._warn(f"skill invalid: {directory.name}: {exc}")
            self._invalid[directory.name] = str(exc)
            return
        name = str(frontmatter["name"])
        if name != directory.name:
            message = f"name mismatch: directory {directory.name!r} declares {name!r}"
            self._warn(f"skill invalid: {directory.name}: {message}")
            self._invalid[directory.name] = message
            return
        if parsed.missing_sections:
            message = "missing section: " + ", ".join(parsed.missing_sections)
            self._warn(f"missing section: {name} ({', '.join(parsed.missing_sections)})")
            self._invalid[name] = message
            return
        target = self._pending if pending else self._states
        if name in target:
            self._warn(f"duplicate skill name: {name} (skipped {skill_md})")
            return
        meta = self._read_meta(directory, revision=0, status="pending" if pending else "active")
        body, truncated, _reason = truncate_body(
            parsed.body,
            max_chars=self.body_max_chars,
            max_lines=self.body_max_lines,
            # Relative on purpose: the hint lands in the prompt, and an absolute
            # data-dir path there is a host-layout leak. `fs` resolves it.
            path=f"{name}/SKILL.md",
        )
        findings = skill_audit.scan_secrets(body)
        if findings:
            self._secret_hits += sum(f.count for f in findings)
            detail = ", ".join(f"{f.kind}x{f.count}" for f in findings)
            self._warn(f"skill secret pattern: {name} ({detail})")
        state = SkillState(
            meta=SkillMeta(
                name=name,
                description=" ".join(str(frontmatter["description"]).split()),
                when_to_use=" ".join(str(frontmatter["when_to_use"]).split()),
                triggers=tuple(str(t) for t in (frontmatter.get("triggers") or ())),
                requires_tools=tuple(str(t) for t in (frontmatter.get("requires_tools") or ())),
                body=body,
                truncated=truncated,
                path=skill_md,
                available=True,
                unavailable_reason="",
            ),
            revision=int(meta.get("revision", 0)),
            status=str(meta.get("status", "pending" if pending else "active")),
            origin=str(meta.get("origin", "manual")),
            last_used_at=meta.get("last_used_at"),
            fail_count=int(meta.get("fail_count", 0)),
            derived_from=meta.get("derived_from"),
            hardware=meta.get("hardware"),
            sha256=hashlib.sha256(raw).hexdigest(),
            sections=tuple(parsed.sections),
        )
        if pending:
            state = state.with_status("pending")
        state = self._availability(state)
        if not state.available and not pending:
            self._warn(f"skill unavailable: {name} ({state.unavailable_reason})")
        target[name] = state
        self._audit(
            "skill_loaded",
            name=name,
            revision=state.revision,
            status=state.status,
            path=str(skill_md),
            body_sha8=state.sha256[:8],
        )

    def _read_meta(self, directory: Path, *, revision: int, status: str) -> dict[str, Any]:
        path = directory / "meta.json"
        defaults = {
            "revision": revision, "status": status, "origin": "manual",
            "last_used_at": None, "fail_count": 0, "derived_from": None, "hardware": None,
        }
        if not path.is_file():
            return defaults
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._warn(f"meta.json unreadable, using defaults: {directory.name}")
            return defaults
        if not isinstance(data, Mapping):
            self._warn(f"meta.json is not an object, using defaults: {directory.name}")
            return defaults
        out = dict(defaults)
        for key in out:
            if key in data:
                out[key] = data[key]
        if out["status"] not in ("pending", "active", "disabled", "stale", "unavailable", "invalid"):
            self._warn(f"meta.json status invalid, using {status}: {directory.name}")
            out["status"] = status
        if out["origin"] not in ("manual", "install", "proposal"):
            out["origin"] = "manual"
        try:
            out["revision"] = max(0, int(out["revision"]))
            out["fail_count"] = max(0, int(out["fail_count"]))
        except (TypeError, ValueError):
            out["revision"], out["fail_count"] = revision, 0
        return out

    def _write_meta(self, state: SkillState) -> None:
        path = self.skills_dir / state.name / "meta.json"
        if not path.parent.is_dir():
            return
        payload = json.dumps(state.meta_json(), ensure_ascii=False, indent=2) + "\n"
        _atomic_write(path, payload.encode("utf-8"))

    # ------------------------------------------------------------------ names
    def _require_name(self, name: str) -> str:
        """Refuse a name that is not one directory under the skills root.

        Every lifecycle method that composes a path from a caller-supplied name
        runs this first (`approve`, `reject`, `uninstall`, `rollback`). Without
        it `reject("../../x")` was an `os.replace` of an arbitrary directory
        into `.rejected/`, and `approve("pending")` renamed the pending root —
        with every other proposal in it — into `.trash`. The check lives here,
        not only in the C-13 wrapper, because the admin API calls these methods
        directly and a guard on one caller protects nobody else.
        """
        reason = _bad_name(name, self.contract.reserved_names)
        if reason:
            raise VadeError("PERMISSION_DENIED", f"skill {reason}")
        return name

    def _contained(self, directory: Path, root: Path) -> Path:
        """`directory` must resolve inside `root` (a symlinked leaf is refused)."""
        try:
            root_real = os.path.realpath(root)
            real = os.path.realpath(directory)
        except OSError as exc:  # pragma: no cover - unreadable data dir
            raise VadeError("PERMISSION_DENIED",
                            f"cannot resolve the skill path: {type(exc).__name__}") from exc
        if real == root_real or not _within(real, root_real):
            raise VadeError("PERMISSION_DENIED",
                            f"skill path escapes {root.name}/: {directory.name}")
        return directory

    # ---------------------------------------------------------------- queries
    def _visible(self) -> list[SkillState]:
        return [self._states[name] for name in sorted(self._states)]

    def catalog(self) -> tuple[SkillState, ...]:
        """Skills that may appear in L0, with availability refreshed for this turn."""
        available = self._tool_names()
        out: list[SkillState] = []
        for state in self._visible():
            if state.status not in ("active", "disabled", "stale", "unavailable"):
                continue
            out.append(self._availability(state, available) if state.status == "active" else state)
        return tuple(out)

    def get(self, name: str, *, include_pending: bool = False) -> SkillState | None:
        state = self._states.get(name)
        if state is None and include_pending:
            state = self._pending.get(name)
        return state

    def pending(self, name: str) -> SkillState | None:
        """The pending proposal for `name`, or None.

        `get(include_pending=True)` returns the ACTIVE state when both exist, so
        a reviewer has no way to see the proposal's body through `get`. The
        admin detail view needs it for the diff; nothing is parsed here.
        """
        return self._pending.get(name)

    def list_all(self, include_pending: bool = False) -> list[SkillSummary]:
        states = list(self._visible())
        if include_pending:
            states.extend(self._pending[name] for name in sorted(self._pending))
        return [
            SkillSummary(
                name=s.name,
                description=s.description,
                revision=s.revision,
                status=s.status,
                origin=s.origin,
                available=s.available,
                unavailable_reason=s.unavailable_reason,
                last_used_at=s.last_used_at,
                fail_count=s.fail_count,
                derived_from=s.derived_from,
                hardware=s.hardware,
            )
            for s in sorted(states, key=lambda s: s.name)
        ]

    def snapshot(self) -> dict[str, Any]:
        """C-11 catalog snapshot: `{name, revision, status}` sorted by name."""
        entries = [s.catalog_entry() for s in self._visible() if s.status != "invalid"]
        canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "apiVersion": "vade/skill-snapshot/v1",
            "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "entries": entries,
        }

    def check(self) -> list[dict[str, Any]]:
        """`python -m vade.skill --check` rows; never starts the service."""
        return [
            {
                "name": s.name,
                "revision": s.revision,
                "status": s.status,
                "marker": s.marker,
                "triggers": list(s.triggers),
                "requires_tools": list(s.requires_tools),
                "unavailable_reason": s.unavailable_reason,
                "truncated": s.truncated,
            }
            for s in self.catalog()
        ]

    # ----------------------------------------------------------------- render
    @property
    def injection(self) -> str:
        """`prompt.injection` (RFC-0003 §7): `legacy` (default) or `layered`."""
        value = self._cfg("prompt.injection", "legacy")
        return "layered" if str(value or "").strip().lower() == "layered" else "legacy"

    def render(self, parts: Any) -> Any:
        """Prompt hook. Reads only `parts.user_message`, returns a replaced copy.

        No per-session state lives here: two concurrent sessions calling this at
        the same time cannot bleed into each other. In layered mode the two
        system slots stay EMPTY (§5.3): the catalog moves to a durable user
        message the loop sends, and bodies move to `skill_content` (@name) or
        the `skill_load` tool result.
        """
        states = self.catalog()
        activation = select(getattr(parts, "user_message", "") or "", states, limit=self.max_activated)
        if self.injection == "layered":
            return self._render_layered(parts, states, activation)
        catalog, l0_dropped = skill_render.render_catalog(
            states, activation.names, token_budget=self.l0_token_budget,
            hits=activation.hits,
        )
        bodies, injected, l1_dropped = skill_render.render_bodies(
            states, activation.names, budget_chars=self.body_budget_chars
        )
        if l0_dropped or l1_dropped:
            event = skill_audit.trim_event(l0_dropped, l1_dropped)
            log.warning(
                event["event"],
                extra={"event": event["event"], "code": None,
                       "msg": f"l0={event['l0_dropped']} l1={event['l1_dropped']}"},
            )
        self._note_used(injected)
        return replace(
            parts,
            skill_catalog=catalog,
            skill_bodies=bodies,
            activated_skills=tuple(sorted(injected)),
        )

    def _render_layered(self, parts: Any, states: Any, activation: Any) -> Any:
        """Layered hook (§4.2.3): only @name mentions get their body, and it
        travels as a user-role `<skill_content>` message, not a system section.
        Trigger hits are marked `[match]` in the catalog message instead — the
        model is expected to call `skill_load` itself (the U1-0 fallback path,
        if activation drops, is to preload trigger hits here again).
        """
        mentioned = tuple(activation.mentioned)
        content = skill_render.render_content(
            states, mentioned, budget_chars=self.body_budget_chars
        )
        self._note_used(mentioned)
        return replace(
            parts,
            skill_catalog="",
            skill_bodies="",
            skill_content=content,
            activated_skills=tuple(sorted(mentioned)),
        )

    # ------------------------------------------------------- catalog message
    def catalog_digest(self, states: Any = None) -> str:
        """RFC-0003 §4.2.1: sha256 over name + clipped description + marker,
        name order. Envelope wording and `[match]` markers stay OUT of it, so
        rewording never resends and per-turn trigger hits never poison it."""
        from ..loop.catalog import CatalogEntry, catalog_digest as _digest

        entries = [
            CatalogEntry(name=str(s.name), description=str(s.description), marker=str(s.marker))
            for s in (states if states is not None else self.catalog())
        ]
        return _digest(entries)

    def catalog_message(self, states: Any, last: str | None = None) -> str | None:
        """RFC-0003 §5.3: the full replacement message, or `None` = do not
        resend (`last` is the digest of the log's most recent catalog message)."""
        from ..loop.catalog import CatalogEntry, catalog_message as _message

        entries = [
            CatalogEntry(name=str(s.name), description=str(s.description), marker=str(s.marker))
            for s in states
        ]
        digest = self.catalog_digest(entries)
        if last is not None and last == digest:
            return None
        return _message(entries, digest)

    def catalog_matches(self, user_text: str) -> tuple[str, ...]:
        """Names this message activates (@name + trigger hits), rank order —
        the `[match]` marker set for the catalog message, capped at
        `max_activated`."""
        matched = select(user_text or "", self.catalog(), limit=self.max_activated)
        return tuple(matched.names)

    def requires_for(self, names: Any) -> tuple[str, ...]:
        """`requires_tools` of the named skills, in order (§4.3.1 layer 2)."""
        out: list[str] = []
        for name in names or ():
            state = self._states.get(str(name))
            if state is not None:
                out.extend(str(t) for t in state.requires_tools)
        return tuple(out)

    def load_body(self, name: str, *, budget_chars: int | None = None) -> tuple[str, int, bool]:
        """RFC-0003 §4.2.4: one body per call, re-read from disk every time.

        Returns `(body, revision, truncated)`. Because the read is fresh, a
        body edit needs no version bump and no catalog refresh — catalog and
        body lifecycles are separate, like dsh's. `budget_chars` (the caller's
        per-turn remainder) may only shrink the cap, never grow it.
        """
        state = self._states.get(name)
        if state is None:
            raise VadeError("NOT_FOUND", f"no active skill named {name!r}")
        if state.status not in ("active", "stale", "unavailable"):
            raise VadeError("INVALID_ARGUMENT", f"skill {name!r} is {state.status}; not loadable")
        path = Path(state.meta.path)
        try:
            text = path.read_bytes().decode("utf-8")
            parsed = parse_document(text, required_sections=self.contract.required_sections)
        except (OSError, UnicodeDecodeError, SkillParseError) as exc:
            raise VadeError("UNAVAILABLE", f"skill body unreadable: {name}: {type(exc).__name__}") from exc
        cap = self.body_max_chars if budget_chars is None else max(0, min(int(budget_chars), self.body_max_chars))
        body, truncated, _reason = truncate_body(
            parsed.body, max_chars=cap, max_lines=self.body_max_lines,
            path=f"{name}/SKILL.md",      # relative: the hint reaches the prompt
        )
        self._note_used([name])
        return body, int(state.revision), bool(truncated)

    def _note_used(self, names: Iterable[str]) -> None:
        """In-memory only: a write per turn would be write amplification."""
        now = _now_ms()
        for name in names:
            state = self._states.get(name)
            if state is not None:
                self._states[name] = replace(state, last_used_at=now)

    def record_tool_failure(self, tool_names: list[str]) -> None:
        """ToolFailureHook: 3 declared-tool failures make a skill `stale`."""
        failing = {_norm_tool(t) for t in tool_names}
        if not failing:
            return
        for name in sorted(self._states):
            state = self._states[name]
            declared = {_norm_tool(t) for t in state.requires_tools}
            if not declared & failing:
                continue
            count = self._fail_counts.get(name, 0) + 1
            self._fail_counts[name] = count
            updated = replace(state, fail_count=count)
            if count >= 3 and updated.status == "active":
                updated = updated.with_status("stale")
                self._write_meta(updated)
                self._audit("skill_stale", name=name, fail_count=count, revision=updated.revision)
            self._states[name] = updated

    # ---------------------------------------------------------------- preview
    def preview(self, source: InstallSource) -> PreviewResult:
        material = materialize(source, self.contract)
        frontmatter, missing_sections, problems = check_source(material.text, contract=self.contract)
        name = str(frontmatter.get("name") or "")
        available = self._tool_names()
        requires = tuple(str(t) for t in (frontmatter.get("requires_tools") or ()))
        conflict = ""
        if name:
            if name in self._states:
                conflict = "active"
            elif name in self._pending:
                conflict = "pending"
        valid = not problems and bool(name)
        token = ""
        if valid:
            token = secrets.token_hex(32)
            self._previews = {
                k: v for k, v in self._previews.items() if v.expires_at > self._clock()
            }
            self._previews[token] = _Preview(name=name, digest=material.digest,
                                            expires_at=self._clock() + PREVIEW_TTL_S)
        body_head = self._body_head(material.text)
        return PreviewResult(
            frontmatter=frontmatter,
            body_head=body_head,
            missing_tools=self._missing_tools(requires, available),
            conflict=conflict,
            preview_token=token,
            valid=valid,
            missing_sections=missing_sections,
            files=material.entries,
            name=name,
        )

    @staticmethod
    def _body_head(text: str, limit: int = 400) -> str:
        body = text
        if body.startswith("---"):
            end = body.find("\n---", 3)
            if end != -1:
                body = body[end + 4:]
        return body.strip()[:limit]

    # ---------------------------------------------------------------- install
    def install(self, source: InstallSource, preview_token: str) -> SkillState:
        """Land a validated skill in `skills/pending/<name>/`. Never active."""
        material = materialize(source, self.contract)
        preview = self._previews.pop(preview_token, None)
        if preview is None:
            _reject("preview_token is unknown or already used; call preview() first")
        if preview.expires_at <= self._clock():
            _reject("preview_token expired; call preview() again")
        frontmatter, missing_sections, problems = check_source(material.text, contract=self.contract)
        name = str(frontmatter.get("name") or "")
        if problems:
            _reject("invalid skill: " + "; ".join(problems)[:240])
        if name != preview.name or material.digest != preview.digest:
            _reject("preview_token does not match this content")
        reason = _bad_name(name, self.contract.reserved_names)
        if reason:
            # C-11: "Reserved install directories are not legal names."
            _reject(f"invalid skill name: {reason}")

        pending_root = self.skills_dir / "pending"
        # mkdir's mode is masked by umask and only applies to the leaf, so both
        # levels are chmod-ed explicitly: skill text is prompt content, not
        # world-readable data.
        for directory in (self.skills_dir, pending_root):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        target = pending_root / name
        if target.exists():
            self._move_to(self.skills_dir / ".trash", target, reason="pending-replaced")
        staging = pending_root / f".{name}.tmp-{secrets.token_hex(4)}"
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(staging, 0o700)
        try:
            _atomic_write(staging / "SKILL.md", material.text.encode("utf-8"))
            for relative, data in sorted(material.files.items()):
                destination = staging / relative
                _atomic_write(destination, data)
            existing = self._states.get(name)
            revision = existing.revision if existing is not None else 0
            state = SkillState(
                meta=SkillMeta(name=name, description=str(frontmatter["description"]),
                               when_to_use=str(frontmatter["when_to_use"]),
                               triggers=tuple(str(t) for t in (frontmatter.get("triggers") or ())),
                               requires_tools=tuple(str(t) for t in (frontmatter.get("requires_tools") or ())),
                               body=material.text, path=target / "SKILL.md"),
                revision=revision,
                status="pending",
                origin="install",
                sha256=hashlib.sha256(material.text.encode("utf-8")).hexdigest(),
            )
            _atomic_write(staging / "meta.json",
                          (json.dumps(state.meta_json(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            os.chmod(staging, 0o700)
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        self._pending[name] = state
        self._audit("skill.install", name=name, revision=revision, source=material.source)
        return state

    # ------------------------------------------------------------ lifecycle
    def approve(self, name: str) -> SkillState:
        """Promote `pending/<name>` to `skills/<name>` — if the bytes still match.

        Two gates before anything moves:

        * the name must be one path component (`_require_name`), because the
          directory is renamed with `os.replace`;
        * `meta.json`'s `sha256` must equal the file's digest. `docs/agents/24
          §T5` forbids approving a pending file that changed after intake: the
          reviewer approved the bytes they were shown, not whatever is on disk
          now. This is the *only* implementation of approve, so the check has to
          be here — `ProposalReview.approve` is a wrapper, not a second gate.
        """
        self._require_name(name)
        pending = self._pending.get(name)
        if pending is None:
            raise VadeError("NOT_FOUND", f"no pending skill named {name!r}")
        source_dir = self._contained(self.skills_dir / "pending" / name,
                                    self.skills_dir / "pending")
        if not source_dir.is_dir():
            raise VadeError("NOT_FOUND", f"pending directory for {name!r} is gone")
        self._verify_pending_digest(name, source_dir)
        target = self.skills_dir / name
        previous = self._states.get(name)
        parked = self._park_superseded(source_dir, name)
        if target.exists():
            self._move_to(self.skills_dir / ".trash", target,
                          reason=f"replaced-rev{previous.revision if previous else 0}")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(source_dir, target)
        revision = (previous.revision if previous else pending.revision) + 1
        approved = replace(pending, revision=revision, status="active")
        approved = approved.with_meta(replace(approved.meta, path=target / "SKILL.md"))
        self._write_meta(approved)
        self._write_history(name, revision, (target / "SKILL.md").read_bytes())
        self._pending.pop(name, None)
        self._fail_counts.pop(name, None)
        self.reload()
        self._audit("skill.approve", name=name, revision=revision, origin=approved.origin,
                    superseded_parked=len(parked) or None)
        return self.get(name) or approved

    def _verify_pending_digest(self, name: str, source_dir: Path) -> None:
        """Refuse to promote bytes that no longer match the intake record."""
        try:
            data = json.loads((source_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return                      # no record (legacy install): nothing to compare
        declared = data.get("sha256") if isinstance(data, Mapping) else None
        if not isinstance(declared, str) or not declared:
            return
        try:
            current = hashlib.sha256((source_dir / "SKILL.md").read_bytes()).hexdigest()
        except OSError as exc:
            raise VadeError("NOT_FOUND", f"pending SKILL.md for {name!r} is gone") from exc
        if current != declared:
            raise VadeError("TOOL_CONFLICT",
                            f"pending content for {name!r} changed since intake; resubmit the proposal")

    def _park_superseded(self, source_dir: Path, name: str) -> tuple[str, ...]:
        """Move `*.superseded` out of the pending dir before it is promoted.

        Intake keeps the previous proposal as `SKILL.md.superseded`; renaming the
        directory would carry those files into `skills/<name>/`, where they are
        not content but history. `.trash` keeps them restorable.
        """
        parked: list[str] = []
        for stale in sorted(source_dir.glob("*.superseded")):
            trash = self.skills_dir / ".trash"
            trash.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(trash, 0o700)
            destination = trash / f"{name}-{stale.name}-{int(self._clock())}"
            suffix = 1
            while destination.exists():
                destination = trash / f"{name}-{stale.name}-{int(self._clock())}-{suffix}"
                suffix += 1
            os.replace(stale, destination)
            parked.append(str(destination))
        return tuple(parked)

    def reject(self, name: str, reason: str = "") -> None:
        self._require_name(name)
        pending_dir = self._contained(self.skills_dir / "pending" / name,
                                     self.skills_dir / "pending")
        if not pending_dir.is_dir():
            raise VadeError("NOT_FOUND", f"no pending skill named {name!r}")
        self._move_to(self.skills_dir / ".rejected", pending_dir, reason=reason or "rejected")
        self._pending.pop(name, None)
        self._audit("skill.reject", name=name)

    def uninstall(self, name: str) -> None:
        self._require_name(name)
        target = self.skills_dir / name
        pending_dir = self.skills_dir / "pending" / name
        moved = False
        for directory in (target, pending_dir):
            if directory.is_dir():
                self._move_to(self.skills_dir / ".trash", directory, reason="uninstall")
                moved = True
        if not moved:
            raise VadeError("NOT_FOUND", f"no skill named {name!r}")
        self._states.pop(name, None)
        self._pending.pop(name, None)
        self._audit("skill.uninstall", name=name)

    def set_status(self, name: str, status: str) -> SkillState:
        if status not in ("active", "disabled", "stale"):
            raise VadeError("INVALID_ARGUMENT", f"status must be active|disabled|stale, got {status!r}")
        state = self._states.get(name)
        if state is None:
            raise VadeError("NOT_FOUND", f"no skill named {name!r}")
        updated = replace(state, status=status, fail_count=0 if status == "active" else state.fail_count)
        if status == "active":
            self._fail_counts.pop(name, None)
        self._states[name] = updated
        self._write_meta(updated)
        self._audit("skill.status", name=name, status=status)
        return updated

    def rollback(self, name: str, revision: int) -> SkillState:
        """Restore `.history/<name>/<revision>.md` as a **new** revision.

        History is append-only: rolling back to 1 after 2 produces revision 3
        whose bytes equal revision 1's. Nothing is overwritten, so the mistake
        that prompted the rollback stays auditable.
        """
        self._require_name(name)
        current = self._states.get(name)
        if current is None:
            raise VadeError("NOT_FOUND", f"no skill named {name!r}")
        history = self.skills_dir / ".history" / name / f"{int(revision)}.md"
        if not history.is_file():
            raise VadeError("NOT_FOUND", f"no history revision {revision} for {name!r}")
        raw = history.read_bytes()
        try:
            text = raw.decode("utf-8")
            parsed = parse_document(text, required_sections=self.contract.required_sections)
            validate_frontmatter(parsed.frontmatter)
        except (UnicodeDecodeError, SkillParseError, SchemaError) as exc:
            raise VadeError("INVALID_ARGUMENT", f"history revision is not a valid skill: {exc}") from exc
        if parsed.missing_sections:
            raise VadeError("INVALID_ARGUMENT",
                            "history revision is missing sections: " + ", ".join(parsed.missing_sections))
        target = self.skills_dir / name / "SKILL.md"
        _atomic_write(target, raw)
        new_revision = current.revision + 1
        restored = replace(current, revision=new_revision,
                           sha256=hashlib.sha256(raw).hexdigest())
        self._write_meta(restored)
        self._write_history(name, new_revision, raw)
        self.reload()
        self._audit("skill.rollback", name=name, revision=new_revision, from_revision=int(revision))
        return self.get(name) or restored

    # ---------------------------------------------------------------- history
    def _write_history(self, name: str, revision: int, raw: bytes) -> None:
        directory = self.skills_dir / ".history" / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        _atomic_write(directory / f"{revision}.md", raw)

    # ----------------------------------------------------------------- moving
    def _move_to(self, root: Path, directory: Path, *, reason: str) -> Path:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        destination = root / f"{directory.name}-{int(self._clock())}"
        suffix = 1
        while destination.exists():
            destination = root / f"{directory.name}-{int(self._clock())}-{suffix}"
            suffix += 1
        os.replace(directory, destination)
        self._audit("skill.moved", name=directory.name, reason=reason, path=str(destination))
        return destination

    def _expire_pending(self, root: Path) -> None:
        pending_root = root / "pending"
        if not pending_root.is_dir():
            return
        cutoff = self._clock() - self.pending_ttl_days * 86_400
        for entry in sorted(pending_root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                self._move_to(root / ".trash", entry, reason="pending_expired")
                self._audit("skill.pending_expired", name=entry.name)

