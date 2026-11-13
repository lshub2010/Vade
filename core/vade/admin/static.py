"""Serve the built admin UI (`admin-ui/dist`) when it exists.

The API never returns HTML; this mount is the only HTML source, and it is a
separate origin from the data plane by construction (port 8444 vs 8443).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["mount_static"]


def mount_static(app: Any, dist: Path | str) -> bool:
    directory = Path(dist)
    if not directory.is_dir():
        return False
    from starlette.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=directory / "assets"), name="assets")
    return True
