#!/usr/bin/env python3
"""Offline batch migration of `data/admin/admin.json` from scrypt to Argon2id.

The admin plane already upgrades a credential on the next successful login
(`SetupService.rehash_password`), so a deployment that keeps logging in has no
legacy hash left. This tool covers the other case: an estate of stopped cores
whose `admin.json` still holds `scrypt$n$r$p$salt$digest`, where the online
path would only fix each instance the first time somebody logs in — and where
"some instances migrated, some not" is the state that makes `password_needs_rehash`
useless as a health signal.

Non-obvious rules, all load-bearing:

* **Argon2id cannot be derived from a scrypt digest.** The plaintext has to be
  supplied (`--credentials` or `--password-stdin`); a target without one is
  reported and skipped, never guessed at.
* **A plaintext that does not verify against the stored hash is never written
  back.** A typo in the credentials file would otherwise destroy a credential
  that works today, and scrypt digests cannot be recovered from.
* **The replacement hash is verified against the same plaintext before the
  write**, so every value this tool writes is a value that logs in.
* **The swap is compare-and-swap**: `SetupService.replace_password_hash` only
  writes when `admin.json` still holds the hash that was read. A login that
  re-hashed in between wins, and this target is reported as skipped.
* **A held `data/core.lock` stops the write** (POSIX): a running core has its
  own view of `admin.json` and would undo the migration on the next re-hash.
* **Parameters travel with the hash**: `password` stays a PHC string, and
  `password_params` records `{alg, version, m, t, p, hash_len, salt_len}` read
  back out of the string that was actually written.
* **Exit codes**: `0` everything is (now) Argon2id at today's parameters,
  `1` fatal (bad path, no `admin.json` anywhere, write failure), `2` unknown or
  corrupt hash formats (listed), `3` incomplete (missing plaintext, plaintext
  mismatch, lost the compare-and-swap race, or the core is still running).
  Precedence: 1 > 2 > 3.

Usage::

    python tools/migrate_password_hashes.py data-a data-b data-c --dry-run
    python tools/migrate_password_hashes.py data-a data-b --credentials creds.json
    python tools/migrate_password_hashes.py data-a --password-stdin < /dev/stdin

`creds.json` is either `[{"path": "<data dir or admin.json>", "password": "…"}]`
or `{"<data dir or admin.json>": "…"}`.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "core") not in sys.path:
    sys.path.insert(0, str(ROOT / "core"))

from argon2 import Type, extract_parameters  # noqa: E402
from argon2.exceptions import InvalidHashError  # noqa: E402

from vade.admin.auth import (  # noqa: E402
    ARGON2_MEMORY_KIB, ARGON2_TIME_COST, hash_password, password_needs_rehash,
    verify_password,
)
from vade.admin.setup import SetupService  # noqa: E402
from vade.audit.writer import AuditWriter  # noqa: E402

__all__ = [
    "main", "build_parser", "classify", "HashFormatError", "Classification",
    "EXIT_OK", "EXIT_FATAL", "EXIT_BAD_HASH", "EXIT_INCOMPLETE",
]

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_BAD_HASH = 2
EXIT_INCOMPLETE = 3

#: `SetupService` only reads/writes `admin.json` on this path; neither value is
#: consulted by `replace_password_hash`.
_PLACEHOLDER = "unused-by-migration"

_ARGON2_NAMES = {Type.ID: "argon2id", Type.I: "argon2i", Type.D: "argon2d"}

#: Legacy scrypt bounds. Not taste: `hashlib.scrypt` with a hostile `n` is an
#: out-of-memory kill, and a digest outside these ranges cannot be a hash this
#: project ever wrote.
_SCRYPT_MAX_N = 2 ** 20
_SCRYPT_MAX_R = 32
_SCRYPT_MAX_P = 16


class HashFormatError(ValueError):
    """Stored material is neither a well-formed scrypt hash nor an Argon2 PHC string."""


@dataclass(slots=True)
class Classification:
    algo: str
    params: dict[str, Any]
    needs_rehash: bool


@dataclass(slots=True)
class Report:
    target: Path
    admin_json: Path
    status: str                      # see STATUSES
    algo: str | None = None          # algorithm found on disk
    params: dict[str, Any] | None = None       # its cost parameters
    new_params: dict[str, Any] | None = None   # parameters written (migrated only)
    detail: str = ""


STATUSES = ("migrated", "planned", "already-current", "skipped", "bad-format", "not-initialized")


# ----------------------------------------------------------------- classification
def _b64(text: str, what: str) -> bytes:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise HashFormatError(f"{what} is not valid base64") from None


def _classify_scrypt(stored: str) -> Classification:
    parts = stored.split("$")
    if len(parts) != 6:
        raise HashFormatError("scrypt hash must have 6 `$`-separated fields")
    _, n_s, r_s, p_s, salt_b64, digest_b64 = parts
    try:
        n, r, p = int(n_s), int(r_s), int(p_s)
    except ValueError:
        raise HashFormatError("scrypt cost parameters are not integers") from None
    if n < 2 or n > _SCRYPT_MAX_N or n & (n - 1):
        raise HashFormatError("scrypt n must be a power of two in [2, 2**20]")
    if not (1 <= r <= _SCRYPT_MAX_R) or not (1 <= p <= _SCRYPT_MAX_P):
        raise HashFormatError("scrypt r/p are outside the accepted range")
    salt = _b64(salt_b64, "scrypt salt")
    digest = _b64(digest_b64, "scrypt digest")
    if not 8 <= len(salt) <= 64:
        raise HashFormatError("scrypt salt length is implausible")
    if not 16 <= len(digest) <= 64:
        raise HashFormatError("scrypt digest length is implausible")
    return Classification(
        algo="scrypt",
        params={"alg": "scrypt", "n": n, "r": r, "p": p,
                "hash_len": len(digest), "salt_len": len(salt)},
        needs_rehash=True,
    )


def _argon2_params(phc: str) -> dict[str, Any]:
    parsed = extract_parameters(phc)
    return {
        "alg": _ARGON2_NAMES.get(parsed.type, "argon2?"),
        "version": parsed.version,
        "m": parsed.memory_cost,
        "t": parsed.time_cost,
        "p": parsed.parallelism,
        "hash_len": parsed.hash_len,
        "salt_len": parsed.salt_len,
    }


def classify(stored: Any) -> Classification:
    """Name the format and whether it still meets today's parameters.

    Raises `HashFormatError` for anything this project did not write — the
    caller's job is to report those, never to overwrite them.
    """
    if not isinstance(stored, str) or not stored:
        raise HashFormatError("password field is missing or empty")
    if stored.startswith("scrypt$"):
        return _classify_scrypt(stored)
    if stored.startswith("$argon2"):
        try:
            params = _argon2_params(stored)
        except (InvalidHashError, ValueError) as exc:
            raise HashFormatError(f"Argon2 PHC string does not parse ({type(exc).__name__})") from None
        # `password_needs_rehash` also covers a weaker `m`/`t`/`p` and argon2i/d.
        return Classification(algo=params["alg"], params=params,
                              needs_rehash=password_needs_rehash(stored))
    raise HashFormatError("unknown hash format (neither scrypt$… nor $argon2…)")


# ------------------------------------------------------------------------ targets
def _data_dir_for(path: Path) -> Path:
    """Accept either a data directory or the `admin/admin.json` inside it."""
    return path.parent.parent if path.name == "admin.json" else path


def resolve_targets(paths: Iterable[str]) -> list[Path]:
    resolved: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        candidate = Path(raw).expanduser()
        if not candidate.exists():
            raise FileNotFoundError(f"target does not exist: {raw}")
        data_dir = _data_dir_for(candidate).resolve()
        if data_dir not in seen:
            seen.add(data_dir)
            resolved.append(data_dir)
    return resolved


def load_credentials(path: Path) -> dict[Path, str]:
    """Map resolved `data dir` and `admin.json` paths to plaintexts."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read credentials file {path}: {type(exc).__name__}") from None
    if isinstance(data, list):
        entries = [(str(item.get("path") or ""), item.get("password")) for item in data if isinstance(item, dict)]
    elif isinstance(data, dict):
        entries = [(key, value) for key, value in data.items()]
    else:
        raise ValueError("credentials file must be a JSON array or object")
    mapping: dict[Path, str] = {}
    for raw_path, password in entries:
        if not raw_path:
            raise ValueError("credentials entry without a path")
        if not isinstance(password, str) or not password:
            raise ValueError(f"credentials entry for {raw_path} has an empty password")
        key = Path(raw_path).expanduser().resolve()
        mapping[key] = password
    return mapping


