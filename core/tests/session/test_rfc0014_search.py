"""RFC-0014: session/search — titles + live bodies, two-tier order, clipped snippets.

The two mutation targets the task pins (§8):
  ① parameterization replaced by string concatenation -> the §4.10 injection
     group (test_percent_is_literal / test_underscore_is_literal /
     test_quote_and_sql_comment_are_literal / test_backslash_is_literal) goes red;
  ② the handler's `limit <= 100` cap deleted -> test_limit_bounds_are_rejected goes red.

Conventions follow test_meta.py: sync test functions wrap an
`asyncio.run(scenario())` because dispatch runs the async handler path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from vade.session.manager import SessionLog
from vade.session.models import Message, Session
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.search import (
    LIMIT_DEFAULT, LIMIT_MAX, QUERY_MAX, SNIPPET_MAX,
    escape_like, find_ascii_ci, make_snippet,
)
from vade.session.sqlite_store import SQLiteStore
from vade.transport.connection import RpcError

SCHEMA_RPC = Path(__file__).resolve().parents[3] / "schema" / "rpc.json"

HIT_KEYS = {"session_id", "title", "updated_at", "matched_in",
            "message_id", "snippet", "snippet_offset", "archived"}


class Conn:
    connection_id = "conn-1"
    device_id = "dev-01"


class ReadOnlyConn(Conn):
    read_only = True


def _log(tmp_path) -> SessionLog:
    return SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))


async def _call(log, method: str, params: dict | None = None, conn=None):
    """One dispatch; returns the result or the RpcError (never raises)."""
    return await SessionRPC(log).dispatch(conn or Conn(), method, params or {})


def _err(result) -> RpcError:
    assert isinstance(result, RpcError), f"expected RpcError, got {result!r}"
    return result


def _ok(result) -> dict:
    assert not isinstance(result, RpcError), f"{result.code}: {result.message} ({result.data})"
    return result


async def _search(log, query: str, **params):
    out = await _call(log, "session/search", {"query": query, **params})
    return _ok(out)


def _mk_session(log, sid: str, *, title: str | None = None, created_at: int = 1,
                updated_at: int | None = None) -> None:
    log.store.create_session(Session(id=sid, created_at=created_at, title=title))
    if updated_at is not None:
        log.store.update_session_meta(sid, {"updated_at": updated_at})


def _mk_message(log, sid: str, mid: str, content: str, seq: int = 1) -> None:
    log.store.append(sid, [Message(id=mid, session_id=sid, role="user",
                                   content=content, seq=seq)], [])


def _ids(result: dict) -> list[str]:
    return [row["session_id"] for row in result["results"]]


# ------------------------------------------------------------------- query validation

def test_absent_empty_and_blank_query_are_rejected(tmp_path):
    """§4.6: absent / non-string / blank (incl. full-width space) -> INVALID_ARGUMENT."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="anything")
        for params in ({}, {"query": ""}, {"query": "   \t "}, {"query": "\u3000"},
                       {"query": None}, {"query": 42}, {"query": ["重构"]}):
            err = _err(await _call(log, "session/search", params))
            assert err.code == "INVALID_ARGUMENT", (params, err.code)
            assert err.data and err.data.get("field") == "query"

    asyncio.run(scenario())


