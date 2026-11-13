"""Fixtures for the C3b skill tests: build skills without touching real data."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Iterable, Mapping

from vade.skill.loader import SkillLoader

SECTIONS: Mapping[str, str] = {
    "When to Use": "The user asks about a past decision or an architecture rule.",
    "Procedure": "1. Query the knowledge base.\n2. Answer with the decision id.",
    "Pitfalls": "Do not invent a decision that is not in the store.",
    "Verification": "The answer cites a decision id that exists.",
}


def skill_text(
    name: str = "kb-guide",
    *,
    description: str = "Project knowledge base rules",
    when_to_use: str = "When the user asks about history or decisions",
    triggers: Iterable[str] = (),
    requires: Iterable[str] = (),
    sections: Mapping[str, str] | None = None,
    extra_frontmatter: str = "",
    body_prefix: str = "Intro line.\n\n",
    body_suffix: str = "",
) -> str:
    chosen = dict(SECTIONS)
    if sections is not None:
        chosen = dict(sections)
    lines = ["---", f"name: {name}", f"description: {description}", f"when_to_use: {when_to_use}"]
    triggers = list(triggers)
    requires = list(requires)
    if triggers:
        lines.append("triggers: [" + ", ".join(json.dumps(t, ensure_ascii=False) for t in triggers) + "]")
    if requires:
        lines.append("requires_tools: [" + ", ".join(json.dumps(t) for t in requires) + "]")
    if extra_frontmatter:
        lines.append(extra_frontmatter)
    lines.append("---")
    body = [*lines, "", body_prefix.rstrip("\n")]
    for heading, content in chosen.items():
        body.extend(["", f"## {heading}", content])
    if body_suffix:
        body.extend(["", body_suffix])
    return "\n".join(body) + "\n"


def write_skill(root: Path, name: str = "kb-guide", *, text: str | None = None, **kwargs) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(text if text is not None else skill_text(name, **kwargs), encoding="utf-8")
    return directory


def make_loader(root: Path, tools: Iterable[str] | None = None, **kwargs) -> SkillLoader:
    loader = SkillLoader(root, tool_snapshot=list(tools) if tools is not None else [], **kwargs)
    loader.reload()
    return loader


def zip_bytes(entries: Mapping[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def zip_with_skill(text: str, *, prefix: str = "kb-guide/", extra: Mapping[str, str | bytes] | None = None) -> bytes:
    entries: dict[str, str | bytes] = {f"{prefix}SKILL.md": text}
    for name, payload in (extra or {}).items():
        entries[f"{prefix}{name}"] = payload
    return zip_bytes(entries)
