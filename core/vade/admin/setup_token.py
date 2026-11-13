"""`data/setup.token` lifecycle — one owner, one file, one format.

Format is `acs_` + base64url(32 random bytes) = 47 chars. Not a uuid, not
base32: the prefix makes it greppable in a log-scrubbing pass, and 256 bits is
why the rate limit is log hygiene rather than a security boundary.

Precedence implemented here (and tested): **file > env > generate**. The
task table in 31-C5a ② says env is consulted before the file, but the
acceptance row "同时存在文件与 VADE_SETUP_TOKEN 且值不同 → 文件生效" is
explicit, and a file wins is also what makes "env pinned ⇒ never rotates"
true across restarts.
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
from pathlib import Path
from typing import Mapping

__all__ = [
    "PREFIX", "TOKEN_LEN", "generate", "ensure_setup_token", "read_token",
    "compare", "consume", "rotate", "mask", "token_path",
]

PREFIX = "acs_"
TOKEN_LEN = 47


def token_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "setup.token"


def generate() -> str:
    return PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _write(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("ascii"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def read_token(data_dir: Path | str) -> str | None:
    path = token_path(data_dir)
    try:
        value = path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, OSError):
        return None
    return value or None


def ensure_setup_token(
    data_dir: Path | str,
    *,
    env: Mapping[str, str] | None = None,
    initialized: bool = False,
) -> str | None:
    """Run on **every** start, not only the first one.

    * initialized → any leftover file is unlinked (a stale token is a key to a
      door that no longer exists, but leaving it invites confusion);
    * file exists → keep it (never rotate a value the user may have pinned);
    * else env → persist it, so the next restart keeps the same value;
    * else → generate a fresh one (D59: restart means rotate).
    """
    path = token_path(data_dir)
    if initialized:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None
    current = read_token(data_dir)
    if current:
        return current
    env_value = (env if env is not None else os.environ).get("VADE_SETUP_TOKEN")
    if env_value:
        # Risk note: /proc/<pid>/environ, `docker inspect` and some orchestrator
        # event logs will show this. The file is the recommended path; the env
        # var is an explicit "I know what I am doing" override.
        _write(path, env_value)
        return env_value
    token = generate()
    _write(path, token)
    return token


def rotate(data_dir: Path | str) -> str:
    token = generate()
    _write(token_path(data_dir), token)
    return token


def consume(data_dir: Path | str) -> None:
    try:
        token_path(data_dir).unlink()
    except FileNotFoundError:
        pass


def compare(expected: str | None, provided: str | None) -> bool:
    """Constant-time, length-safe. No normalisation: tokens are copy-pasted."""
    if not expected or provided is None:
        return False
    expected_b = expected.encode("utf-8")
    provided_b = provided.encode("utf-8")
    if len(expected_b) != len(provided_b):
        # Still do one comparison so the timing does not reveal the length.
        hmac.compare_digest(expected_b, expected_b)
        return False
    return hmac.compare_digest(expected_b, provided_b)


def mask(token: str) -> str:
    """First 8 chars only — logs must never carry the whole value."""
    return token[:8] + "…"