def test_query_length_bounds_are_enforced(tmp_path):
    """§4.6: 201 code points -> INVALID_ARGUMENT with data.limit; 200 passes."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="ok")
        err = _err(await _call(log, "session/search", {"query": "a" * (QUERY_MAX + 1)}))
        assert err.code == "INVALID_ARGUMENT"
        assert err.data["limit"] == QUERY_MAX and err.data["actual"] == QUERY_MAX + 1
        result = _ok(await _call(log, "session/search", {"query": "a" * QUERY_MAX}))
        assert isinstance(result["total"], int)

    asyncio.run(scenario())


def test_no_results_is_an_empty_page_with_total_zero(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="重构方案")
        _mk_message(log, "s1", "m1", "里面的正文也不会命中")
        result = await _search(log, "zz-nowhere-to-be-found")
        assert result["results"] == [] and result["total"] == 0

    asyncio.run(scenario())


# ------------------------------------------------------------------- hit rows

def test_title_hit_row_shape(tmp_path):
    """§5: a title hit has matched_in=title and no snippet/message pointer."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="分页策略讨论", updated_at=7)
        result = await _search(log, "分页策略")
        assert result["total"] == 1
        row = result["results"][0]
        assert set(row) == HIT_KEYS
        assert row["session_id"] == "s1" and row["matched_in"] == "title"
        assert row["title"] == "分页策略讨论"
        assert row["message_id"] is None and row["snippet"] is None
        assert row["snippet_offset"] is None
        assert row["updated_at"] == 7 and row["archived"] is False

    asyncio.run(scenario())


def test_body_hit_row_carries_clipped_snippet_and_offset(tmp_path):
    """§4.7: snippet <= 120 code points, offset points into the ORIGINAL text."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", updated_at=3)
        long_body = "x" * 200 + "the needle is here " + "y" * 200
        _mk_message(log, "s1", "m1", long_body)
        result = await _search(log, "needle")
        assert result["total"] == 1
        row = result["results"][0]
        assert row["matched_in"] == "body"
        assert row["message_id"] == "m1"
        assert row["snippet_offset"] == long_body.index("needle")
        assert row["snippet"] is not None
        assert len(row["snippet"]) <= SNIPPET_MAX
        assert "needle" in row["snippet"]
        assert row["snippet"].startswith("…")  # window clipped on the left
        assert row["snippet"].endswith("…")    # and on the right
        # the offset re-locates the match in the ORIGINAL content
        assert long_body[row["snippet_offset"]:row["snippet_offset"] + 6] == "needle"

    asyncio.run(scenario())


def test_ascii_case_folding(tmp_path):
    """§4.3: LIKE semantics — ASCII folds, so 'todo list' finds 'TODO LIST'."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="TODO LIST")
        _mk_message(log, "s1", "m1", "we hit a ParseError in module B", seq=1)
        assert (await _search(log, "todo list"))["total"] == 1
        assert (await _search(log, "parseerror"))["total"] == 1
        assert (await _search(log, "PARSEerror"))["total"] == 1

    asyncio.run(scenario())