def _lookup(creds: Mapping[Path, str], data_dir: Path, admin_json: Path) -> str | None:
    return creds.get(admin_json.resolve()) or creds.get(data_dir.resolve())


def _audit(data_dir: Path, **event: Any) -> str | None:
    writer = AuditWriter(data_dir / "audit" / "audit.db", sync=True)
    try:
        return writer.record(event)
    finally:
        writer.close()


def _core_lock_held(data_dir: Path) -> bool:
    """`flock data/core.lock` is C2's single-instance guard; honour it here too.

    A running core keeps its own `admin.json` in memory and re-reads it on the
    next login, so writing under it can be undone by a re-hash the operator
    never sees. Windows has no advisory `flock`; there the check degrades to
    "assume stopped" and the operator's own discipline is the guard.
    """
    lock_path = data_dir / "core.lock"
    if not lock_path.exists():
        return False
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return False
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        handle.close()
    return False


# ---------------------------------------------------------------------- migration
def _params_text(params: Mapping[str, Any] | None) -> str:
    if not params:
        return ""
    if params.get("alg") == "scrypt":
        return f"n={params['n']},r={params['r']},p={params['p']}"
    return f"m={params['m']},t={params['t']},p={params['p']}"


def migrate_target(data_dir: Path, *, dry_run: bool, creds: Mapping[Path, str]) -> Report:
    admin_json = data_dir / "admin" / "admin.json"
    report = Report(target=data_dir, admin_json=admin_json, status="not-initialized")
    if not admin_json.exists():
        report.detail = "no admin/admin.json (never initialised)"
        return report

    service = SetupService(data_dir, _PLACEHOLDER, _PLACEHOLDER)
    doc = service.load_admin()
    if not isinstance(doc, dict):
        report.status = "bad-format"
        report.detail = "admin.json is not readable JSON"
        return report

    stored = doc.get("password")
    try:
        current = classify(stored)
    except HashFormatError as exc:
        report.status = "bad-format"
        report.detail = str(exc)
        return report

    report.algo, report.params = current.algo, current.params
    if not current.needs_rehash:
        report.status = "already-current"
        return report

    password = _lookup(creds, data_dir, admin_json)
    if password is None:
        report.status = "planned" if dry_run else "skipped"
        report.detail = "no plaintext supplied; pass --credentials for a full check" if dry_run \
            else "no plaintext supplied (hash left untouched)"
        return report
    # Never trust a credentials file over the hash it claims to open.
    if not verify_password(password, stored):
        report.status = "skipped"
        report.detail = "supplied plaintext does not verify against the stored hash (left untouched)"
        return report
    if dry_run:
        report.status = "planned"
        report.detail = "plaintext verified"
        return report
    if _core_lock_held(data_dir):
        report.status = "skipped"
        report.detail = "data/core.lock is held: stop the core before migrating"
        return report

    new_hash = hash_password(password)
    if not verify_password(password, new_hash):
        # Unreachable unless argon2-cffi is broken; refuse to write it anyway.
        report.status = "skipped"
        report.detail = "internal error: the new hash does not verify, nothing written"
        return report
    params = _argon2_params(new_hash)
    if not service.replace_password_hash(
        new_hash, expected_current=stored, params=params, rehashed_from=current.algo,
    ):
        report.status = "skipped"
        report.detail = "admin.json changed since it was read (concurrent rehash); nothing written"
        return report

    _audit(
        data_dir, kind="admin.password_rehashed", status="ok", actor="cli",
        extra={
            "to": "argon2id", "from": current.algo, "source": "offline-migration",
            "path": str(admin_json), "m": params["m"], "t": params["t"],
            "p": params["p"], "version": params["version"],
        },
    )
    report.status = "migrated"
    report.new_params = params
    report.detail = "old password still verifies against the new hash"
    return report


