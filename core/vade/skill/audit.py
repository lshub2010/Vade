"""Secret scanning + the only log/audit surface a skill body may touch.

Rules (docs/agents/23 §T4):
  * scan on load; on a hit **warn and count**, never rewrite and never block
    (the patterns false-positive: a skill may legitimately document a fake key);
  * logs carry `path` and `sha256(body)[:8]`, **never** the body;
  * `turn.completed` carries skill *names* only.

`scan_secrets` is deliberately conservative: over-redacting a skill body would
silently change the instructions the model receives.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = ["SecretFinding", "SECRET_PATTERNS", "scan_secrets", "body_ref", "trim_event"]


@dataclass(frozen=True, slots=True)
class SecretFinding:
    kind: str
    count: int


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("private_key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    # 200+ chars of a base64 alphabet, optionally padded. Requires a boundary so
    # a long identifier inside prose is not counted as a blob.
    ("base64_blob", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{200,}={0,2}")),
)


def scan_secrets(text: str) -> tuple[SecretFinding, ...]:
    findings = []
    for kind, pattern in SECRET_PATTERNS:
        count = len(pattern.findall(text))
        if count:
            findings.append(SecretFinding(kind, count))
    return tuple(findings)


def body_ref(path: str | Path, body: str) -> dict[str, Any]:
    """The only body-derived values allowed in a log line."""
    return {"path": str(path), "body_sha8": hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]}


def trim_event(l0_dropped: Iterable[str], l1_dropped: Iterable[str]) -> dict[str, Any]:
    """`skill_render_trimmed` payload: names only, never text."""
    return {
        "event": "skill_render_trimmed",
        "l0_dropped": sorted(l0_dropped),
        "l1_dropped": sorted(l1_dropped),
    }


def audit_names(state: Mapping[str, Any] | Any) -> list[str]:
    """Normalise an audit payload to `{"skills": [names]}` input."""
    names = getattr(state, "activated_skills", None)
    if names is None and isinstance(state, Mapping):
        names = state.get("skills")
    return sorted(str(n) for n in (names or ()))