def test_chinese_matches_exactly(tmp_path):
    """§4.3: non-ASCII compares code-point exact; CJK needs no case folding."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="重构方案讨论")
        _mk_message(log, "s1", "m1", "把会话列表的重构方案放在今天做")
        _mk_session(log, "s2", updated_at=2)
        _mk_message(log, "s2", "m2", "另一个会话也提到了重构方案")
        # s1 hits on BOTH faces -> one row, title tier (§1 #5 dedup)
        first = await _search(log, "重构方案")
        assert first["total"] == 2 and _ids(first) == ["s1", "s2"]
        assert first["results"][0]["matched_in"] == "title"
        assert (await _search(log, "列表的重构"))["total"] == 1
        assert (await _search(log, "重构蓝方案"))["total"] == 0  # no fuzzy anything

    asyncio.run(scenario())


# ------------------------------------------------------------------- pagination

def test_limit_bounds_are_rejected(tmp_path):
    """§4.5: 0 / negative / over-cap / bool / str -> INVALID_ARGUMENT.

    MUTATION TARGET ②: deleting the handler's `limit <= LIMIT_MAX` check makes
    the 101 case pass and this test red.
    """
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="anything")
        for limit in (0, -1, LIMIT_MAX + 1, True, "20", 2.5):
            err = _err(await _call(log, "session/search", {"query": "anything", "limit": limit}))
            assert err.code == "INVALID_ARGUMENT", (limit, err.code)
            assert err.data and err.data.get("field") == "limit"
        # the default is LIMIT_DEFAULT; 1 and the cap itself are legal
        for limit in (1, LIMIT_MAX, None):
            params = {"query": "anything"}
            if limit is not None:
                params["limit"] = limit
            result = _ok(await _call(log, "session/search", params))
            assert isinstance(result["total"], int)

    asyncio.run(scenario())


def test_offset_must_be_non_negative(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="anything")
        err = _err(await _call(log, "session/search", {"query": "anything", "offset": -1}))
        assert err.code == "INVALID_ARGUMENT" and err.data["field"] == "offset"

    asyncio.run(scenario())


def test_pagination_pages_have_no_duplicates_or_gaps(tmp_path):
    """§4.5: limit=2 over 5 hits -> 2/2/1, union complete, total constant."""
    async def scenario():
        log = _log(tmp_path)
        for i in range(5):
            _mk_session(log, f"s{i}", created_at=i, updated_at=100 + i)
            _mk_message(log, f"s{i}", f"m{i}", f"paginated body number {i}", seq=1)
        seen: list[str] = []
        for offset in (0, 2, 4):
            result = await _search(log, "paginated", limit=2, offset=offset)
            assert result["total"] == 5
            seen.extend(_ids(result))
        assert len(seen) == 5 and len(set(seen)) == 5  # no dups, no gaps
        last = await _search(log, "paginated", limit=2, offset=4)
        assert len(last["results"]) == 1  # short page = the end
        assert (await _search(log, "paginated", limit=2, offset=5))["results"] == []

    asyncio.run(scenario())


def test_offset_beyond_total_returns_empty_page(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="lonely")
        result = await _search(log, "lonely", limit=20, offset=999)
        assert result["results"] == [] and result["total"] == 1  # total stays honest

    asyncio.run(scenario())


# ------------------------------------------------------------------- injection (mutation target ①)

def test_percent_is_literal_not_wildcard(tmp_path):
    """§4.10: '100%' must not become '%100%%' and match the world."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", updated_at=1)
        _mk_session(log, "s2", updated_at=2)
        _mk_message(log, "s1", "m1", "progress 100% done", seq=1)
        _mk_message(log, "s2", "m2", "progress 100x done", seq=1)
        hit = await _search(log, "100%")
        assert hit["total"] == 1 and _ids(hit) == ["s1"]
        both = await _search(log, "100")
        assert both["total"] == 2

    asyncio.run(scenario())


def test_underscore_is_literal(tmp_path):
    """§4.10: '_' matches a literal underscore only, never any character."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", updated_at=1)
        _mk_session(log, "s2", updated_at=2)
        _mk_message(log, "s1", "m1", "snake_case_name here", seq=1)
        _mk_message(log, "s2", "m2", "snakeXcaseXname here", seq=1)
        hit = await _search(log, "case_name")
        assert hit["total"] == 1 and _ids(hit) == ["s1"]
        assert (await _search(log, "_"))["total"] == 1 and _ids(await _search(log, "_")) == ["s1"]
        assert (await _search(log, "case"))["total"] == 2

    asyncio.run(scenario())


def test_quote_and_sql_comment_are_literal(tmp_path):
    """§4.10: ' and -- are data; even a fake DROP TABLE changes nothing."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="market notes")
        _mk_message(log, "s1", "m1", "it's a --bear-- market; stay long", seq=1)
        assert (await _search(log, "it's"))["total"] == 1
        assert (await _search(log, "--bear--"))["total"] == 1
        # the classic payloads: no error, no hit, no table mutation
        for hostile in ("'; DROP TABLE sessions; --", "'; DELETE FROM messages; --"):
            result = await _search(log, hostile)
            assert result["total"] == 0
        assert (await _search(log, "stay long"))["total"] == 1  # tables are intact

    asyncio.run(scenario())


