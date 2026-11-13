"""The push side of the evolve channel: queues, backpressure, journal, replay.

Shape of one delivery (C-14):

    {"jsonrpc": "2.0", "method": "events/deliver",
     "params": {"topic": "session.turn", "session_id": ..., "seq_from": 41,
                "seq_to": 47, "payload": {...}}}

Decisions worth knowing before reading the code:

* **Pull, not hook.** C3's `emit()` broadcasts to UI sinks and C3d may not edit
  `session/`. So `pump()` reads the events C3 already persisted (`read_since`)
  and advances a per-session watermark. Nothing is written twice and no C3 code
  changed. `install()` also appends a prompt-hook tick so a live core does not
  depend on someone calling `pump()`.
* **Exactly once per turn.** A per-session set of delivered `turn_id`s plus the
  watermark means two pumps cannot emit the same turn twice.
* **Best-effort, resumable.** A slow instance gets its oldest queued envelope
  dropped (`delivery_dropped`), never a stalled session. A disconnected one
  loses the live push, but every `session.turn` is also kept in a bounded
  journal, so `events/subscribe(since_seq=N)` fills the gap without duplicates.
  `session.closed` is deliberately not replayable (one lost evolution beat is
  cheaper than delivery semantics on a best-effort channel).
* **Audit carries metadata only** (`module_id, instance_id, session_id, turns,
  bytes, ok`). The transcript never reaches a log line.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Iterable, Mapping

from ..errors import VadeError
from ..logging import get_logger
from . import transcript as tx
from .subscribe import EVOLVE_TOPICS, Subscription, SubscriptionTable

__all__ = ["DeliveryQueue", "EventBus", "DEFAULT_CAPACITY", "DEFAULT_TIMEOUT_S", "JOURNAL_SIZE"]

log = get_logger("vade.events")

DEFAULT_CAPACITY = 32          # C-14: per instance, drop-oldest
DEFAULT_TIMEOUT_S = 5.0        # C-14: a send that takes longer is a failure
JOURNAL_SIZE = 512             # per session, for since_seq replay
SEEN_TURNS = 2048              # per session; older ids cannot come back (seq only grows)
AUDIT_RECORDS = 256


@dataclass(slots=True)
class DeliveryQueue:
    """One bounded queue per instance: offer is sync, sending happens in a task."""

    instance_id: str
    connection_id: str
    sink: Any = None
    capacity: int = DEFAULT_CAPACITY
    timeout_s: float = DEFAULT_TIMEOUT_S
    delivered: int = 0
    dropped: int = 0
    failed: int = 0
    sent_bytes: int = 0
    _items: Deque[dict[str, Any]] = field(default_factory=deque)
    _wake: Any = None
    _worker: Any = None
    _audit: Any = None
    _closed: bool = False

    # ------------------------------------------------------------------ queue
    def offer(self, envelope: Mapping[str, Any]) -> bool:
        """Enqueue; when full, the OLDEST entry goes (a slow module never blocks)."""
        if self._closed:
            self.dropped += 1
            return False
        if len(self._items) >= self.capacity:
            self._items.popleft()
            self.dropped += 1
            self._emit_audit("events.delivery_dropped", envelope, ok=False, reason="queue full")
        self._items.append(dict(envelope))
        self._ensure_worker()
        if self._wake is not None:
            self._wake.set()
        return True

    def pending(self) -> int:
        return len(self._items)

    def close(self) -> None:
        self._closed = True
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()

    # ------------------------------------------------------------------ drain
    async def drain(self) -> int:
        """Send everything queued right now. Used by subscribe/close and tests."""
        sent = 0
        while self._items:
            envelope = self._items.popleft()
            if await self._send_one(envelope):
                sent += 1
        return sent

    async def _send_one(self, envelope: Mapping[str, Any]) -> bool:
        session_id = envelope.get("session_id")
        payload = envelope.get("payload") or {}
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        turns = 1 if envelope.get("topic") == "session.turn" else len(payload.get("turns") or ())
        if self.sink is None:
            self.failed += 1
            self._emit_audit("events.delivery_failed", envelope, ok=False, reason="no connection",
                             turns=turns, bytes=size)
            return False
        frame = {"jsonrpc": "2.0", "method": "events/deliver", "params": dict(envelope)}
        try:
            await asyncio.wait_for(self._send(frame), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            self.failed += 1
            self._emit_audit("events.delivery_failed", envelope, ok=False, reason="timeout",
                             turns=turns, bytes=size)
            return False
        except Exception as exc:  # no retry: reconnect + since_seq is the recovery path
            self.failed += 1
            self._emit_audit("events.delivery_failed", envelope, ok=False,
                             reason=type(exc).__name__, turns=turns, bytes=size)
            return False
        self.delivered += 1
        self.sent_bytes += size
        self._emit_audit("events.deliver", envelope, ok=True, turns=turns, bytes=size)
        return True

    async def _send(self, frame: Mapping[str, Any]) -> None:
        send_event = getattr(self.sink, "send_event", None)
        if callable(send_event):
            await send_event(dict(frame))
            return
        send_json = getattr(self.sink, "send_json", None)
        if callable(send_json):
            await send_json(dict(frame))
            return
        if callable(self.sink):
            await self.sink(dict(frame))
            return
        raise TypeError("connection has no send_json/send_event")

    # ----------------------------------------------------------------- worker
    def _ensure_worker(self) -> None:
        if self._worker is not None or self._closed:
            return
        try:
            asyncio.get_running_loop()
            self._wake = asyncio.Event()
            self._worker = asyncio.get_running_loop().create_task(self._run())
        except RuntimeError:  # no loop yet: drain() will push it later
            self._wake = None

    async def _run(self) -> None:
        assert self._wake is not None
        while not self._closed:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            while self._items and not self._closed:
                await self._send_one(self._items.popleft())

    def _emit_audit(self, event: str, envelope: Mapping[str, Any], *, ok: bool,
                    reason: str | None = None, turns: int = 0, bytes: int = 0) -> None:
        if self._audit is None:
            return
        self._audit(event, module_id=None, instance_id=self.instance_id,
                    session_id=envelope.get("session_id"), turns=turns, bytes=bytes,
                    ok=ok, reason=reason)


class EventBus:
    """`events/subscribe` + delivery. One per app; `install()` wires it."""

    def __init__(
        self,
        *,
        store: Any = None,
        app: Any = None,
        allow_transcript: bool = False,
        allowlist: Iterable[Any] | None = (),
        redact: Iterable[str] | None = (),
        registry: Any = None,
        connections: Any = None,
        clock: Any = time.time,
        capacity: int = DEFAULT_CAPACITY,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        journal_size: int = JOURNAL_SIZE,
    ) -> None:
        self._store = store
        self._app = app
        self._registry = registry
        self._connections = connections
        self._clock = clock
        self.capacity = int(capacity)
        self.timeout_s = float(timeout_s)
        self.table = SubscriptionTable(
            allow_transcript=allow_transcript, allowlist=allowlist, redact=redact, clock=clock
        )
        self._queues: dict[tuple[str, str], DeliveryQueue] = {}
        self._journal: dict[str, Deque[dict[str, Any]]] = {}
        self._journal_size = int(journal_size)
        self._watermarks: dict[str, int] = {}
        self._seen: dict[str, OrderedDict[str, None]] = {}
        self._closed_sessions: set[str] = set()
        #: Fallback only: `identify()` reads `record.manifest.subscriptions`
        #: first (C-03 append, RFC-0001). This map is for a registry that does
        #: not retain the manifest -- tests, or a future remote table.
        self._declared: dict[tuple[str, str], tuple[str, ...]] = {}
        self._audit_records: Deque[dict[str, Any]] = deque(maxlen=AUDIT_RECORDS)

    # ------------------------------------------------------------- providers
    @property
    def store(self) -> Any:
        return self._store if self._store is not None else getattr(self._app, "session_store", None)

    @property
    def registry(self) -> Any:
        return self._registry if self._registry is not None else getattr(self._app, "registry", None)

    def _connection_table(self) -> Mapping[str, Any]:
        provider = self._connections
        if provider is None:
            provider = getattr(self._app, "_connections", None)
        table = provider() if callable(provider) else provider
        return table or {}

    # ------------------------------------------------------------------ audit
    def _audit(self, event: str, **fields: Any) -> None:
        record = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
        self._audit_records.append(record)
        log.info(event, extra={"event": event, **record})

    def audit_records(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._audit_records]

    # -------------------------------------------------------------- identity
    @dataclass(frozen=True, slots=True)
    class Who:
        module_id: str
        instance_id: str
        fingerprint: str
        role: str
        declared: tuple[str, ...]
        connection_id: str

    def declare(self, *, module_id: str, instance_id: str, topics: Iterable[str]) -> None:
        """Record `manifest.subscriptions` for a registry that does not keep it.

        The normal path is passive: `manifest.subscriptions` now survives
        validation (C-03 append, RFC-0001) and `identify()` reads it off the
        record, so a plain `registry.register()` is enough. This stays as the
        explicit seam for a registry whose records are not local objects.
        """
        self._declared[(str(module_id), str(instance_id))] = tuple(str(t) for t in topics or ())

    def identify(self, connection: Any) -> "EventBus.Who":
        cid = str(getattr(connection, "connection_id", None) or getattr(connection, "id", None) or "")
        crypto = getattr(connection, "crypto", None)
        fingerprint = str(getattr(crypto, "remote_fingerprint", "") or "") if crypto is not None else ""
        record = None
        registry = self.registry
        if registry is not None and cid:
            try:
                for candidate in registry.snapshot():
                    if getattr(candidate, "connection_id", None) == cid:
                        record = candidate
                        break
            except Exception:  # a broken registry must not turn into a grant
                record = None
        module_id = getattr(record, "module_id", "") if record is not None else ""
        instance_id = getattr(record, "instance_id", "") if record is not None else ""
        role = getattr(record, "role", "") if record is not None else ""
        manifest = getattr(record, "manifest", None) if record is not None else None
        declared: tuple[str, ...] = ()
        for holder in (manifest, record):
            value = getattr(holder, "subscriptions", None) if holder is not None else None
            if value is not None:
                declared = tuple(str(t) for t in value)
                break
        if not declared and module_id and instance_id:
            declared = self._declared.get((module_id, instance_id), ())
        return self.Who(module_id=module_id, instance_id=instance_id, fingerprint=fingerprint,
                        role=role, declared=declared, connection_id=cid)

    # ------------------------------------------------------------- subscribe
    def handlers(self) -> dict[str, Any]:
        """RPC methods this bus publishes; C3 merges them into its handler table."""
        return {"events/subscribe": self.on_subscribe}

    async def on_subscribe(self, connection: Any, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """`events/subscribe`: accepted/rejected per topic; never closes the socket."""
        params = dict(params or {})
        topics = params.get("topics")
        if not isinstance(topics, (list, tuple)) or not topics:
            raise VadeError("INVALID_ARGUMENT", "topics must be a non-empty array of topic names")
        if any(not isinstance(t, str) for t in topics):
            raise VadeError("INVALID_ARGUMENT", "topics must contain strings only")
        since_seq = self._since_seq(params.get("since_seq"))
        fields, override = self.table.resolve_redact(params.get("redact"), present="redact" in params)
        who = self.identify(connection)
        accepted, rejected = self.table.evaluate(
            topics, declared=who.declared, role=who.role,
            module_id=who.module_id, fingerprint=who.fingerprint,
        )
        if override:
            self._audit("events.redact_override_denied", module_id=who.module_id or None,
                        instance_id=who.instance_id or None, ok=False)
        subscription = Subscription(
            module_id=who.module_id, instance_id=who.instance_id,
            connection_id=who.connection_id, role=who.role,
            topics=frozenset(accepted), since_seq=since_seq, redact=fields,
            subscribed_at=float(self._clock()),
        )
        self.table.put(subscription)
        replayed = self._replay(subscription) if since_seq is not None else 0
        queue = self._queue_for(subscription)
        if queue is not None:
            await queue.drain()
        self._audit("events.subscribe", module_id=who.module_id or None,
                    instance_id=who.instance_id or None, ok=bool(accepted),
                    accepted=len(accepted), rejected=len(rejected), replayed=replayed)
        return {"accepted": accepted, "rejected": rejected, "replayed": replayed}

    async def on_disconnect(self, connection: Any) -> None:
        """Drop the live subscription; the journal keeps the gap recoverable."""
        cid = str(getattr(connection, "connection_id", None) or getattr(connection, "id", None) or "")
        for key, queue in list(self._queues.items()):
            if queue.connection_id == cid:
                queue.close()
                self._queues.pop(key, None)
        self.table.drop(cid)

    @staticmethod
    def _since_seq(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VadeError("INVALID_ARGUMENT", "since_seq must be a non-negative integer or null")
        return value

    # --------------------------------------------------------------- delivery
    def _queue_for(self, sub: Subscription) -> DeliveryQueue | None:
        if not sub.connection_id:
            return None
        key = (sub.instance_id, sub.connection_id)
        queue = self._queues.get(key)
        if queue is None:
            sink = self._connection_table().get(sub.connection_id)
            queue = DeliveryQueue(
                instance_id=sub.instance_id, connection_id=sub.connection_id, sink=sink,
                capacity=self.capacity, timeout_s=self.timeout_s, _audit=self._audit_delivery,
            )
            self._queues[key] = queue
        elif queue.sink is None:
            queue.sink = self._connection_table().get(sub.connection_id)
        return queue

    def _audit_delivery(self, event: str, **fields: Any) -> None:
        self._audit(event, **{k: v for k, v in fields.items() if v is not None})

    def _envelope(self, topic: str, session_id: str | None, seq_from: int,
                  seq_to: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"topic": topic, "session_id": session_id, "seq_from": int(seq_from),
                "seq_to": int(seq_to), "payload": dict(payload)}

    def _dispatch(self, topic: str, session_id: str | None, seq_from: int, seq_to: int,
                  payload: Mapping[str, Any]) -> int:
        # Journal first, subscribers second: the journal is what makes a gap
        # recoverable, so it must fill even while nobody is connected.
        if topic in EVOLVE_TOPICS and session_id:
            journal = self._journal.setdefault(session_id, deque(maxlen=self._journal_size))
            journal.append(self._envelope(topic, session_id, seq_from, seq_to, payload))
        subscribers = self.table.subscribers(topic)
        if not subscribers:
            return 0
        sent = 0
        for sub in subscribers:
            body, _hit = tx.redact_payload(payload, sub.redact or self.table.redact)
            queue = self._queue_for(sub)
            if queue is None:
                continue
            queue.offer(self._envelope(topic, session_id, seq_from, seq_to, body))
            sent += 1
        return sent

    def _replay(self, sub: Subscription) -> int:
        """Fill `since_seq+1 ..` from the journal. Only `session.turn` is replayed."""
        if "session.turn" not in sub.topics or sub.since_seq is None:
            return 0
        queue = self._queue_for(sub)
        if queue is None:
            return 0
        sent = 0
        for session_id in sorted(self._journal):
            for entry in list(self._journal[session_id]):
                if entry["seq_to"] <= sub.since_seq:
                    continue
                body, _hit = tx.redact_payload(entry["payload"], sub.redact or self.table.redact)
                queue.offer(self._envelope("session.turn", session_id, entry["seq_from"],
                                           entry["seq_to"], body))
                sent += 1
        return sent

    # ------------------------------------------------------------------- pump
    def pump(self, session_id: str | None = None) -> int:
        """Deliver every finished-but-undelivered turn. Safe to call repeatedly."""
        store = self.store
        if store is None:
            return 0
        if session_id is not None:
            session_ids = [session_id]
        else:
            try:
                session_ids = [str(s.id) for s in store.list_sessions(limit=50)]
            except Exception:
                session_ids = sorted(self._watermarks)
        emitted = 0
        for sid in session_ids:
            emitted += self._pump_one(store, sid)
        return emitted

    def _pump_one(self, store: Any, session_id: str) -> int:
        watermark = self._watermarks.get(session_id, 0)
        try:
            events = list(store.read_since(session_id, watermark))
        except Exception as exc:
            self._audit("events.pump_failed", session_id=session_id, ok=False,
                        reason=type(exc).__name__)
            return 0
        if not events:
            return 0
        first_seq: dict[str, int] = {}
        for event in events:
            turn_id = str((getattr(event, "payload", {}) or {}).get("turn_id") or "")
            if turn_id and turn_id not in first_seq:
                first_seq[turn_id] = int(event.seq)
        emitted = 0
        for event in events:
            if not tx.is_terminal(str(event.type)):
                continue
            payload_in = getattr(event, "payload", {}) or {}
            turn_id = str(payload_in.get("turn_id") or "")
            if not turn_id:
                continue
            if self._seen_turn(session_id, turn_id):
                continue
            payload = tx.turn_payload(store, session_id, turn_id, state=payload_in.get("state"))
            if payload is None:
                # The turn row is not visible yet: stop here so this event is retried
                # instead of silently skipped (a turn that never arrives is a hole).
                self._watermarks[session_id] = max(watermark, int(event.seq) - 1)
                return emitted
            self._remember_turn(session_id, turn_id)
            emitted += self._dispatch("session.turn", session_id,
                                      first_seq.get(turn_id, int(event.seq)),
                                      int(event.seq), payload)
        self._watermarks[session_id] = int(events[-1].seq)
        return emitted

    def _seen_turn(self, session_id: str, turn_id: str) -> bool:
        seen = self._seen.get(session_id)
        return seen is not None and turn_id in seen

    def _remember_turn(self, session_id: str, turn_id: str) -> None:
        seen = self._seen.get(session_id)
        if seen is None:
            seen = OrderedDict()
            self._seen[session_id] = seen
        seen[turn_id] = None
        while len(seen) > SEEN_TURNS:
            seen.popitem(last=False)

    # ----------------------------------------------------------------- closed
    def emit_closed(self, closed: Any = None) -> int:
        """`session.closed`. Idempotent per session; accepts str / mapping / object."""
        session_id = self._session_id_of(closed)
        if not session_id or session_id in self._closed_sessions:
            return 0
        store = self.store
        if store is None:
            return 0
        # The last turn must be delivered before the closing beat, otherwise a
        # session whose final turn finished just now loses it.
        self.pump(session_id)
        payload = tx.closed_payload(store, session_id, closed_at=int(self._clock() * 1000))
        if payload is None:
            return 0
        self._closed_sessions.add(session_id)
        try:
            seq_to = int(store.last_seq(session_id))
        except Exception:
            seq_to = payload.get("turns_total", 0)
        return self._dispatch("session.closed", session_id, 1, max(0, seq_to), payload)

    @staticmethod
    def _session_id_of(closed: Any) -> str:
        if closed is None:
            return ""
        if isinstance(closed, str):
            return closed
        if isinstance(closed, Mapping):
            return str(closed.get("session_id") or "")
        return str(getattr(closed, "session_id", None) or getattr(closed, "id", None) or "")

    # ------------------------------------------------------------------ skill
    def emit_skill_proposed(self, *, name: str, origin: str, revision: int,
                            base_revision: int, status: str = "pending") -> int:
        """Metadata only -- the body must never ride an event (C-14)."""
        payload = {"name": str(name), "origin": str(origin), "revision": int(revision),
                   "base_revision": int(base_revision), "status": str(status)}
        return self._dispatch("skill.proposed", None, 0, 0, payload)

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        queues = list(self._queues.values())
        return {
            "subscribers": len(self.table),
            "delivered": sum(q.delivered for q in queues),
            "dropped": sum(q.dropped for q in queues),
            "failed": sum(q.failed for q in queues),
            "bytes": sum(q.sent_bytes for q in queues),
            "queued": sum(q.pending() for q in queues),
            "journal": {sid: len(entries) for sid, entries in sorted(self._journal.items())},
            "watermarks": dict(sorted(self._watermarks.items())),
            "redact_override_attempts": self.table.redact_override_attempts,
        }

    async def drain(self) -> int:
        total = 0
        for queue in list(self._queues.values()):
            total += await queue.drain()
        return total

    def close(self) -> None:
        for queue in list(self._queues.values()):
            queue.close()
        self._queues.clear()
