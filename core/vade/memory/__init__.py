"""`vade.memory` component: the core-owned `MemoryPort` adapter.

Installs `app.memory_port` (and the `app.memory` alias) so two consumers work
without importing anything from the memory module:

* C3's agent loop reads the session layer through `MemoryPort.read_session`
  (wired in `session/__init__.py::_LazyMemory`);
* C5a's admin plane forwards `/admin/api/memory*` through the `admin_*` methods
  (`AdminContext.memory`).

The adapter resolves the live memory node lazily on every call, so install order
and a module that connects after startup are both fine.
"""

from __future__ import annotations

from typing import Any

from .adapter import DEFAULT_TIMEOUT_MS, READ_TIMEOUT_MS, MemoryAdapter

__all__ = ["install", "MemoryAdapter", "DEFAULT_TIMEOUT_MS", "READ_TIMEOUT_MS"]


def install(app: Any) -> Any:
    adapter = MemoryAdapter(app)
    app.memory_port = adapter
    # The shorter name is the "optional app memory slot" the loop/admin read;
    # keep both pointing at one object so an injected fake is never shadowed.
    app.memory = adapter
    return app
