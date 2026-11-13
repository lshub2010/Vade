"""B-1: a browser session may not write, and never receives a token.

Three layers, because the first two are endpoint-specific and the third has to
catch the route somebody adds next week:

* `POST /admin/api/logout-all` — destructive (kicks every enrolled device);
* `POST /admin/api/password` — returns tokens in the body if it is allowed;
* every write route in the router, enumerated from the app itself, must answer
  403 `READ_ONLY_SESSION` for a browser cookie.
"""

from __future__ import annotations

import asyncio
import re

from vade.admin.app import PREAUTH_PREFIXES, READONLY_EXEMPT, readonly_exempt
from vade.audit.reader import AuditReader

from .conftest import PASSWORD, bearer, client_for, login_browser, login_with_device, new_device

#: 31-C5a §T4 ⑤, verbatim (`setup*` is the PREAUTH prefix, tested separately).
DESIGN_EXEMPT = frozenset({
    "/admin/api/login", "/admin/api/login/challenge", "/admin/api/logout",
    "/admin/api/token/refresh", "/admin/api/health",
})

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _write_routes(app):
    """(path, methods) for every non-exempt write route under /admin."""
    for route in app.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or not path.startswith("/admin"):
            continue
        writes = {m.upper() for m in methods} & _WRITE_METHODS
        if not writes or readonly_exempt(path):
            continue
        yield path, sorted(writes)


def test_the_browser_exemption_is_exactly_the_design_list():
    """Widening browser writes must require editing this assertion, not a stray path."""
    assert READONLY_EXEMPT == DESIGN_EXEMPT
    assert PREAUTH_PREFIXES == ("/admin/setup", "/admin/bind")
    # The two paths that used to escape (B-1) are explicitly not exempt.
    assert not readonly_exempt("/admin/api/logout-all")
    assert not readonly_exempt("/admin/api/password")


def test_a_browser_session_cannot_revoke_every_session(ctx, app):
    async def scenario():
        device, key = new_device(ctx)
        device_client = client_for(app)
        token = (await login_with_device(device_client, ctx, device, key)).json()["access_token"]

        browser = client_for(app)
        logged = await login_browser(browser, ctx)
        assert logged.json()["mode"] == "browser"

        response = await browser.post("/admin/api/logout-all",
                                      headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "READ_ONLY_SESSION"

        # The refusal is real: the device session was not revoked on the way out.
        still_valid = await device_client.get("/admin/api/nodes", headers=bearer(token))
        assert still_valid.status_code == 200, still_valid.text

        rows = AuditReader(ctx.audit.path).query(kind="admin.rejected")["items"]
        assert any(row["code"] == "READ_ONLY_SESSION" for row in rows), rows

    asyncio.run(scenario())


def test_a_browser_session_cannot_change_the_password_and_gets_no_tokens(ctx, app):
    async def scenario():
        browser = client_for(app)
        await login_browser(browser, ctx)
        before = ctx.setup.password_hash()

        response = await browser.post(
            "/admin/api/password",
            json={"old_password": PASSWORD, "new_password": "a-different-good-passphrase"},
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "READ_ONLY_SESSION"
        # No credential may appear in a browser response, not even the refusal.
        assert "access_token" not in response.text and "refresh_token" not in response.text

        # The password did not change, and the old one still logs in.
        assert ctx.setup.password_hash() == before
        again = await login_browser(client_for(app), ctx)
        assert again.status_code == 200, again.text

    asyncio.run(scenario())


def test_every_write_route_refuses_a_browser_session(ctx, app):
    """Enumeration fallback: a new write route is covered the day it is added."""

    async def scenario():
        browser = client_for(app)
        logged = await login_browser(browser, ctx)
        assert logged.json()["mode"] == "browser"

        routes = list(_write_routes(app))
        paths = {path for path, _ in routes}
        assert len(routes) >= 12, f"enumeration is suspiciously narrow: {routes}"
        # Pin B-1's two paths so the test cannot pass by being narrower than the app.
        assert "/admin/api/logout-all" in paths and "/admin/api/password" in paths

        for path, methods in routes:
            concrete = _PATH_PARAM.sub("probe", path)
            for method in methods:
                response = await browser.request(
                    method, concrete, json={}, headers={"Sec-Fetch-Site": "same-origin"},
                )
                assert response.status_code == 403, (method, path, response.status_code, response.text)
                assert response.json()["code"] == "READ_ONLY_SESSION", (method, path, response.text)
                assert "access_token" not in response.text, (method, path)

    asyncio.run(scenario())
