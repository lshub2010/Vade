"""Risk judgement (C5a T2). The module's manifest declares a risk; this decides.

Rules (31-C5a §T2):

* the manifest value is a *declaration*, never the last word;
* `[policy.tool_risk]` overrides may only raise (`RISK_DOWNGRADE_FORBIDDEN`);
* a hard-coded floor keeps `shell`/`write`/`delete`/`forget` at `high` even if
  a module (or a careless override) says `low`;
* an unknown risk string is treated as `high` — fail closed, never "unknown
  therefore low".
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ..errors import VadeError

__all__ = ["PolicyEngine", "RISK_RANK", "HIGH_FLOOR_TOOLS", "risk_floor", "effective_risk"]

RISK_RANK = {"low": 0, "medium": 1, "high": 2}

#: Tools that may never drop below `high`, whatever the manifest says. Mirrors
#: C2's registry floor on purpose: two independent gates, same answer.
HIGH_FLOOR_TOOLS = frozenset({
    "shell", "shell_exec", "exec", "write", "write_file", "delete", "delete_file", "forget",
})

_GENERIC_TAILS = {"write", "delete", "forget", "exec", "shell"}


def risk_floor(tool_id: str) -> str:
    bare = tool_id.rsplit("/", 1)[-1]
    tail = bare.rsplit("_", 1)[-1]
    if bare in HIGH_FLOOR_TOOLS or tail in _GENERIC_TAILS:
        return "high"
    return "low"


def _rank(risk: str) -> int:
    return RISK_RANK.get(str(risk).lower(), RISK_RANK["high"])


def effective_risk(declared: str, tool_id: str, overrides: Mapping[str, str] | None = None) -> str:
    risk = str(declared).lower()
    if risk not in RISK_RANK:
        risk = "high"
    candidates = [risk, risk_floor(tool_id)]
    for key in (tool_id, tool_id.rsplit("/", 1)[-1]):
        if overrides and key in overrides:
            candidates.append(str(overrides[key]).lower())
    return max(candidates, key=_rank)


class PolicyEngine:
    """Holds the override table and answers "does this need a human?"."""

    def __init__(
        self,
        tool_risk: Mapping[str, str] | None = None,
        *,
        medium_requires_approval: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._overrides: dict[str, str] = {}
        for key, value in (tool_risk or {}).items():
            if str(value).lower() not in RISK_RANK:
                raise VadeError("INVALID_ARGUMENT", f"policy.tool_risk[{key!r}] must be low|medium|high")
            self._overrides[str(key)] = str(value).lower()
        self.medium_requires_approval = medium_requires_approval
        self._clock = clock

    # ------------------------------------------------------------------ queries
    def risk_of(self, tool_id: str, declared: str) -> str:
        return effective_risk(declared, tool_id, self._overrides)

    def requires_approval(self, risk: str) -> bool:
        rank = _rank(risk)
        if rank >= RISK_RANK["high"]:
            return True
        return rank >= RISK_RANK["medium"] and self.medium_requires_approval

    # ------------------------------------------------------------------ mutation
    def set_override(self, tool_id: str, risk: str) -> None:
        """Raise-only, same rule as `module/capabilities.update` (31 §T2)."""
        risk = str(risk).lower()
        if risk not in RISK_RANK:
            raise VadeError("INVALID_ARGUMENT", "risk must be low|medium|high")
        current = self.risk_of(tool_id, "low")
        if _rank(risk) < _rank(current):
            raise VadeError(
                "RISK_DOWNGRADE_FORBIDDEN",
                f"{tool_id} effective risk is {current}; override to {risk} would lower it",
            )
        self._overrides[tool_id] = risk

    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)
