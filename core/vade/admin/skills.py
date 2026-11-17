"""`/admin/api/skills*` — the admin surface over C3b's `SkillLoader`.

Every read and every mutation goes through the loader: `list_all`, `preview`,
`install`, `approve`, `reject`, `rollback`, `uninstall`, `set_status`. This
module never opens `SKILL.md` and never re-implements frontmatter validation —
two copies of those rules would drift, and the loader is also the only owner of
`pending/`, `.history/`, `.trash/` and `.rejected/`.

Two things are thin here on purpose:

* the install stash. `SkillLoader.install` wants the source bytes *and* the
  preview token, while the schema's install body carries only the token. The
  token is bound to `(name, sha256(content))` by the loader, so keeping the
  source in this process for the same 60 s the loader keeps the token is not a
  second validation rule — it is the same content handed back to the same gate.
* `pending_diff`. The diff is computed here (the UI must not parse YAML), from
  bodies the loader already truncated, so a hostile 8 MB skill cannot blow up
  the response.
"""

from __future__ import annotations

import difflib
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..errors import VadeError
from ..skill.loader import InstallSource

__all__ = ["build_routes", "PREVIEW_TTL_S"]

#: Mirrors `SkillLoader`'s own preview TTL. Longer here would be a lie: the
#: loader refuses an expired token no matter what this stash still holds.
PREVIEW_TTL_S = 60.0
#: Bounded so a preview loop cannot grow the process's memory without limit:
#: a body is at most 1 MiB, so this caps the stash at ~16 MiB.
STASH_LIMIT = 16
MAX_DIFF_CHARS = 20_000
#: `schema/admin-api.yaml#SkillSummary.status`. `invalid` lives in the loader's
#: `_invalid` map and never reaches a state; anything unexpected is reported as
#: unavailable rather than inventing a status the UI cannot render.
STATUSES = frozenset({"active", "pending", "disabled", "stale", "unavailable"})


class _PreviewStash:
    """`preview_token -> InstallSource`, one-shot, 60 s, per admin app."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, InstallSource]] = {}

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, (expires, _s) in self._items.items() if expires <= now]:
            self._items.pop(token, None)

    def put(self, token: str, source: InstallSource) -> None:
        self._prune()
        while len(self._items) >= STASH_LIMIT:
            self._items.pop(next(iter(self._items)))
        self._items[token] = (time.time() + PREVIEW_TTL_S, source)

    def pop(self, token: str) -> InstallSource | None:
        self._prune()
        item = self._items.pop(token, None)
        return item[1] if item is not None else None


# ------------------------------------------------------------------ helpers
def _loader(ctx: Any) -> Any:
    loader = getattr(ctx, "skills", None)
    if loader is None:
        # A build without C3b keeps the honest 501: the schema declares these
        # routes, and "not in this build" is a label the UI can show.
        raise VadeError("UNSUPPORTED", "skill loader is not installed")
    return loader


def _status(value: Any) -> str:
    text = str(value or "")
    return text if text in STATUSES else "unavailable"


def _summary(state: Any) -> dict[str, Any]:
    """One row for `SkillSummary`. Works for `SkillState` and `SkillSummary`."""
    return {
        "name": str(getattr(state, "name", "")),
        "description": str(getattr(state, "description", "")),
        "revision": int(getattr(state, "revision", 0) or 0),
        "status": _status(getattr(state, "status", "")),
        "origin": str(getattr(state, "origin", "manual")),
        "available": bool(getattr(state, "available", True)),
        "unavailable_reason": str(getattr(state, "unavailable_reason", "") or ""),
        "last_used_at": getattr(state, "last_used_at", None),
        "fail_count": int(getattr(state, "fail_count", 0) or 0),
        "derived_from": getattr(state, "derived_from", None),
        "hardware": getattr(state, "hardware", None),
    }


def _pending_state(loader: Any, name: str) -> Any:
    """The pending proposal for `name`, or None.

    `get(include_pending=True)` returns the ACTIVE state when both exist, so the
    loader exposes `pending()` (a read-only accessor over the same map). Older
    loaders fall back to `get`, which is still correct when nothing is active.
    """
    accessor = getattr(loader, "pending", None)
    if callable(accessor):
        return accessor(name)
    state = loader.get(name, include_pending=True)
    return state if state is not None and getattr(state, "status", "") == "pending" else None


def _history(loader: Any, name: str) -> list[dict[str, Any]]:
    """Revision index from `.history/<name>/`, names only — never the content."""
    root = getattr(loader, "skills_dir", None)
    if root is None:
        return []
    directory = Path(root) / ".history" / name
    try:
        entries = sorted(directory.glob("*.md"))
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for entry in entries:
        stem = entry.stem
        if not stem.isdigit():
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        rows.append({"revision": int(stem), "bytes": size})
    rows.sort(key=lambda row: row["revision"])
    return rows


def _diff(active: Any, pending: Any) -> str | None:
    before = active.body.splitlines() if active is not None else []
    after = pending.body.splitlines()
    text = "\n".join(difflib.unified_diff(
        before, after,
        fromfile=f"{pending.name}@{active.revision}" if active is not None else "/dev/null",
        tofile=f"{pending.name}@pending",
        lineterm="",
    ))
    if not text:
        return None
    return text[:MAX_DIFF_CHARS]


def _integer(body: Mapping[str, Any], field: str) -> int:
    value = body.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise VadeError("INVALID_ARGUMENT", f"{field} must be an integer")
    if value < 0:
        raise VadeError("INVALID_ARGUMENT", f"{field} must be >= 0")
    return value


def _source(body: Mapping[str, Any]) -> InstallSource:
    kind = body.get("source")
    if kind == "upload":
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise VadeError("INVALID_ARGUMENT", "content is required when source=upload")
        return InstallSource(kind="text", text=content)
    if kind == "url":
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise VadeError("INVALID_ARGUMENT", "url is required when source=url")
        return InstallSource(kind="url", url=url.strip())
    raise VadeError("INVALID_ARGUMENT", "source must be one of upload|url")


def _plain(value: Any) -> Any:
    """Frontmatter is YAML: a date or a custom scalar must not 500 the route."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {}


