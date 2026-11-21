"""`python -m vade admin <cmd>` — local-only recovery and trust commands.

Eleven subcommands per 31-C5a §七之二. None of them make a remote call: they
read/write `data/` directly, which is why they are the only way back in when
every device is lost.

`proxy` is the one exception: it is a developer tool that needs a device private
key to sign with, and this build does not ship that client. It exits non-zero
with an explicit message instead of pretending.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from ..config import load_config
from ..errors import VadeError
from . import setup_token
from .auth import normalize_pubkey, validate_password, PasswordPolicyError, hash_password
from .devices import DeviceStore
from .identity import load_or_create
from ..audit.writer import AuditWriter

__all__ = ["main", "build_parser"]


def _data_dir(args: argparse.Namespace) -> Path:
    if getattr(args, "data_dir", None):
        return Path(args.data_dir)
    try:
        cfg = load_config()
        return Path(str(cfg.get("core.data_dir", "./data")))
    except Exception:
        return Path("./data")


def _audit(data_dir: Path, **event: Any) -> None:
    writer = AuditWriter(data_dir / "audit" / "audit.db", sync=True)
    try:
        writer.record(event)
    finally:
        writer.close()


def _require_not_running(data_dir: Path) -> None:
    """Refuse to touch `data/` while a core holds `data/core.lock`.

    Same lock, same truth as `__main__._serve`: a CLI write to `devices.json`
    while a core is running used to be erased by that core's next
    `mark_used()` write-back (review P0-5). `DeviceStore` also invalidates its
    cache on every stat, so a write that does land is at least not ignored —
    but the supported contract is "stop the core first".
    """
    from .locks import acquire_instance_lock, release_instance_lock

    try:
        fd = acquire_instance_lock(data_dir)
    except OSError:
        raise SystemExit("core appears to be running (data/core.lock is held); stop it first") from None
    release_instance_lock(fd)


def _resolve_data_dir(args) -> Path:  # pragma: no cover - thin alias
    return _data_dir(args)


def _common(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
    # Accept `--data-dir` both before and after the subcommand: C2's entry point
    # hands us the tokens after `admin`, so the common position is the second one.
    sp.add_argument("--data-dir", default=argparse.SUPPRESS,
                    help="core data directory (default: core.toml)")
    return sp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vade admin", description="Vade admin plane operations (local only)")
    parser.add_argument("--data-dir", default=None, help="core data directory (default: core.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        # `--data-dir` is accepted after the subcommand too: C2's entry point
        # forwards the tokens that follow `admin`, which is where users type it.
        return _common(sub.add_parser(name, help=help_text))

    add("print-fingerprint", "print the core identity fp8")
    add("print-setup-token", "print data/setup.token (only before initialisation)")
    add("rotate-setup-token", "replace data/setup.token")
    add("reset-password", "interactive password reset (TTY only)")
    add("reset", "wipe data/admin and return to setup mode")
    add("proxy", "local signing proxy (developer tool, not a user path)")

    tls = add("print-tls-fingerprint", "print the TLS SPKI fingerprint")
    tls.add_argument("--cert", required=True, help="PEM certificate path")

    # Rescue tools, not the user path: the App's QR is the normal way in.
    trust = add("trust-key", "add a device key (rescue; normal path is the App's QR)")
    trust.add_argument("--pubkey-file", required=True)
    trust.add_argument("--label", default="rescue")
    trust.add_argument("--alg", default=None, choices=[None, "p256", "ed25519"])

    revoke = add("revoke-device", "remove a device and end its sessions")
    revoke.add_argument("--id", required=True)

    ticket = add("issue-bind-ticket", "mint a one-off bind ticket (rescue)")
    ticket.add_argument("--ttl", default="10m")

    init = add("init", "write admin.json while the core is stopped")
    init.add_argument("--password-stdin", action="store_true")
    init.add_argument("--pubkey-file", default=None)

    return parser


def _parse_ttl(text: str) -> int:
    text = text.strip().lower()
    if text.endswith("m"):
        return int(float(text[:-1]) * 60)
    if text.endswith("h"):
        return int(float(text[:-1]) * 3600)
    return int(float(text))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    data_dir = _data_dir(args)
    command = args.command

    if command == "print-fingerprint":
        identity = load_or_create(data_dir)
        print(identity.fp8)
        return 0

    if command == "print-tls-fingerprint":
        import hashlib

        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_pem_x509_certificate(Path(args.cert).read_bytes())
        spki = cert.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        print(hashlib.sha256(spki).hexdigest()[:16])
        return 0

    if command == "print-setup-token":
        if (data_dir / "admin" / "admin.json").exists():
            raise SystemExit("already initialised; data/setup.token is not used")
        token = setup_token.read_token(data_dir)
        if not token:
            raise SystemExit("no data/setup.token; start the core once to generate one")
        print(token)
        return 0

    if command == "rotate-setup-token":
        _require_not_running(data_dir)
        if (data_dir / "admin" / "admin.json").exists():
            raise SystemExit("already initialised; setup tokens are not accepted")
        token = setup_token.rotate(data_dir)
        _audit(data_dir, kind="setup.token_rotated", status="ok", actor="cli")
        print(setup_token.mask(token))
        return 0

    if command == "trust-key":
        _require_not_running(data_dir)
        text = Path(args.pubkey_file).read_text(encoding="utf-8")
        alg, raw = normalize_pubkey(text, args.alg)
        store = DeviceStore(data_dir / "admin" / "devices.json")
        device = store.add(pubkey_b64=base64.b64encode(raw).decode(), alg=alg,
                           label=args.label, ip="cli", hardware_backed=False)
        _audit(data_dir, kind="device.added", status="ok", actor="cli",
               extra={"device_id": device.device_id, "alg": alg, "label": device.label})
        print(json.dumps(device.public(), ensure_ascii=False, indent=2))
        return 0

    if command == "revoke-device":
        _require_not_running(data_dir)
        store = DeviceStore(data_dir / "admin" / "devices.json")
        if not store.revoke(args.id):
            raise SystemExit("unknown device id")
        _audit(data_dir, kind="device.revoked", status="ok", actor="cli", extra={"device_id": args.id})
        print("revoked")
        return 0

    if command == "issue-bind-ticket":
        identity = load_or_create(data_dir)
        from .setup import SetupService

        ttl = _parse_ttl(args.ttl)
        service = SetupService(data_dir, "127.0.0.1:8444", identity.fp8)
        ticket = service.create_ticket(ttl)
        _audit(data_dir, kind="device.ticket", status="ok", actor="cli",
               extra={"expires_at": ticket["expires_at"], "rescue": True})
        print(ticket["bind_url"])
        print(f"expires_at: {ticket['expires_at']}")
        return 0

    if command == "init":
        _require_not_running(data_dir)
        if (data_dir / "admin" / "admin.json").exists():
            raise SystemExit("already initialised")
        if not args.password_stdin:
            raise SystemExit("use --password-stdin")
        password = sys.stdin.readline().rstrip("\n")
        try:
            validate_password(password)
        except PasswordPolicyError as exc:
            raise SystemExit(str(exc))
        from .setup import SetupService

        identity = load_or_create(data_dir)
        service = SetupService(data_dir, "127.0.0.1:8444", identity.fp8)
        pubkey = Path(args.pubkey_file).read_text(encoding="utf-8") if args.pubkey_file else None
        service.initialize(password=password, loopback=True, ip="cli", pubkey=pubkey,
                           device_store=DeviceStore(data_dir / "admin" / "devices.json"))
        _audit(data_dir, kind="setup.attempt", status="ok", actor="cli", extra={"source": "cli"})
        print("initialised")
        return 0

    if command == "reset-password":
        if not sys.stdin.isatty():
            raise SystemExit("reset-password needs a TTY")
        _require_not_running(data_dir)
        path = data_dir / "admin" / "admin.json"
        if not path.exists():
            raise SystemExit("not initialised")
        first = getpass.getpass("new password: ")
        second = getpass.getpass("repeat: ")
        if first != second:
            raise SystemExit("passwords do not match")
        try:
            validate_password(first)
        except PasswordPolicyError as exc:
            raise SystemExit(str(exc))
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["password"] = hash_password(first)
        doc["password_changed_at"] = time.time()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _audit(data_dir, kind="admin.password_changed", status="ok", actor="cli")
        print("password changed; every session is invalid")
        return 0

    if command == "reset":
        _require_not_running(data_dir)
        print("This wipes data/admin (password, devices, identity) and returns to setup mode.")
        if input("type RESET to continue: ") != "RESET":
            raise SystemExit("aborted")
        target = data_dir / "admin"
        if target.exists():
            shutil.move(str(target), f"{target}.bak-{int(time.time())}")
        setup_token.consume(data_dir)
        setup_token.ensure_setup_token(data_dir, initialized=False)
        _audit(data_dir, kind="admin.reset", status="ok", actor="cli")
        print("reset complete")
        return 0

    if command == "proxy":
        print("`admin proxy` is a developer tool that signs with a device key; this build does not ship "
              "that client. Use the Vade App or `X-AC-Client: cli` on loopback.", file=sys.stderr)
        return 3

    raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
