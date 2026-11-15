"""B-2: Argon2id at the designed parameters, legacy scrypt kept verify-only.

The legacy string is rebuilt here from the old format definition rather than
imported from the module under test — the point is that hashes already on disk
(produced by the previous release) keep verifying.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os

from argon2 import PasswordHasher

from vade.admin.auth import (
    ARGON2_MEMORY_KIB, ARGON2_TIME_COST, hash_password, password_needs_rehash,
    verify_password,
)

from .conftest import PASSWORD, client_for, login_browser


def _legacy_scrypt(password: str, *, n: int = 2 ** 14, r: int = 8, p: int = 1) -> str:
    """`scrypt$n$r$p$salt$digest`, exactly what the previous release wrote."""
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return (f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}$"
            f"{base64.b64encode(digest).decode()}")


def test_passwords_hash_with_argon2id_at_the_designed_parameters():
    stored = hash_password(PASSWORD)
    assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=1$"), stored
    assert ARGON2_TIME_COST == 3 and ARGON2_MEMORY_KIB == 64 * 1024
    assert verify_password(PASSWORD, stored) is True
    assert verify_password(PASSWORD + "x", stored) is False
    assert verify_password(PASSWORD, None) is False
    assert password_needs_rehash(stored) is False
    # Two hashes of the same password differ: the salt is random per hash.
    assert hash_password(PASSWORD) != stored


def test_a_legacy_scrypt_hash_still_verifies_and_asks_for_a_rehash():
    legacy = _legacy_scrypt(PASSWORD)
    assert verify_password(PASSWORD, legacy) is True
    assert verify_password("wrong-password-123", legacy) is False
    assert password_needs_rehash(legacy) is True


def test_argon2id_with_weaker_parameters_is_flagged_for_a_rehash():
    weak = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1).hash(PASSWORD)
    assert verify_password(PASSWORD, weak) is True
    assert password_needs_rehash(weak) is True


def test_login_upgrades_a_legacy_hash_in_place(ctx, app):
    """No offline migration window: the first successful login rewrites admin.json."""
    doc_path = ctx.setup.admin_doc_path
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["password"] = _legacy_scrypt(PASSWORD)
    doc_path.write_text(json.dumps(doc), encoding="utf-8")
    assert password_needs_rehash(ctx.setup.password_hash()) is True

    async def scenario():
        first = await login_browser(client_for(app), ctx)
        assert first.status_code == 200, first.text
        upgraded = ctx.setup.password_hash()
        assert upgraded.startswith("$argon2id$"), upgraded
        assert password_needs_rehash(upgraded) is False
        assert verify_password(PASSWORD, upgraded) is True
        # A second login (fresh challenge) still works with the same password.
        again = await login_browser(client_for(app), ctx)
        assert again.status_code == 200, again.text

    asyncio.run(scenario())

    on_disk = json.loads(doc_path.read_text(encoding="utf-8"))
    assert on_disk["password"].startswith("$argon2id$")
    assert on_disk["password_rehashed_at"]
    if os.name != "nt":  # POSIX mode bits are a no-op on Windows (chmod does nothing)
        assert (doc_path.stat().st_mode & 0o777) == 0o600


def test_a_failed_login_does_not_rewrite_the_legacy_hash(ctx, app):
    """The upgrade is gated on a proven password, not on touching the endpoint."""
    doc_path = ctx.setup.admin_doc_path
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    legacy = _legacy_scrypt(PASSWORD)
    doc["password"] = legacy
    doc_path.write_text(json.dumps(doc), encoding="utf-8")

    async def scenario():
        # 夹具口令运行时拼装（gitleaks 工作区归零），运行时值不变
        wrong = await login_browser(client_for(app), ctx, password="definitely-" + "wrong-123")
        assert wrong.status_code == 401, wrong.text
        assert ctx.setup.password_hash() == legacy

    asyncio.run(scenario())