def test_backslash_is_literal(tmp_path):
    """§4.10: the escape character itself is escaped, so paths match verbatim.

    The `\\'` case is the sharp one: with ESCAPE '\\' a query backslash must
    be doubled by `escape_like`, or it swallows the quote / breaks the SQL —
    which is exactly what mutation ① (concatenation) does and this test pins.
    """
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", updated_at=1)
        _mk_message(log, "s1", "m1", r"config path C:\Users\x stays", seq=1)
        _mk_session(log, "s2", updated_at=2)
        _mk_message(log, "s2", "m2", r"backup of C:\data, it\'s mine", seq=1)
        hit = await _search(log, r"C:\Users")
        assert hit["total"] == 1 and _ids(hit) == ["s1"]
        slash_quote = await _search(log, r"it\'s")
        assert slash_quote["total"] == 1 and _ids(slash_quote) == ["s2"]

    asyncio.run(scenario())


# ------------------------------------------------------------------- ordering & dedup

def test_title_hits_rank_before_body_hits(tmp_path):
    """§4.4: the title tier leads even when the body tier is fresher."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s_title", title="alpha planner", updated_at=100)
        _mk_session(log, "s_body", updated_at=9999)
        _mk_message(log, "s_body", "m1", "the alpha engine hums", seq=1)
        result = await _search(log, "alpha")
        assert _ids(result) == ["s_title", "s_body"]
        assert [row["matched_in"] for row in result["results"]] == ["title", "body"]

    asyncio.run(scenario())


def test_body_tier_orders_by_updated_at_desc_with_id_tiebreak(tmp_path):
    """§4.4: within the body tier, updated_at DESC then id ASC for ties.

    Messages are inserted FIRST: an append advances `updated_at` (RFC-0010
    §4.2), so the ranking keys must be stamped after the activity, exactly
    what a real workspace looks like.
    """
    async def scenario():
        log = _log(tmp_path)
        for sid, text in (("s_a", "bramble one"), ("s_b", "bramble two"),
                          ("s_c", "bramble three")):
            log.store.create_session(Session(id=sid, created_at=1))
            _mk_message(log, sid, f"m_{sid}", text, seq=1)
        log.store.update_session_meta("s_a", {"updated_at": 5000})
        log.store.update_session_meta("s_b", {"updated_at": 9000})
        log.store.update_session_meta("s_c", {"updated_at": 5000})
        result = await _search(log, "bramble")
        assert _ids(result) == ["s_b", "s_a", "s_c"]  # 9000 first; tie -> id ASC

    asyncio.run(scenario())


def test_session_matching_both_title_and_body_appears_once_as_title(tmp_path):
    """§1 #5: one session, one row; title dominates; total counts it once."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s_both", title="datalog notes", updated_at=10)
        _mk_message(log, "s_both", "m1", "datalog entry for today", seq=1)
        _mk_session(log, "s_other", updated_at=20)
        _mk_message(log, "s_other", "m2", "another datalog mention", seq=1)
        result = await _search(log, "datalog")
        assert result["total"] == 2
        assert _ids(result) == ["s_both", "s_other"]
        assert result["results"][0]["matched_in"] == "title"
        assert result["results"][0]["snippet"] is None

    asyncio.run(scenario())


# ------------------------------------------------------------------- scope & permission

def test_session_id_scope_pins_results_to_one_session(tmp_path):
    """§4.8: session_id pins both scan faces; unknown session -> NOT_FOUND."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="databricks runbook")
        _mk_message(log, "s1", "m1", "databricks cluster notes", seq=1)
        _mk_session(log, "s2", updated_at=2)
        _mk_message(log, "s2", "m2", "databricks pricing table", seq=1)
        scoped = await _search(log, "databricks", session_id="s1")
        assert scoped["total"] == 1 and _ids(scoped) == ["s1"]
        scoped2 = await _search(log, "pricing", session_id="s2")
        assert scoped2["total"] == 1
        err = _err(await _call(log, "session/search",
                               {"query": "databricks", "session_id": "nope"}))
        assert err.code == "NOT_FOUND"
        empty = await _search(log, "pricing", session_id="s1")  # scoped, no hit there
        assert empty["total"] == 0

    asyncio.run(scenario())


def test_read_only_connection_can_search(tmp_path):
    """§4.8: a read verb — read-only connections search like they list."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="readonly friendly")
        result = await _call(log, "session/search", {"query": "readonly"},
                             conn=ReadOnlyConn())
        assert _ok(result)["total"] == 1

    asyncio.run(scenario())


