"""Static bundle mount: the release tree must serve the SPA, not only index.html."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from vade.admin.app import create_admin_app

ADMIN_HOST = "127.0.0.1:8444"


def test_login_and_assets_are_served_from_the_packaged_dist(ctx, tmp_path: Path) -> None:
    dist = tmp_path / "admin-ui" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (dist / "assets" / "index.js").write_text("console.log('vade')", encoding="utf-8")

    async def scenario() -> None:
        app = create_admin_app(ctx, static_dist=dist)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"http://{ADMIN_HOST}"
        ) as client:
            index = await client.get("/login")
            assert index.status_code == 200
            assert "text/html" in index.headers["content-type"]
            assert "id=root" in index.text

            asset = await client.get("/assets/index.js")
            assert asset.status_code == 200
            assert "console.log" in asset.text

    asyncio.run(scenario())