# ------------------------------------------------------------------------- output
def _exit_code(reports: Sequence[Report]) -> int:
    if any(r.status == "bad-format" for r in reports):
        return EXIT_BAD_HASH
    if any(r.status == "skipped" for r in reports):
        return EXIT_INCOMPLETE
    return EXIT_OK


def _render(reports: Sequence[Report], *, dry_run: bool) -> str:
    counts = {status: 0 for status in STATUSES}
    for report in reports:
        counts[report.status] += 1
    target_params = f"m={ARGON2_MEMORY_KIB},t={ARGON2_TIME_COST},p=1"
    lines: list[str] = []
    if dry_run:
        lines.append("dry run: nothing is written")
    lines.append(
        f"targets: {len(reports)}  " + "  ".join(
            f"{status}: {counts[status]}" for status in STATUSES if counts[status]
        )
    )
    for report in reports:
        line = f"  {report.status:<15} {report.target}"
        if report.algo and report.status in {"migrated", "planned"}:
            line += (f"  {report.algo} ({_params_text(report.params)}) → "
                     f"argon2id ({_params_text(report.new_params) or target_params})")
        elif report.algo:
            line += f"  {report.algo} ({_params_text(report.params)})"
        if report.detail:
            line += f"  — {report.detail}"
        lines.append(line)
    broken = [r for r in reports if r.status == "bad-format"]
    if broken:
        lines.append("unknown or corrupt hash formats (left untouched):")
        for report in broken:
            lines.append(f"  {report.admin_json}: {report.detail}")
    return "\n".join(lines)


