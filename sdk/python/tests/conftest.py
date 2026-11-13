"""Test path bootstrap: the SDK is used from the source tree, not an install."""

from __future__ import annotations

import sys
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SDK_ROOT.parents[1]

for entry in (str(SDK_ROOT), str(REPO_ROOT / "core"), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
