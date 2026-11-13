"""Agent loop package. No `install()` on purpose: `vade.session` owns wiring."""

from __future__ import annotations

from .context import BlobStore, CompactionConfig, ContextBuilder, estimate_tokens
from .engine import LoopConfig, LoopEngine, TurnOutcome
from .ports import (ApprovalDecision, ApprovalPort, ApprovalRequest, AuditPort,
                    EventSink, MemoryItem, MemoryPort, NullApprovalPort, NullAuditPort)
from .prompt import PromptParts, apply_hooks, assemble
from .tools import ToolEntry, ToolSnapshot, build_snapshot

__all__ = [
    "LoopEngine", "LoopConfig", "TurnOutcome", "PromptParts", "assemble", "apply_hooks",
    "ToolSnapshot", "ToolEntry", "build_snapshot", "CompactionConfig", "ContextBuilder",
    "BlobStore", "estimate_tokens", "ApprovalPort", "ApprovalRequest", "ApprovalDecision",
    "NullApprovalPort", "AuditPort", "NullAuditPort", "MemoryPort", "MemoryItem", "EventSink",
]
