from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core"
PROTOCOL_PY = ROOT / "protocol" / "core" / "python"

for entry in (str(CORE), str(PROTOCOL_PY)):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def pytest_configure(config):
    config.addinivalue_line("markers", "rust: requires libvade_protocol.so")