def _as_json(reports: Sequence[Report], *, dry_run: bool, code: int) -> str:
    return json.dumps({
        "dry_run": dry_run,
        "exit_code": code,
        "targets": [
            {
                "target": str(r.target),
                "admin_json": str(r.admin_json),
                "status": r.status,
                "algo": r.algo,
                "params": r.params,
                "new_params": r.new_params,
                "detail": r.detail,
            }
            for r in reports
        ],
        "summary": {status: sum(1 for r in reports if r.status == status) for status in STATUSES},
    }, ensure_ascii=False, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_password_hashes",
        description="Replace legacy scrypt admin password hashes with Argon2id (core stopped).",
    )
    parser.add_argument("paths", nargs="+", metavar="DATA_DIR",
                        help="core data directory (or its admin/admin.json); repeatable")
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    parser.add_argument("--credentials", default=None, metavar="FILE",
                        help="JSON array [{path, password}] or object {path: password}")
    parser.add_argument("--password-stdin", action="store_true",
                        help="read one plaintext from stdin (exactly one target, no echo)")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        targets = resolve_targets(args.paths)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FATAL

    creds: dict[Path, str] = {}
    if args.credentials:
        try:
            creds = load_credentials(Path(args.credentials))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_FATAL
    if args.password_stdin:
        if len(targets) != 1:
            print("--password-stdin needs exactly one target", file=sys.stderr)
            return EXIT_FATAL
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            print("no password on stdin", file=sys.stderr)
            return EXIT_FATAL
        creds[targets[0]] = password

    reports = [migrate_target(target, dry_run=args.dry_run, creds=creds) for target in targets]

    if all(report.status == "not-initialized" for report in reports):
        # A typo'd path must not look like "nothing to do".
        print("no admin/admin.json under any target; check the paths", file=sys.stderr)
        return EXIT_FATAL

    code = _exit_code(reports)
    if args.json:
        print(_as_json(reports, dry_run=args.dry_run, code=code))
    else:
        print(_render(reports, dry_run=args.dry_run))
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
