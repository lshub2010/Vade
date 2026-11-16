"""TLS material for the data plane.

Self-signed generation needs `cryptography` (allowed here: certificate
generation is explicitly excluded from the "no crypto in Python" rule).
When the package is absent we do NOT silently fall back to plaintext — the
caller gets a clear UNSUPPORTED and must supply `[tls].cert` / `[tls].key`.

Two different values live here, and mixing them is a real bug (D78):

* `cert_fingerprint_hex()` — SHA-256 of the whole certificate DER. Audit and
  backwards compatibility only; it changes on every re-issue.
* `spki_fingerprint_hex()` — SHA-256 of the DER `SubjectPublicKeyInfo`,
  truncated to 16 hex chars. **This is the `tls=SPKI[:16]` pin** shown by
  `--print-tls-fingerprint` / the setup page and stored by the App. It survives
  re-issue of a certificate that keeps the same key pair, which is why the pin
  uses it instead of the certificate DER.
"""

from __future__ import annotations

import hashlib
import ssl
from pathlib import Path

from ..errors import VadeError

__all__ = ["cert_fingerprint_hex", "spki_fingerprint_hex", "ensure_certificates"]


def cert_fingerprint_hex(cert_path: Path | str) -> str:
    """SHA-256 of the certificate DER (full hex). NOT the SPKI pin."""
    pem = Path(cert_path).read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def spki_fingerprint_hex(cert_path: Path | str) -> str:
    """`SHA-256(SPKI)[:16]`, lowercase hex — the TLS pin (`tls=` field, D78).

    SPKI is `SubjectPublicKeyInfo`: the public key plus algorithm identifier,
    without the certificate wrapper (serial, validity, signature). Re-issuing a
    certificate for the same key keeps this value; the certificate DER does not.

    Needs `cryptography` (Python has no SPKI parser in the stdlib). When the
    package is missing this raises `UNSUPPORTED` — it must NOT quietly degrade
    to the certificate DER, because that value would never match the pin the
    App was given (a silent-rotation bug, not a fallback).
    """
    try:
        from cryptography import x509  # type: ignore
        from cryptography.hazmat.primitives import serialization  # type: ignore
    except ImportError:
        raise VadeError(
            "UNSUPPORTED",
            "SPKI fingerprint needs the `cryptography` package; "
            "install it (the certificate DER is a different value and not a fallback)",
        ) from None

    certificate = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    spki = certificate.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(spki).hexdigest()[:16]


def ensure_certificates(cert: str | None, key: str | None, data_dir: Path | str, san: list[str] | None = None) -> tuple[str, str]:
    if cert and key:
        for path in (cert, key):
            if not Path(path).exists():
                raise VadeError("INTERNAL", f"configured TLS file {path} does not exist")
        return cert, key

    try:
        from cryptography import x509  # type: ignore  # noqa: F401
        from cryptography.hazmat.primitives import hashes, serialization  # type: ignore  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import ec  # type: ignore  # noqa: F401
        from cryptography.x509.oid import NameOID  # type: ignore  # noqa: F401
    except ImportError:
        raise VadeError(
            "UNSUPPORTED",
            "self-signed TLS generation needs the `cryptography` package; "
            "install it or set [tls].cert and [tls].key",
        ) from None

    return _generate(cert, key, data_dir, san or [])


def _generate(cert: str | None, key: str | None, data_dir: Path | str, san: list[str]) -> tuple[str, str]:  # pragma: no cover
    import datetime as _dt
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory = Path(data_dir) / "tls"
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = Path(cert) if cert else directory / "server.crt"
    key_path = Path(key) if key else directory / "server.key"

    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vade-core")])
    alt: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    for name in san:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt.append(x509.DNSName(name))
    now = _dt.datetime.now(_dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return str(cert_path), str(key_path)
