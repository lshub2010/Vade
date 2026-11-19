"""A12 — the admin `tokens_used` counter must stay an EVOLVE budget.

RFC-0005 makes every chat turn write an `llm.call` audit row with
`purpose="chat"`. `config_usage` feeds `[evolve].daily_token_budget`, so before
this test the chat rows would have been counted against self-evolution: a busy
conversation day would silently switch the evolve trigger off. The second half
is the reader's 500-row clamp: a day with more than 500 chat rows must not hide
the evolve rows that sit behind them.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from vade.admin.app import create_admin_app
from vade.audit.reader import AuditReader, AuditReadError

from .conftest import client_for, login_with_device, make_context, new_device


def _row(*, ts: float, purpose: str, tokens: int) -> dict:
    return {
        "kind": "llm.call",
        "ts": ts,
        "session_id": "s-1",
        "turn_id": "t-1",
        "extra": {"purpose": purpose, "total_tokens": tokens, "tokens": tokens},
    }


async def _usage(ctx) -> dict:
    device, key = new_device(ctx)
    app_client = client_for(create_admin_app(ctx))
    login = await login_with_device(app_client, ctx, device, key)
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    response = await app_client.get("/admin/api/config/usage",
                                    headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()


def test_chat_rows_never_count_against_the_evolve_budget(tmp_path):
    ctx = make_context(tmp_path)
    now = time.time()
    ctx.audit.record(_row(ts=now, purpose="chat", tokens=900))
    ctx.audit.record(_row(ts=now + 0.001, purpose="evolve", tokens=7))
    ctx.audit.record(_row(ts=now + 0.002, purpose="chat", tokens=1_000))
    ctx.audit.flush()

    body = asyncio.run(_usage(ctx))
    assert body["tokens_used"] == 7, f"chat tokens leaked into the evolve budget: {body}"
    ctx.audit.close()


def test_the_evolve_total_survives_more_than_one_reader_page(tmp_path):
    """The oldest rows are on the last page (`ts DESC`); they must still be read."""
    ctx = make_context(tmp_path)
    # Pin the rows to the START of the current UTC day: `time.time() - 700`
    # lands on yesterday when the suite runs in the first ~12 minutes after
    # midnight, and "today" is exactly what this endpoint counts.
    now = time.time() - (time.time() % 86_400) + 1.0
    # Oldest first: the evolve row is the one the 500-row clamp would drop.
    ctx.audit.record(_row(ts=now, purpose="evolve", tokens=11))
    for index in range(600):
        ctx.audit.record(_row(ts=now + 1 + index * 0.001, purpose="chat", tokens=1))
    ctx.audit.flush()

    body = asyncio.run(_usage(ctx))
    assert body["tokens_used"] == 11, (
        f"the evolve row was hidden behind a page of chat rows: {body}"
    )
    ctx.audit.close()


# --------------------------------------------------------------------- P0-2
# `GET /admin/api/config/usage` used to page the whole retention window on the
# event loop: 200k rows / 10 day tables = 267 pages × N tables = 2670 SELECTs,
# 26.9 s of a frozen loop, reachable from a read-only browser session.
def _seed_days(ctx, *, today_rows: int = 200, days: int = 8, per_day: int = 25) -> float:
    """`today_rows` rows today (mostly chat) + `per_day` rows on each older day."""
    # Day-boundary safe: 01:00 UTC today, never a `now - N` that can slip into
    # yesterday while the suite runs just after midnight.
    now = time.time() - (time.time() % 86_400) + 3_600.0
    ctx.audit.record(_row(ts=now - 0.5, purpose="evolve", tokens=13))
    for index in range(today_rows - 1):
        ctx.audit.record(_row(ts=now - 1 - index * 0.001, purpose="chat", tokens=5))
    for days_ago in range(1, days + 1):
        base = now - days_ago * 86_400
        for index in range(per_day):
            ctx.audit.record(_row(ts=base + index, purpose="evolve", tokens=1000))
    ctx.audit.flush()
    return now


def test_usage_only_queries_todays_table(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    now = _seed_days(ctx)
    today = time.strftime("%Y%m%d", time.gmtime(now))

    seen: list[str] = []
    original = AuditReader._tables

    def spy(self, db, day_keys=None):
        tables = original(self, db, day_keys)
        seen.extend(tables)
        return tables

    monkeypatch.setattr(AuditReader, "_tables", spy)

    body = asyncio.run(_usage(ctx))
    assert body["tokens_used"] == 13, body
    assert seen, "the usage query did not read any audit table"
    assert set(seen) == {f"audit_{today}"}, (
        f"usage fanned out over the retention window instead of today: {sorted(set(seen))}"
    )
    ctx.audit.close()


def test_usage_query_does_not_block_the_event_loop(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    ctx.audit.record(_row(ts=time.time(), purpose="evolve", tokens=7))
    ctx.audit.flush()

    delay = 0.4  # stand-in for the 26.9 s scan on 200k rows

    def slow(self, **kwargs):
        time.sleep(delay)
        return 7

    monkeypatch.setattr(AuditReader, "sum_extra_int", slow)

    async def scenario() -> tuple[dict, float, list[float]]:
        device, key = new_device(ctx)
        client = client_for(create_admin_app(ctx))
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]

        gaps: list[float] = []
        stop = asyncio.Event()

        async def ticker() -> None:
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        started = time.monotonic()
        response = await client.get("/admin/api/config/usage",
                                    headers={"Authorization": f"Bearer {token}"})
        elapsed = time.monotonic() - started
        stop.set()
        await task
        assert response.status_code == 200, response.text
        return response.json(), elapsed, gaps

    body, elapsed, gaps = asyncio.run(scenario())
    assert body["tokens_used"] == 7, body
    assert elapsed >= delay * 0.9, f"the slow query never ran ({elapsed:.3f}s)"
    assert len(gaps) >= 20, f"the loop only ticked {len(gaps)} times: the query blocked it"
    assert max(gaps) < delay / 2, (
        f"event loop was blocked for {max(gaps) * 1000:.0f} ms during the usage query"
    )
    ctx.audit.close()


def test_usage_is_cached_for_a_minute(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    ctx.audit.record(_row(ts=time.time(), purpose="evolve", tokens=3))
    ctx.audit.flush()

    calls: list[str] = []
    original = AuditReader.sum_extra_int

    def counting(self, **kwargs):
        calls.append(kwargs["day"])
        return original(self, **kwargs)

    monkeypatch.setattr(AuditReader, "sum_extra_int", counting)

    async def scenario():
        device, key = new_device(ctx)
        client = client_for(create_admin_app(ctx))
        token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        first = (await client.get("/admin/api/config/usage", headers=headers)).json()
        # A row recorded after the first read must NOT change the answer yet.
        ctx.audit.record(_row(ts=time.time(), purpose="evolve", tokens=100))
        ctx.audit.flush()
        second = (await client.get("/admin/api/config/usage", headers=headers)).json()
        return first, second

    first, second = asyncio.run(scenario())
    assert first["tokens_used"] == 3, first
    assert second["tokens_used"] == 3, f"the 60 s cache was bypassed: {second}"
    assert len(calls) == 1, f"the aggregate ran {len(calls)} times for 2 requests"
    ctx.audit.close()


def test_usage_is_correct_without_json1(tmp_path, monkeypatch):
    """Older SQLite builds fall back to one scan + a Python sum, same answer."""
    import vade.audit.reader as reader_module

    ctx = make_context(tmp_path)
    now = _seed_days(ctx, today_rows=40, days=2, per_day=5)
    monkeypatch.setattr(reader_module, "_JSON_OK", False)
    body = asyncio.run(_usage(ctx))
    assert body["tokens_used"] == 13, body
    assert body["day"] == time.strftime("%Y-%m-%d", time.gmtime(now))
    ctx.audit.close()


# --------------------------------------------------------------------- P2-1
# `sum_extra_int` turned **any** `sqlite3.OperationalError` into `0`. One row
# whose `extra` is not JSON (an older build, a hand-edited database) therefore
# zeroed the whole day, and a locked or corrupt database was indistinguishable
# from "no calls yet" — so the budget could never fire and nothing said why.
def _today_start(offset: float = 60.0) -> float:
    """01:00-ish UTC today: never slips into yesterday mid-suite."""
    return time.time() - (time.time() % 86_400) + offset


def _break_today_table(path, day_start: float) -> str:
    """Create `audit_<today>` with the right name and the wrong schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y%m%d", time.gmtime(day_start))
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f'CREATE TABLE "audit_{day}" (id INTEGER PRIMARY KEY, ts REAL, kind TEXT)'
        )
        connection.commit()
    finally:
        connection.close()
    return day


