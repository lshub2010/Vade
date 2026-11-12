#!/usr/bin/env python3
"""Build an installable wheel for the generated UniFFI Python binding.

No maturin / setuptools-rust: the binding is one .py file plus one cdylib, which
a wheel can hold directly. Doing it by hand keeps the spike honest about what
actually ships and avoids pulling a build backend into the verification path.

  python3 build_wheel.py --lib ../protocol-spike/target/release/libprotocol_spike.so \
                         --module protocol_spike.py --out dist

Layout inside the wheel (site-packages root, matching what the generated loader
expects: `libprotocol_spike.so` next to `protocol_spike.py`).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import platform
import shutil
import tempfile
import zipfile
from pathlib import Path

NAME = "protocol_spike"
VERSION = "0.1.0"


def platform_tag() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "linux_x86_64"
    if machine in ("aarch64", "arm64"):
        return "linux_aarch64"
    raise SystemExit(f"unsupported platform for this spike: {machine}")


def record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", required=True, help="path to libprotocol_spike.so")
    ap.add_argument("--module", required=True, help="path to protocol_spike.py")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--version", default=VERSION)
    args = ap.parse_args()

    lib = Path(args.lib).resolve()
    module = Path(args.module).resolve()
    out = Path(args.out).resolve()
    for p in (lib, module):
        if not p.is_file():
            raise SystemExit(f"missing input: {p}")

    tag = platform_tag()
    wheel_name = f"{NAME}-{args.version}-py3-none-{tag}.whl"
    out.mkdir(parents=True, exist_ok=True)
    wheel_path = out / wheel_name

    dist_info = f"{NAME}-{args.version}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\n"
        f"Name: {NAME.replace('_', '-')}\n"
        f"Version: {args.version}\n"
        "Summary: Vade C0a spike - UniFFI Python binding over the Noise IK core\n"
        "Requires-Python: >=3.9\n"
        "Classifier: Programming Language :: Python :: 3\n"
    )
    wheel_meta = (
        "Wheel-Version: 1.0\n"
        "Generator: vade-c0a-build_wheel.py (hand-rolled)\n"
        "Root-Is-Purelib: false\n"
        f"Tag: py3-none-{tag}\n"
    )

    entries: list[tuple[str, bytes]] = [
        (f"{NAME}.py", module.read_bytes()),
        (f"lib{NAME}.so", lib.read_bytes()),
        (f"{dist_info}/METADATA", metadata.encode()),
        (f"{dist_info}/WHEEL", wheel_meta.encode()),
        (f"{dist_info}/top_level.txt", (NAME + "\n").encode()),
    ]

    record_lines = []
    for arcname, data in entries:
        record_lines.append(f"{arcname},{record_hash(data)},{len(data)}")
    record_lines.append(f"{dist_info}/RECORD,,")
    record = ("\n".join(record_lines) + "\n").encode()
    entries.append((f"{dist_info}/RECORD", record))

    tmp = Path(tempfile.mkdtemp(prefix="c0a-wheel-"))
    try:
        for arcname, data in entries:
            dest = tmp / arcname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        if wheel_path.exists():
            wheel_path.unlink()
        with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, _ in entries:
                zf.write(tmp / arcname, arcname)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    size = wheel_path.stat().st_size
    print(f"WHEEL={wheel_path}")
    print(f"WHEEL_BYTES={size}")
    print(f"WHEEL_TAG=py3-none-{tag}")


if __name__ == "__main__":
    main()
