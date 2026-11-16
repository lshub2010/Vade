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
  journal, so `events/subscribe(since_seq=...)` fills the gap without
  duplicates. `since_seq` is **per session** (`session_id` + int, or a
  `{session_id: seq}` map): sequence numbers live in per-session counters, so
  one watermark compared against every session both skips earlier events of the
  other sessions and mis-orders them. `session.closed` is deliberately not
  replayable (one lost evolution beat is cheaper than delivery semantics on a
  best-effort channel) and a journal entry never changes topic on the way out.
* **A turn row can land after its terminal event.** C3 appends the event first
  and writes the `turns` row right after, so `turn_payload() is None` is
  usually a race, not a hole. It is retried a bounded number of times; when the
  budget is spent the turn is audited (`events.turn_missing`) and the watermark
  moves past it. Never retry forever: one malformed event would otherwise pin
  the watermark and silently stop every later turn of that session.
* **A closed socket is gone immediately.** C3's transport has no app-level
  "socket closed" slot; the real callback is `Connection._on_close` (the sweep
  `CoreApp` installs on every socket). The bus chains onto it, so a disconnect
  drops that connection's subscription and queue instead of leaving a dead
  subscriber that keeps auditing drops forever. `install()` also publishes
  `connection_closed` on `app.on_disconnect` for a transport that grows such a
  slot, and hooks sockets already in the live table.
* **Every session, not the newest page.** `pump()` walks the store's light
  session rows to the end (archived included) instead of taking
  `list_sessions(limit=50)`: a turn in session number 51 used to be persisted
  and never delivered. Paging is metadata-only; the only full `load()` is the
  per-turn payload the channel already builds.
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
from .subscribe import (
    EVOLVE_TOPICS, Subscription, SubscriptionTable, validate_topics,
)

__all__ = ["DeliveryQueue", "EventBus", "DEFAULT_CAPACITY", "DEFAULT_TIMEOUT_S",
           "JOURNAL_SIZE", "SESSION_PAGE_SIZE"]

log = get_logger("vade.events")

DEFAULT_CAPACITY = 32          # C-14: per instance, drop-oldest
DEFAULT_TIMEOUT_S = 5.0        # C-14: a send that takes longer is a failure
JOURNAL_SIZE = 512             # per session, for since_seq replay
SEEN_TURNS = 2048              # per session; older ids cannot come back (seq only grows)
AUDIT_RECORDS = 256
#: Light session rows per page while `pump()` walks the store. Metadata only,
#: so a few hundred rows cost far less than one full `Session` load. Chosen so
#: a workspace of a few thousand sessions is a handful of small queries per
#: pump, not one query per session.
SESSION_PAGE_SIZE = 200

