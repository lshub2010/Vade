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
import time

from vade.admin.app import create_admin_app

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
    now = time.time() - 700
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