def test_a_malformed_extra_row_does_not_zero_the_day(tmp_path):
    ctx = make_context(tmp_path)
    now = _today_start()
    ctx.audit.record(_row(ts=now, purpose="evolve", tokens=11))
    ctx.audit.flush()

    # Bypass the writer, which always emits valid JSON: this is the row an older
    # build could have left behind.
    table = f"audit_{time.strftime('%Y%m%d', time.gmtime(now))}"
    connection = sqlite3.connect(ctx.audit.path)
    try:
        connection.execute(
            f'INSERT INTO "{table}" (ts, kind, extra) VALUES (?, ?, ?)',
            (now + 1, "llm.call", "{not json"),
        )
        connection.commit()
    finally:
        connection.close()

    body = asyncio.run(_usage(ctx))
    assert body["tokens_used"] == 11, (
        f"one non-JSON row zeroed the day's usage: {body}"
    )
    assert "tokens_unavailable" not in body
    ctx.audit.close()


def test_a_read_failure_is_an_error_not_a_zero(tmp_path):
    """A real sqlite failure must not look like an empty day."""
    path = tmp_path / "audit.db"
    day = _break_today_table(path, _today_start())

    with pytest.raises(AuditReadError, match="no such column"):
        AuditReader(path).sum_extra_int(
            day=day, kind="llm.call", field="tokens", exclude={"purpose": "chat"},
        )


def test_a_missing_day_table_is_no_data_not_an_error(tmp_path):
    path = tmp_path / "audit.db"
    sqlite3.connect(path).close()  # exists, but has no day tables at all
    assert AuditReader(path).sum_extra_int(day="20200101") == 0
    assert AuditReader(tmp_path / "nothing.db").sum_extra_int(day="20200101") == 0


def test_the_json1_fallback_also_raises_instead_of_zeroing(tmp_path, monkeypatch):
    import vade.audit.reader as reader_module

    monkeypatch.setattr(reader_module, "_JSON_OK", False)
    path = tmp_path / "audit.db"
    day = _break_today_table(path, _today_start())
    with pytest.raises(AuditReadError):
        AuditReader(path).sum_extra_int(day=day, kind="llm.call", field="tokens")


def test_usage_says_unavailable_when_the_audit_read_fails(tmp_path):
    """The endpoint carries the marker: a 0 alone is what hid the failure."""
    ctx = make_context(tmp_path)
    _break_today_table(ctx.audit.path, _today_start())

    body = asyncio.run(_usage(ctx))
    assert body["tokens_used"] == 0
    assert body["tokens_unavailable"] is True, (
        f"a failed audit read was served as an ordinary empty day: {body}"
    )
    ctx.audit.close()