# ------------------------------------------------------------------- scope of content

def test_superseded_messages_are_not_searched(tmp_path):
    """§4.1: a row replaced by edit_resubmit (RFC-0006 §6.3) is edit history."""
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", updated_at=1)
        _mk_message(log, "s1", "m1", "unique zeppelin first draft", seq=1)
        assert (await _search(log, "zeppelin"))["total"] == 1
        log.store.append("s1", [], [], message_updates=[("m1", "m2")])
        assert (await _search(log, "zeppelin"))["total"] == 0
        _mk_message(log, "s1", "m2", "zeppelin second draft", seq=2)
        result = await _search(log, "zeppelin")
        assert result["total"] == 1 and result["results"][0]["message_id"] == "m2"

    asyncio.run(scenario())


def test_deleted_session_disappears_from_results(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        _mk_session(log, "s1", title="deletable candidate")
        assert (await _search(log, "deletable"))["total"] == 1
        log.store.delete_session("s1")
        assert (await _search(log, "deletable"))["total"] == 0

    asyncio.run(scenario())


def test_no_fts_infrastructure_is_required(tmp_path):
    """§4.2: the degrade path is 'nowhere to break' — no FTS tables, reopen works."""
    async def scenario():
        path = tmp_path / "sessions.db"
        log = SessionLog(SQLiteStore(path), replay=ReplayBuffer(maxlen=500))
        _mk_session(log, "s1", title="plain like scan")
        master = log.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%fts%'").fetchall()
        assert master == []  # no shadow tables were ever created
        assert (await _search(log, "plain like"))["total"] == 1

        reopened = SessionLog(SQLiteStore(path), replay=ReplayBuffer(maxlen=500))
        result = await _search(reopened, "plain like")
        assert result["total"] == 1 and result["results"][0]["session_id"] == "s1"

    asyncio.run(scenario())


# ------------------------------------------------------------------- contract echo

def test_schema_and_helpers_agree_on_the_contract():
    """A1: rpc.json declares session/search at 1.5.0 with the same surface."""
    data = json.loads(SCHEMA_RPC.read_text(encoding="utf-8"))
    assert data["version"] == "1.8.0"  # 0.0.3 model/discover owns the C-05 head
    entry = data["directions"]["ui_to_core"]["session/search"]
    assert set(entry["params"]) == {"query", "limit", "offset", "session_id"}
    assert set(entry["result"]) == {"results", "total"}
    assert set(entry["result"]["results"][0]) == HIT_KEYS
    assert set(entry["errors"]) == {"INVALID_ARGUMENT", "NOT_FOUND"}
    # the constants are echoed in the schema strings, not just in code
    assert "1..200" in entry["params"]["query"]
    assert "1..100" in entry["params"]["limit"]
    assert "120 code points" in entry["result"]["results"][0]["snippet"]
    blocks = [b for b in data["x-vade"]["appends"] if "RFC-0014" in b["doc"]]
    assert len(blocks) == 1
    assert blocks[0]["adds"] == ["session/search"]
    assert "none" in blocks[0]["rewrites"] and blocks[0]["version"] == "1.5.0"
    # helper probes (mirrors the contract gate)
    assert escape_like("%") == "\\%" and escape_like("_") == "\\_"
    assert escape_like("\\") == "\\\\" and escape_like("'") == "'"
    assert find_ascii_ci("TODO LIST", "todo") == 0
    assert find_ascii_ci("中文TODAY", "today") == 2
    snippet = make_snippet("a" * 300 + "NEEDLE" + "b" * 300, 300)
    assert len(snippet) <= SNIPPET_MAX and "NEEDLE" in snippet