#: How many pumps a terminal event may wait for its `turns` row before the
#: event is declared broken (audited, watermark advanced). C3 writes the event
#: first and the row immediately after, so 2 misses are normal; 3 are not.
TURN_INVISIBLE_RETRIES = 3


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
            self._emit_audit("events.delivery_dropped", envelope, ok=False,
                             reason="queue closed")
            return False
        if len(self._items) >= self.capacity:
            # `oldest` is the envelope that actually leaves. Auditing the
            # incoming one points an operator at a turn that was delivered and
            # hides the one that was lost.
            oldest = self._items.popleft()
            self.dropped += 1
            self._emit_audit("events.delivery_dropped", oldest, ok=False, reason="queue full")
        self._items.append(dict(envelope))
        self._ensure_worker()
        if self._wake is not None:
            self._wake.set()
        return True

    def pending(self) -> int:
        return len(self._items)

    def close(self) -> None:
        """Stop the worker and release queued frames: a closed queue holds nothing."""
        self._closed = True
        self._items.clear()
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
        turn_retries: int = TURN_INVISIBLE_RETRIES,
    ) -> None:
        self._store = store
        self._app = app
        self._registry = registry
        self._connections = connections
        self._clock = clock
        self.capacity = int(capacity)
        self.timeout_s = float(timeout_s)
        self.turn_retries = max(1, int(turn_retries))
        self.table = SubscriptionTable(
            allow_transcript=allow_transcript, allowlist=allowlist, redact=redact, clock=clock
        )
        self._queues: dict[tuple[str, str], DeliveryQueue] = {}
        self._journal: dict[str, Deque[dict[str, Any]]] = {}
        self._journal_size = int(journal_size)
        self._watermarks: dict[str, int] = {}
        self._seen: dict[str, OrderedDict[str, None]] = {}
        #: (session_id, turn_id) -> how many pumps have missed the turn row.
        self._turn_attempts: dict[tuple[str, str], int] = {}
        #: (session_id, turn_id) -> seq of its first event, kept across retries:
        #: the retry pass reads from a watermark that already sits past
        #: `turn.queued`, so recomputing `seq_from` there would be wrong.
        self._turn_first_seq: dict[tuple[str, str], int] = {}
        self._closed_sessions: set[str] = set()
        #: connection_id -> the socket object whose `_on_close` we chained.
        #: Kept per bus (not an attribute on the socket): two buses in one
        #: process -- tests, a dev reload -- each need their own cleanup.
        self._hooked: dict[str, Any] = {}
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

    def _connection_table(self) -> Mapping[str, Any] | None:
        """The live socket table, or `None` when no provider exists.

        `None` and `{}` must stay distinguishable: an empty table means every
        connection is gone, while no table means "nothing to compare against"
        (a unit test constructing a bus without a transport). Cleanup must not
        treat the second as the first.
        """
        provider: Any = self._connections
        if provider is None:
            provider = getattr(self._app, "_connections", None)
        if provider is None:
            return None
        table: Any = provider() if callable(provider) else provider
        return table if table is not None else None

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
        cid = self._connection_id(connection)
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
        topics = validate_topics(params.get("topics"))
        session_id = params.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise VadeError("INVALID_ARGUMENT", "session_id must be a non-empty string")
        since_seq = self._since_seq(params.get("since_seq"), session_id)
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
        # A subscription is a queue with a lifetime; the socket's close hook is
        # the only reliable end-of-lifetime signal the transport exposes. Do it
        # after `put`, so a socket that is already closed drops this record
        # instead of the record resurrecting a dead queue.
        self.watch(connection)
        if self.table.get(who.connection_id) is subscription:
            replayed = self._replay(subscription) if since_seq is not None else 0
            queue = self._queue_for(subscription)
            if queue is not None:
                await queue.drain()
        else:
            replayed = 0
        self._audit("events.subscribe", module_id=who.module_id or None,
                    instance_id=who.instance_id or None, ok=bool(accepted),
                    accepted=len(accepted), rejected=len(rejected), replayed=replayed)
        return {"accepted": accepted, "rejected": rejected, "replayed": replayed}

    # ------------------------------------------------------------ disconnect
    @staticmethod
    def _connection_id(connection: Any) -> str:
        return str(getattr(connection, "connection_id", None)
                   or getattr(connection, "id", None) or "")

    def watch(self, connection: Any) -> bool:
        """Chain the bus onto the close hook the transport already owns.

        `CoreApp` passes `on_close=self._forget` to every `Connection`, and
        `Connection.close()` calls it exactly once, from the loop that owns the
        socket. That is the real disconnect seam; there is no app-level one.
        Without chaining it, a module that subscribed and then vanished keeps a
        `Subscription` and a `DeliveryQueue` alive: every later turn is offered
        and audited as dropped for a socket that is gone.

        Idempotent per bus; the transport's own callback always still runs
        (even if our cleanup raises), so governance's table sweep is untouched.
        A duck-typed sink without `_on_close` is skipped -- it has no lifetime
        to follow, and `on_disconnect` remains the explicit path for it.
        """
        cid = self._connection_id(connection)
        if not cid or self._hooked.get(cid) is connection:
            return False
        if getattr(connection, "_closed", False):
            # The socket closed before anyone subscribed (or between the RPC
            # and this hook): its `close()` will never fire again, so hooking it
            # would pin a subscription that can never be delivered to.
            self.drop_connection(cid, reason="connection already closed")
            return False
        if not hasattr(connection, "_on_close"):
            return False
        original = getattr(connection, "_on_close", None)
        bus = self

        def _closed(conn: Any) -> None:
            try:
                bus.connection_closed(conn)
            except Exception:  # teardown must never break the socket's close
                log.warning("event disconnect cleanup failed",
                            extra={"code": None, "connection_id": cid})
            if callable(original):
                original(conn)

        try:
            connection._on_close = _closed
        except Exception:  # frozen/slotted sink: keep the explicit path only
            return False
        self._hooked[cid] = connection
        return True

    def watch_connections(self, connections: Mapping[str, Any] | None = None) -> int:
        """Hook every live socket now; `install()` uses this for sockets already up."""
        table = connections if connections is not None else self._connection_table()
        hooked = 0
        for connection in list((table or {}).values()):
            cid = self._connection_id(connection)
            fresh = bool(cid) and self._hooked.get(cid) is not connection
            if self.watch(connection) and fresh:
                hooked += 1
        return hooked

    def connection_closed(self, connection: Any) -> int:
        """Sync teardown, safe to call from a transport close callback."""
        cid = self._connection_id(connection)
        self._hooked.pop(cid, None)
        return self.drop_connection(cid, reason="connection closed")

    def drop_connection(self, connection_id: str, *, reason: str = "disconnect") -> int:
        """Forget one socket: no live subscription, no queue, no queued frames.

        Idempotent -- a transport may report the close and an operator may also
        call `on_disconnect`. The journal is untouched: `since_seq` is how a
        reconnecting module recovers the gap.
        """
        if not connection_id:
            return 0
        instance = ""
        dropped = 0
        for key, queue in list(self._queues.items()):
            if queue.connection_id != connection_id:
                continue
            instance = instance or queue.instance_id
            queue.close()
            self._queues.pop(key, None)
            dropped += 1
        had_subscription = self.table.get(connection_id) is not None
        self.table.drop(connection_id)
        if dropped or had_subscription:
            self._audit("events.subscriber_gone", instance_id=instance or None,
                        ok=True, reason=reason)
        return dropped

    async def on_disconnect(self, connection: Any) -> None:
        """Drop the live subscription; the journal keeps the gap recoverable."""
        self.connection_closed(connection)

    @staticmethod
    def _seq_int(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VadeError("INVALID_ARGUMENT",
                            "since_seq values must be non-negative integers")
        return int(value)

    @classmethod
    def _since_seq(cls, value: Any, session_id: Any = None) -> dict[str, int] | None:
        """Normalise `since_seq` into `{session_id: seq}`.

        A seq belongs to one session's counter, so a bare integer only means
        something together with `session_id`; a single watermark applied to
        every journal compares unrelated counters and both skips other
        sessions' early events and replays them out of order. A mapping is the
        only shape that can carry a whole reconnect position.
        """
        if value is None:
            return None
        if isinstance(value, Mapping):
            out: dict[str, int] = {}
            for key, seq in value.items():
                if not isinstance(key, str) or not key:
                    raise VadeError("INVALID_ARGUMENT",
                                    "since_seq keys must be session ids")
                out[key] = cls._seq_int(seq)
            return out or None
        seq = cls._seq_int(value)
        sid = str(session_id or "")
        if not sid:
            raise VadeError(
                "INVALID_ARGUMENT",
                "since_seq needs a session: pass session_id with an integer, "
                "or a {session_id: seq} object for several sessions",
            )
        return {sid: seq}

    # --------------------------------------------------------------- delivery
    def _queue_for(self, sub: Subscription) -> DeliveryQueue | None:
        if not sub.connection_id:
            return None
        sink = (self._connection_table() or {}).get(sub.connection_id)
        if sink is not None:
            self.watch(sink)
            if self.table.get(sub.connection_id) is None:
                return None          # the socket was already closed: no queue
        key = (sub.instance_id, sub.connection_id)
        queue = self._queues.get(key)
        if queue is None:
            queue = DeliveryQueue(
                instance_id=sub.instance_id, connection_id=sub.connection_id, sink=sink,
                capacity=self.capacity, timeout_s=self.timeout_s, _audit=self._audit_delivery,
            )
            self._queues[key] = queue
        elif queue.sink is None:
            queue.sink = sink
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
            # Count frames the queue actually took: a closed queue refuses and
            # `offer` audits it as dropped, so counting the attempt would report
            # a delivery that never happened.
            if queue.offer(self._envelope(topic, session_id, seq_from, seq_to, body)):
                sent += 1
        return sent

    def _replay(self, sub: Subscription) -> int:
        """Fill `seq+1 ..` per session from the journal. Only `session.turn`.

        `sub.since_seq` is already a `{session_id: seq}` map; each session is
        compared against its own counter. A journal entry keeps its topic on the
        way out: `session.closed` is not replayable by design, and re-labelling
        it as `session.turn` would hand the consumer a payload whose shape does
        not match the topic it arrived under.
        """
        if "session.turn" not in sub.topics or not sub.since_seq:
            return 0
        queue = self._queue_for(sub)
        if queue is None:
            return 0
        sent = 0
        for session_id, since in sub.since_seq.items():
            for entry in list(self._journal.get(session_id, ())):
                if entry.get("topic") != "session.turn":
                    continue
                if entry["seq_to"] <= since:
                    continue
                body, _hit = tx.redact_payload(entry["payload"], sub.redact or self.table.redact)
                if queue.offer(self._envelope(entry["topic"], session_id,
                                              entry["seq_from"], entry["seq_to"], body)):
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
                session_ids = self._session_ids(store)
            except Exception as exc:  # a broken listing must not kill the tick
                self._audit("events.pump_failed", ok=False, reason=type(exc).__name__)
                session_ids = sorted(self._watermarks)
        emitted = 0
        for sid in session_ids:
            try:
                emitted += self._pump_one(store, sid)
            except Exception as exc:  # one broken session must not silence the rest
                self._audit("events.pump_failed", session_id=sid, ok=False,
                            reason=type(exc).__name__)
        return emitted

    @staticmethod
    def _session_ids(store: Any) -> list[str]:
        """Every session id -- archived included -- not just the newest 50.

        `list_session_rows` is the light page (metadata only, no messages or
        turns), so walking it to the end is cheap enough for a per-turn tick.
        `list_sessions` is the legacy fallback: its default limit is what hid
        every session past #50, so it is called with a growing limit until the
        store returns a short page. Full `Session` loads there are the price of
        a store that predates RFC-0010, and correctness wins over that cost.
        """
        pager = getattr(store, "list_session_rows", None)
        ids: list[str] = []
        if callable(pager):
            try:
                for archived in (False, True):
                    offset = 0
                    while True:
                        page = list(pager(limit=SESSION_PAGE_SIZE, offset=offset,
                                          archived=archived) or ())
                        ids.extend(str(row.id) for row in page)
                        if len(page) < SESSION_PAGE_SIZE:
                            break
                        offset += len(page)
                return list(dict.fromkeys(ids))
            except TypeError:
                ids = []          # older store signature: fall through
        limit = SESSION_PAGE_SIZE
        while True:
            page = list(store.list_sessions(limit=limit) or ())
            ids.extend(str(session.id) for session in page)
            if len(page) < limit:
                break
            limit *= 2
        return list(dict.fromkeys(ids))

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
            key = (session_id, turn_id)
            if payload is None:
                if not self._session_present(store, session_id):
                    # Definitively absent: the session itself is gone, so no
                    # `turns` row can ever appear for this event.
                    self._give_up_turn(session_id, turn_id,
                                       "session vanished before its turn row landed")
                    continue
                if self._retry_missing_turn(session_id, turn_id):
                    # Temporarily invisible: hold the watermark just before
                    # this event so the next pump retries it.
                    self._turn_first_seq.setdefault(key, first_seq.get(turn_id, int(event.seq)))
                    self._watermarks[session_id] = max(watermark, int(event.seq) - 1)
                    return emitted
                # Bounded retries spent: audit, remember the turn so a later
                # event cannot resurrect it, and advance -- one broken turn may
                # not stop the whole session.
                self._give_up_turn(session_id, turn_id,
                                   f"turn row absent after {self.turn_retries + 1} pumps")
                continue
            self._turn_attempts.pop(key, None)
            self._remember_turn(session_id, turn_id)
            seq_from = self._turn_first_seq.pop(key, first_seq.get(turn_id, int(event.seq)))
            emitted += self._dispatch("session.turn", session_id, seq_from,
                                      int(event.seq), payload)
        self._watermarks[session_id] = int(events[-1].seq)
        return emitted

    def _retry_missing_turn(self, session_id: str, turn_id: str) -> bool:
        """True while the `turns` row may still land; False once the budget is spent."""
        key = (session_id, turn_id)
        attempts = self._turn_attempts.get(key, 0) + 1
        if attempts <= self.turn_retries:
            self._turn_attempts[key] = attempts
            return True
        return False

    def _give_up_turn(self, session_id: str, turn_id: str, reason: str) -> None:
        """Stop retrying one turn: audit it and never build a payload for it again."""
        key = (session_id, turn_id)
        self._turn_attempts.pop(key, None)
        self._turn_first_seq.pop(key, None)
        self._remember_turn(session_id, turn_id)
        self._audit("events.turn_missing", session_id=session_id, ok=False, reason=reason)

    @staticmethod
    def _session_present(store: Any, session_id: str) -> bool:
        """False only when the session row is gone -- then no turn row can arrive.

        A read error is not proof of absence: it keeps the bounded-retry path so
        a transient store failure cannot silently drop a real turn.
        """
        try:
            return store.load(session_id) is not None
        except Exception:
            return True

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
        self._hooked.clear()
