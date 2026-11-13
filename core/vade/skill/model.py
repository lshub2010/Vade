"""Skill data model: frontmatter -> `SkillMeta`, plus the runtime `SkillState`.

`SkillMeta` is exactly the shape docs/agents/23-C3b-skill-loader.md §四 asks for.
`SkillState` adds the version fields C-11's catalog snapshot exposes
(`revision`, `status`) and the runtime metadata kept in `meta.json`.

Nothing here executes, imports or evaluates a skill. A skill is text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

__all__ = [
    "SkillMeta", "SkillState", "SkillSummary", "ParsedSkill", "SkillParseError",
    "split_frontmatter", "parse_document", "scan_sections", "truncate_body",
    "normalise_heading", "BLOCK_SCALAR_KEYS",
]

#: Frontmatter keys whose YAML value is a string, not a list.
BLOCK_SCALAR_KEYS = ("name", "description", "when_to_use")

_HEADING = re.compile(r"^(#{1,3})[ \t]+(.+?)[ \t]*#*[ \t]*$")


class SkillParseError(ValueError):
    """Malformed frontmatter. The loader turns this into a warning + skip."""


@dataclass(frozen=True, slots=True)
class SkillMeta:
    name: str
    description: str
    when_to_use: str
    triggers: tuple[str, ...] = ()
    requires_tools: tuple[str, ...] = ()
    body: str = ""
    truncated: bool = False
    path: Path = Path(".")
    available: bool = True
    unavailable_reason: str = ""


@dataclass(frozen=True, slots=True)
class SkillState:
    """`SkillMeta` + the fields C-11 snapshots and `meta.json` persists."""

    meta: SkillMeta
    revision: int = 0
    status: str = "active"
    origin: str = "manual"
    last_used_at: int | None = None
    fail_count: int = 0
    derived_from: str | None = None
    hardware: str | None = None
    sha256: str = ""
    sections: tuple[str, ...] = ()

    # ------------------------------------------------------------- delegating
    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def description(self) -> str:
        return self.meta.description

    @property
    def when_to_use(self) -> str:
        return self.meta.when_to_use

    @property
    def triggers(self) -> tuple[str, ...]:
        return self.meta.triggers

    @property
    def requires_tools(self) -> tuple[str, ...]:
        return self.meta.requires_tools

    @property
    def body(self) -> str:
        return self.meta.body

    @property
    def truncated(self) -> bool:
        return self.meta.truncated

    @property
    def path(self) -> Path:
        return self.meta.path

    @property
    def available(self) -> bool:
        return self.meta.available

    @property
    def unavailable_reason(self) -> str:
        return self.meta.unavailable_reason

    # ------------------------------------------------------------------ state
    @property
    def injectable(self) -> bool:
        """Only `active` + available skills get their body injected."""
        return self.status == "active" and self.meta.available

    @property
    def marker(self) -> str:
        """Catalog suffix: empty, `[unavailable]`, `[stale]` or `[disabled]`."""
        if self.status == "stale":
            return "[stale]"
        if self.status == "disabled":
            return "[disabled]"
        if self.status == "unavailable" or not self.meta.available:
            return "[unavailable]"
        return ""

    def with_meta(self, meta: SkillMeta) -> "SkillState":
        return replace(self, meta=meta)

    def with_status(self, status: str) -> "SkillState":
        return replace(self, status=status)

    def with_revision(self, revision: int) -> "SkillState":
        return replace(self, revision=revision)

    def catalog_entry(self) -> dict[str, Any]:
        """Exactly `{name, revision, status}` — C-11 freezes the snapshot shape."""
        return {"name": self.name, "revision": self.revision, "status": self.status}

    def meta_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "status": self.status,
            "origin": self.origin,
            "last_used_at": self.last_used_at,
            "fail_count": self.fail_count,
            "derived_from": self.derived_from,
            "hardware": self.hardware,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """`list_all()` row: no absolute path (the admin UI does not need it and it

    leaks the deployment layout); `get()` gives the core the full state.
    """

    name: str
    description: str
    revision: int
    status: str
    origin: str
    available: bool
    unavailable_reason: str = ""
    last_used_at: int | None = None
    fail_count: int = 0
    derived_from: str | None = None
    hardware: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedSkill:
    frontmatter: Mapping[str, Any]
    body: str
    sections: Mapping[str, str]
    missing_sections: tuple[str, ...]


# --------------------------------------------------------------------- parsing
def split_frontmatter(text: str) -> tuple[Mapping[str, Any], str]:
    """Split `---`-delimited YAML frontmatter from the markdown body.

    `yaml.safe_load` only: a skill file comes from disk/zip and `yaml.load`
    would be an object-construction primitive. Anything that is not a mapping
    is a parse error (the loader skips that skill, it never guesses).
    """
    text = text.lstrip("\ufeff")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillParseError("missing frontmatter (file must start with ---)")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        raise SkillParseError("unterminated frontmatter (no closing ---)")
    raw = "".join(lines[1:end])
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SkillParseError(f"invalid YAML: {str(exc).splitlines()[0][:160]}") from exc
    if data is None:
        raise SkillParseError("empty frontmatter")
    if not isinstance(data, Mapping):
        raise SkillParseError(f"frontmatter must be a mapping, got {type(data).__name__}")
    return data, "".join(lines[end + 1:]).lstrip("\n")


def normalise_heading(raw: str) -> str:
    """`## when to use:` -> `when to use` (case/space/punctuation tolerant).

    Tolerance is only about formatting. A *missing* section is still fatal:
    without `Verification` there is no way to tell whether the skill worked.
    """
    return re.sub(r"\s+", " ", raw.strip()).rstrip(":：").strip().casefold()


def scan_sections(body: str) -> dict[str, str]:
    """Map canonical `When to Use`-style names to their section text."""
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        match = _HEADING.match(line)
        if match:
            current = normalise_heading(match.group(2))
            if current in found:  # first heading wins; a repeat is not a section
                current = None
            else:
                found[current] = []
            continue
        if current is not None:
            found[current].append(line)
    return {key: "\n".join(value).strip() for key, value in found.items()}


def parse_document(text: str, *, required_sections: tuple[str, ...]) -> ParsedSkill:
    frontmatter, body = split_frontmatter(text)
    raw_sections = scan_sections(body)
    by_key = {normalise_heading(name): name for name in required_sections}
    sections: dict[str, str] = {}
    missing: list[str] = []
    for key, canonical in by_key.items():
        content = raw_sections.get(key, "").strip()
        if not content:
            missing.append(canonical)
        else:
            sections[canonical] = content
    return ParsedSkill(
        frontmatter=frontmatter,
        body=body.strip(),
        sections=sections,
        missing_sections=tuple(missing),
    )


# ------------------------------------------------------------------ truncation
def truncate_body(
    body: str, *, max_chars: int, max_lines: int, path: str | Path
) -> tuple[str, bool, str]:
    """Clip to `max_lines` / `max_chars`, appending a readable hint.

    Not an error: an over-long skill still loads, it just says where the rest
    is (`fs` reads the file). Returns `(body, truncated, reason)`.
    """
    lines = body.splitlines()
    reason = ""
    clipped = body
    if len(lines) > max_lines:
        clipped = "\n".join(lines[:max_lines])
        reason = "lines"
    if len(clipped) > max_chars:
        clipped = clipped[:max_chars]
        reason = "chars"
    if not reason:
        return body, False, ""
    if reason == "lines":
        hint = f"[... truncated at {max_lines} lines, use fs to read {path} ...]"
    else:
        hint = f"[... truncated at {max_chars} chars, use fs to read {path} ...]"
    return clipped.rstrip() + "\n\n" + hint, True, reason