def _audit(ctx: Any, session: Any, event: str, **fields: Any) -> None:
    ctx.write_audit(
        kind=event, status="ok", actor=getattr(session, "device_id", None) or "loopback_admin",
        extra=fields,
    )


# ------------------------------------------------------------------- routes
def build_routes(ctx: Any, guard: Callable[..., Any],
                 read_json: Callable[..., Any]) -> list[Route]:
    """The seven `/admin/api/skills*` paths, wired onto `ctx.skills`."""
    stash = _PreviewStash()

    async def list_skills(request: Request) -> Response:
        guard(request)
        loader = _loader(ctx)
        # Pending IS a status the list must show (the UI approves from here);
        # `loader.list_all()` without the flag is what proves install is inert.
        return JSONResponse({"items": [_summary(s) for s in loader.list_all(include_pending=True)]})

    async def preview_install(request: Request) -> Response:
        guard(request)
        loader = _loader(ctx)
        body = await read_json(request)
        source = _source(body)
        result = loader.preview(source)
        if result.preview_token:
            stash.put(result.preview_token, source)
        return JSONResponse({
            "preview_token": result.preview_token,
            "name": result.name,
            "frontmatter": _plain(dict(result.frontmatter)),
            "excerpt": result.body_head,
            "missing_tools": list(result.missing_tools),
            "conflict": bool(result.conflict),
            "conflict_with": result.conflict or None,
            "valid": result.valid,
            "missing_sections": list(result.missing_sections),
            "files": list(result.files),
        })

    async def install(request: Request) -> Response:
        session = guard(request)
        loader = _loader(ctx)
        body = await read_json(request)
        token = body.get("preview_token")
        if not isinstance(token, str) or not token.strip():
            # 31-C5a §T4: "没有 preview_token 一律拒绝" — server side, not a
            # disabled button on the client.
            raise VadeError("INVALID_ARGUMENT",
                            "preview_token is required; call /admin/api/skills/install/preview first")
        token = token.strip()
        source = stash.pop(token)
        if source is None:
            raise VadeError("INVALID_ARGUMENT",
                            "preview_token is unknown or expired; preview the skill again")
        state = loader.install(source, token)
        _audit(ctx, session, "skill.install", name=state.name, revision=state.revision)
        return JSONResponse(_summary(state))

    async def get_skill(request: Request) -> Response:
        guard(request)
        loader = _loader(ctx)
        name = request.path_params["name"]
        state = loader.get(name, include_pending=True)
        if state is None:
            raise VadeError("NOT_FOUND", f"no skill named {name!r}")
        pending = _pending_state(loader, name)
        active = loader.get(name)
        return JSONResponse({
            **_summary(state),
            "when_to_use": str(getattr(state, "when_to_use", "")),
            "triggers": list(getattr(state, "triggers", ()) or ()),
            "requires_tools": list(getattr(state, "requires_tools", ()) or ()),
            "body": getattr(state, "body", ""),
            "pending_diff": _diff(active, pending) if pending is not None else None,
            "pending_revision": getattr(pending, "revision", None),
            "history": _history(loader, str(getattr(state, "name", name))),
        })

    async def patch_skill(request: Request) -> Response:
        session = guard(request)
        loader = _loader(ctx)
        name = request.path_params["name"]
        body = await read_json(request)
        status = body.get("status")
        if status not in ("active", "disabled"):
            raise VadeError("INVALID_ARGUMENT",
                            "status must be active|disabled; use approve to accept a proposal")
        state = loader.set_status(name, str(status))
        _audit(ctx, session, "skill.status", name=name, status=status)
        return JSONResponse(_summary(state))

    async def delete_skill(request: Request) -> Response:
        session = guard(request)
        loader = _loader(ctx)
        name = request.path_params["name"]
        loader.uninstall(name)
        _audit(ctx, session, "skill.uninstall", name=name)
        return JSONResponse({"ok": True})

    async def approve_skill(request: Request) -> Response:
        session = guard(request)
        loader = _loader(ctx)
        name = request.path_params["name"]
        body = await read_json(request)
        revision = _integer(body, "revision")
        # The revision the reviewer saw is a CAS token, and the loader checks it
        # inside the lock that also moves the proposal. Comparing
        # `pending.revision` here and calling `approve` after would be a TOCTOU:
        # an install landing in between promotes bytes nobody read.
        state = loader.approve(name, expect_revision=revision)
        _audit(ctx, session, "skill.approve", name=name, revision=state.revision,
               reviewed_revision=revision)
        return JSONResponse(_summary(state))

    async def reject_skill(request: Request) -> Response:
        session = guard(request)
        loader = _loader(ctx)
        name = request.path_params["name"]
        body = await read_json(request)
        revision = _integer(body, "revision")
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise VadeError("INVALID_ARGUMENT", "reason is required to reject a proposal")
        loader.reject(name, reason.strip()[:300], expect_revision=revision)
        _audit(ctx, session, "skill.reject", name=name, revision=revision)
        return JSONResponse({"ok": True})

    async def rollback_skill(request: Request) -> Response:
        session = guard(request)
        loader = _loader(ctx)
        name = request.path_params["name"]
        body = await read_json(request)
        to_revision = _integer(body, "to_revision")
        state = loader.rollback(name, to_revision)
        _audit(ctx, session, "skill.rollback", name=name, revision=state.revision,
               from_revision=to_revision)
        return JSONResponse(_summary(state))

    return [
        # `/install*` must precede `/{name}`: Starlette matches in order, and
        # `install` is not a skill name.
        Route("/admin/api/skills", list_skills, methods=["GET"]),
        Route("/admin/api/skills/install/preview", preview_install, methods=["POST"]),
        Route("/admin/api/skills/install", install, methods=["POST"]),
        Route("/admin/api/skills/{name}", get_skill, methods=["GET"]),
        Route("/admin/api/skills/{name}", patch_skill, methods=["PATCH"]),
        Route("/admin/api/skills/{name}", delete_skill, methods=["DELETE"]),
        Route("/admin/api/skills/{name}/approve", approve_skill, methods=["POST"]),
        Route("/admin/api/skills/{name}/reject", reject_skill, methods=["POST"]),
        Route("/admin/api/skills/{name}/rollback", rollback_skill, methods=["POST"]),
    ]
