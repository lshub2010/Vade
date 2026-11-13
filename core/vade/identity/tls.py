"""TLS material for the data plane.

Self-signed generation needs `cryptography` (allowed here: certificate
generation is explicitly excluded from the "no crypto in Python" rule).
When the package is absent we do NOT silently fall back to plaintext — the
caller gets a clear UNSUPPORTED and must supply `[tls].cert` / `[tls].key`.

`fingerprint` is SHA-256 of the DER certificate. The SPKI pin required by
`--print-tls-fingerprint` is a different value; see RESULT.md (unverified).
"""

from __future__ import annotations

import hashlib
import ssl
from pathlib import Path

from ..errors import VadeError

__all__ = ["cert_fingerprint_hex", "ensure_certificates"]


def cert_fingerprint_hex(cert_path: Path | str) -> str:
    """SHA-256 of the certificate DER, lowercase hex, first 16 chars for display."""
    pem = Path(cert_path).read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


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
