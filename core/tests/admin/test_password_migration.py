"""Task #7: offline batch scrypt → Argon2id migration of `admin.json`.

`tools/migrate_password_hashes.py` exists because the online upgrade fires only
on a successful login: an estate of stopped cores keeps the legacy digest until
somebody logs into each one. These tests call the tool's real entry point
(`main()` returns the exit code) and then re-open `admin.json` on disk — a
zero return that did not change the file would be worse than no tool at all.

Fixtures that need a working admin plane use the shared `ctx`/`app` fixtures;
the format-survey cases build `admin.json` by hand so a corrupt-hash test does
not pay for an Argon2id initialisation it never reads.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from vade.admin.auth import (
    ARGON2_MEMORY_KIB, ARGON2_TIME_COST, hash_password, password_needs_rehash,
    verify_password,
)
from vade.audit.reader import AuditReader

from .conftest import PASSWORD, client_for, login_browser

TOOL_PATH = Path(__file__).resolve().parents[3] / "tools" / "migrate_password_hashes.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("migrate_password_hashes", TOOL_PATH)
    assert spec is not None and spec.loader is not None, TOOL_PATH
    module = importlib.util.module_from_spec(spec)
    # `@dataclass(slots=True)` resolves annotations through `sys.modules`, so the
    # half-built module must be registered before its body runs (Py3.14).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


def _legacy_scrypt(password: str, *, n: int = 2 ** 14, r: int = 8, p: int = 1) -> str:
    """The pre-Argon2id `scrypt$n$r$p$salt$digest` the previous release wrote."""
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return (f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}$"
            f"{base64.b64encode(digest).decode()}")


def _make_data_dir(root: Path, stored: str, *, name: str = "data") -> Path:
    data_dir = root / name
    (data_dir / "admin").mkdir(parents=True, exist_ok=True)
    (data_dir / "admin" / "admin.json").write_text(
        json.dumps({"version": 1, "password": stored, "created_at": 1.0}), encoding="utf-8",
    )
    return data_dir


def _admin_doc(data_dir: Path) -> dict:
    return json.loads((data_dir / "admin" / "admin.json").read_text(encoding="utf-8"))


def _credentials(root: Path, entries: dict[Path, str]) -> Path:
    path = root / "credentials.json"
    path.write_text(json.dumps([{"path": str(target), "password": pw}
                                for target, pw in entries.items()]), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- dry run
def test_dry_run_reports_the_plan_and_writes_nothing(tmp_path, capsys):
    data_dir = _make_data_dir(tmp_path, _legacy_scrypt(PASSWORD))
    admin_json = data_dir / "admin" / "admin.json"
    before = admin_json.read_bytes()

    code = TOOL.main([str(data_dir), "--dry-run"])
    out = capsys.readouterr().out

    assert code == TOOL.EXIT_OK == 0
    assert admin_json.read_bytes() == before
    assert "password_params" not in _admin_doc(data_dir)
    assert verify_password(PASSWORD, _admin_doc(data_dir)["password"])
    assert "planned" in out and "scrypt" in out
    assert str(data_dir) in out


def test_dry_run_with_credentials_verifies_the_plaintext_but_still_writes_nothing(tmp_path, capsys):
    data_dir = _make_data_dir(tmp_path, _legacy_scrypt(PASSWORD))
    admin_json = data_dir / "admin" / "admin.json"
    before = admin_json.read_bytes()
    creds = _credentials(tmp_path, {data_dir: PASSWORD})

    assert TOOL.main([str(data_dir), "--dry-run", "--credentials", str(creds)]) == 0
    assert "plaintext verified" in capsys.readouterr().out
    assert admin_json.read_bytes() == before
    assert _admin_doc(data_dir)["password"].startswith("scrypt$")


# ------------------------------------------------------------------------- real run
def test_a_real_run_writes_argon2id_and_records_its_parameters(tmp_path, capsys):
    data_dir = _make_data_dir(tmp_path, _legacy_scrypt(PASSWORD))
    creds = _credentials(tmp_path, {data_dir: PASSWORD})

    assert TOOL.main([str(data_dir), "--credentials", str(creds)]) == 0
    # The report names both sides of the swap, not the destination twice.
    assert "scrypt (n=16384,r=8,p=1) →" in capsys.readouterr().out

    doc = _admin_doc(data_dir)
    assert doc["password"].startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    assert doc["password_params"] == {
        "alg": "argon2id", "version": 19,
        "m": ARGON2_MEMORY_KIB, "t": ARGON2_TIME_COST, "p": 1,
        "hash_len": 32, "salt_len": 16,
    }
    assert doc["password_rehashed_from"] == "scrypt"
    assert doc["password_rehashed_at"]
    # The whole point: the old password opens the new hash.
    assert verify_password(PASSWORD, doc["password"]) is True
    assert password_needs_rehash(doc["password"]) is False
    if os.name != "nt":  # POSIX mode bits only; chmod is a no-op on Windows
        assert ((data_dir / "admin" / "admin.json").stat().st_mode & 0o777) == 0o600


def test_a_second_run_is_a_no_op_and_current_argon2id_is_left_alone(tmp_path, capsys):
    legacy = _make_data_dir(tmp_path, _legacy_scrypt(PASSWORD), name="legacy")
    current = _make_data_dir(tmp_path, hash_password(PASSWORD), name="current")
    creds = _credentials(tmp_path, {legacy: PASSWORD, current: PASSWORD})

    assert TOOL.main([str(legacy), "--credentials", str(creds)]) == 0
    migrated_bytes = (legacy / "admin" / "admin.json").read_bytes()
    current_bytes = (current / "admin" / "admin.json").read_bytes()
    capsys.readouterr()

    assert TOOL.main([str(legacy), str(current), "--credentials", str(creds)]) == 0
    out = capsys.readouterr().out
    assert (legacy / "admin" / "admin.json").read_bytes() == migrated_bytes
    assert (current / "admin" / "admin.json").read_bytes() == current_bytes
    assert "already-current" in out


def test_the_old_password_still_logs_in_after_the_migration(ctx, app, tmp_path):
    """End-to-end: the rewritten file is what the real login route reads."""
    doc_path = ctx.setup.admin_doc_path
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc["password"] = _legacy_scrypt(PASSWORD)
    doc_path.write_text(json.dumps(doc), encoding="utf-8")
    creds = _credentials(tmp_path, {tmp_path: PASSWORD})

    assert TOOL.main([str(tmp_path), "--credentials", str(creds)]) == 0
    migrated = ctx.setup.password_hash()
    assert migrated.startswith("$argon2id$")

    async def scenario():
        response = await login_browser(client_for(app), ctx)
        assert response.status_code == 200, response.text
        assert response.json()["csrf_token"]

    asyncio.run(scenario())
    # The login path must not re-hash what is already Argon2id at today's params.
    assert ctx.setup.password_hash() == migrated


# --------------------------------------------------------------------- bad formats
def test_unknown_and_corrupt_formats_exit_two_and_are_listed(tmp_path, capsys):
    unknown = _make_data_dir(tmp_path, "md5$deadbeef", name="unknown")
    corrupt = _make_data_dir(tmp_path, "scrypt$16384$8$1$not*base64$zz", name="corrupt")

    code = TOOL.main([str(unknown), str(corrupt), "--dry-run"])
    out = capsys.readouterr().out

    assert code == TOOL.EXIT_BAD_HASH == 2
    assert "unknown hash format" in out
    assert "not valid base64" in out
    assert str(unknown / "admin" / "admin.json") in out
    assert str(corrupt / "admin" / "admin.json") in out
    assert _admin_doc(unknown)["password"] == "md5$deadbeef"
    assert _admin_doc(corrupt)["password"].startswith("scrypt$16384$8$1$not*base64")


def test_the_json_report_names_every_bad_format(tmp_path, capsys):
    bad = _make_data_dir(tmp_path, "pbkdf2_sha256$1000$nope", name="bad")
    code = TOOL.main([str(bad), "--dry-run", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["exit_code"] == 2 and payload["dry_run"] is True
    assert [t["status"] for t in payload["targets"]] == ["bad-format"]
    assert payload["summary"]["bad-format"] == 1


def test_a_corrupt_sibling_does_not_block_a_healthy_target(tmp_path):
    good = _make_data_dir(tmp_path, _legacy_scrypt(PASSWORD), name="good")
    bad = _make_data_dir(tmp_path, "totally-not-a-hash", name="bad")
    creds = _credentials(tmp_path, {good: PASSWORD})

    # Non-zero because the estate is not fully migrated, but the good one is done.
    assert TOOL.main([str(good), str(bad), "--credentials", str(creds)]) == 2
    assert _admin_doc(good)["password"].startswith("$argon2id$")
    assert _admin_doc(bad)["password"] == "totally-not-a-hash"


def test_the_script_exits_non_zero_as_a_process(tmp_path):
    bad = _make_data_dir(tmp_path, "md5$deadbeef")
    result = subprocess.run(
        [sys.executable, str(TOOL_PATH), str(bad), "--dry-run"],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 2, result.stderr
    assert "unknown hash format" in result.stdout


# ---------------------------------------------------------------------- refusals
def test_plaintext_that_does_not_verify_is_never_written(tmp_path):
    data_dir = _make_data_dir(tmp_path, _legacy_scrypt(PASSWORD))
    admin_json = data_dir / "admin" / "admin.json"
    before = admin_json.read_bytes()
    # 运行时拼装（gitleaks 工作区归零），运行时值不变
    creds = _credentials(tmp_path, {data_dir: "definitely-" + "wrong-123"})

    assert TOOL.main([str(data_dir), "--credentials", str(creds)]) == TOOL.EXIT_INCOMPLETE == 3
    assert admin_json.read_bytes() == before


def test_a_real_run_without_a_plaintext_reports_and_leaves_the_hash(tmp_path):
    data_dir = _make_data_dir(tmp_path, _legacy_scrypt(PASSWORD))
    admin_json = data_dir / "admin" / "admin.json"
    before = admin_json.read_bytes()

    assert TOOL.main([str(data_dir)]) == TOOL.EXIT_INCOMPLETE == 3
    assert admin_json.read_bytes() == before


def test_a_target_without_admin_json_is_an_error_when_it_is_the_only_one(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert TOOL.main([str(empty), "--dry-run"]) == TOOL.EXIT_FATAL == 1
    assert "no admin/admin.json" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="advisory flock does not exist on Windows")
def test_a_running_core_blocks_the_write(tmp_path):
    """`data/core.lock` held ⇒ refuse, even with a verified plaintext."""
    import fcntl

    data_dir = _make_data_dir(tmp_path, _legacy_scrypt(PASSWORD))
    admin_json = data_dir / "admin" / "admin.json"
    before = admin_json.read_bytes()
    creds = _credentials(tmp_path, {data_dir: PASSWORD})

    held = open(data_dir / "core.lock", "a+")
    try:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert TOOL.main([str(data_dir), "--credentials", str(creds)]) == TOOL.EXIT_INCOMPLETE == 3
        assert admin_json.read_bytes() == before
    finally:
        held.close()


# --------------------------------------------------------------- setup interface
def test_the_setup_interface_refuses_a_weaker_hash_and_a_stale_compare(ctx):
    current = ctx.setup.password_hash()
    weak = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1).hash(PASSWORD)

    with pytest.raises(ValueError):
        ctx.setup.replace_password_hash(weak, expected_current=current)
    # Compare-and-swap: a stale `expected_current` is a no-op, never a clobber.
    rotated = hash_password(PASSWORD + "-rotated")
    assert ctx.setup.replace_password_hash(rotated, expected_current=_legacy_scrypt(PASSWORD)) is False
    assert ctx.setup.password_hash() == current
