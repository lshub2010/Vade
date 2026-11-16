"""Subscription table and the three-of-four permission gate (C-14).

A module can only ever *receive*. Nothing here lets a caller ask for a turn, a
session or a skill body; `events/subscribe` declares interest and core decides.
Four independent permissions, all of which must hold for `session.turn` /
`session.closed`:

1. the caller's `(module_id, public key fingerprint)` is in core's
   `[evolve] allowlist` -- the only one a module cannot forge;
2. its manifest declared the topic in `subscriptions`;
3. core's config has `[evolve] allow_transcript = true`;
4. its role is `evolve` (a tool/ui module never sees a transcript).

`skill.proposed` is metadata only, so gate (2) plus role `ui|admin` is the
whole check: it is a front-end beat, not a transcript, so neither the evolve
allowlist (1) nor the transcript switch (3) applies. A module that is not
allowlisted can still receive it when its manifest declares it and its role is
`ui` or `admin`; a tool/ui module can never receive `session.*`.

A request may name at most `MAX_TOPICS` (64) topics. A longer list is
`INVALID_ARGUMENT` before any gate runs -- the gate walks the request once, and
an unbounded array is a cheap way to spend core's time. Repeated topics collapse
to one row in `accepted` and one in `rejected`, so the response length follows
the distinct request, not how many times a client repeated itself.

Failures are `rejected: [{topic, reason}]`, never a dropped connection: an
evolve module that may not read transcripts can still use tools.

Topic names are validated **here** and not at registration time: C2 validates
`subscriptions` as a string array only (and C-03 is frozen), so an unknown topic
must be a `rejected` row, not a manifest error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..errors import VadeError

__all__ = [
    "TOPICS", "EVOLVE_TOPICS", "ADMIN_TOPICS", "REASONS", "MAX_TOPICS",
    "Subscription", "SubscriptionTable", "parse_allowlist", "fingerprint_matches",
    "validate_topics",
]

EVOLVE_TOPICS: frozenset[str] = frozenset({"session.turn", "session.closed"})
ADMIN_TOPICS: frozenset[str] = frozenset({"skill.proposed"})
TOPICS: frozenset[str] = EVOLVE_TOPICS | ADMIN_TOPICS

#: One `events/subscribe` may name at most this many topics. The channel has
#: three, so anything near this is a client bug or an attempt to make core walk
#: an unbounded array; refuse it before the per-topic gate runs.
MAX_TOPICS = 64


#: Exact strings that appear in `events/subscribe` rejections. A test pins them:
#: an operator greps for these, so they may not drift into prose.
REASONS: Mapping[str, str] = {
    "unknown": "unknown topic",
    "manifest": "not in manifest",
    "allowlist": "not allowlisted",
    "config": "disabled by config",
    "role": "role not allowed",
}


def _fp(value: str) -> str:
    return "".join(str(value).split()).lower().replace(":", "")


def parse_allowlist(entries: Iterable[Any] | None) -> frozenset[tuple[str, str]]:
    """`[evolve] allowlist` -> `{(module_id, fingerprint)}`.

    Accepted entry shapes (all normalise to the same pair):
      * ``"com.example.evolve:ab12...:..."``  (module_id `:` fingerprint)
      * ``"com.example.evolve@ab12..."``
      * ``"com.example.evolve ab12..."``
      * ``("com.example.evolve", "ab12...")``
      * ``{"module_id": ..., "fingerprint": ...}``

    An unparsable entry is skipped, not guessed at: guessing would either widen
    the allowlist (a security bug) or silently lock a module out.
    """
    out: set[tuple[str, str]] = set()
    for entry in entries or ():
        module_id = fingerprint = ""
        if isinstance(entry, Mapping):
            module_id = str(entry.get("module_id") or "")
            fingerprint = str(entry.get("fingerprint") or "")
        elif isinstance(entry, (tuple, list)) and len(entry) == 2:
            module_id, fingerprint = str(entry[0]), str(entry[1])
        elif isinstance(entry, str):
            raw = entry.strip()
            for separator in (":", "@", " ", "\t"):
                if separator in raw:
                    module_id, _, fingerprint = raw.partition(separator)
                    break
        if module_id and fingerprint:
            out.add((module_id.strip(), _fp(fingerprint)))
    return frozenset(out)


def fingerprint_matches(actual: str, expected: str) -> bool:
    """Exact, or a prefix of at least 16 hex chars (64 bits).

    A truncated fingerprint in `core.toml` is common (operators copy the fp8 the
    CLI prints); 16 chars is the shortest prefix we accept, because a shorter one
    would let a colliding key inherit an allowlist slot.
    """
    actual_n, expected_n = _fp(actual), _fp(expected)
    if not actual_n or not expected_n:
        return False
    if len(expected_n) >= 16:
        return actual_n == expected_n or actual_n.startswith(expected_n)
    return actual_n == expected_n


def validate_topics(topics: Any) -> tuple[str, ...]:
    """Validate `events/subscribe`'s `topics` field; raise `INVALID_ARGUMENT`.

    Shape, then element type, then count. The count check is a refusal, not a
    truncation: silently dropping the 65th topic would tell a client it
    subscribed to something it did not.
    """
    if not isinstance(topics, (list, tuple)) or not topics:
        raise VadeError("INVALID_ARGUMENT", "topics must be a non-empty array of topic names")
    if any(not isinstance(topic, str) for topic in topics):
        raise VadeError("INVALID_ARGUMENT", "topics must contain strings only")
    if len(topics) > MAX_TOPICS:
        raise VadeError("INVALID_ARGUMENT",
                        f"topics may contain at most {MAX_TOPICS} entries")
    return tuple(topics)


@dataclass(frozen=True, slots=True)
class Subscription:
    """One instance's live subscription. In memory only: a restart forgets it."""

    module_id: str
    instance_id: str
    connection_id: str
    role: str
    topics: frozenset[str]
    #: `{session_id: seq}`: a resume point is per session, never one number
    #: compared against every session's counter (see `EventBus._since_seq`).
    since_seq: Mapping[str, int] | None = None
    redact: tuple[str, ...] = ()
    subscribed_at: float = field(default_factory=time.time)

    def wants(self, topic: str) -> bool:
        return topic in self.topics


class SubscriptionTable:
    """The table plus the gate. No I/O, no asyncio: trivially testable."""

    def __init__(
        self,
        *,
        allow_transcript: bool = False,
        allowlist: Iterable[Any] | Sequence[Any] | None = (),
        redact: Iterable[str] | None = (),
        clock: Any = time.time,
    ) -> None:
        self.allow_transcript = bool(allow_transcript)
        self.allowlist = parse_allowlist(allowlist)
        #: core-side redaction list; a request may not add to or remove from it.
        self.redact = tuple(str(f) for f in (redact or ()))
        self._clock = clock
        self._by_connection: dict[str, Subscription] = {}
        self.redact_override_attempts = 0

    # ------------------------------------------------------------------ table
    def put(self, subscription: Subscription) -> Subscription:
        """Same instance subscribing twice overwrites; it is not an error."""
        self._by_connection[subscription.connection_id] = subscription
        return subscription

    def get(self, connection_id: str) -> Subscription | None:
        return self._by_connection.get(connection_id)

    def drop(self, connection_id: str) -> None:
        self._by_connection.pop(connection_id, None)

    def subscribers(self, topic: str) -> list[Subscription]:
        return [s for s in self._by_connection.values() if topic in s.topics]

    def instances(self) -> list[str]:
        return sorted({s.instance_id for s in self._by_connection.values()})

    def __len__(self) -> int:
        return len(self._by_connection)

    # ------------------------------------------------------------------- gate
    def is_allowlisted(self, module_id: str, fingerprint: str) -> bool:
        for allowed_module, allowed_fp in self.allowlist:
            if allowed_module == module_id and fingerprint_matches(fingerprint, allowed_fp):
                return True
        return False

    def evaluate(
        self,
        topics: Iterable[str],
        *,
        declared: Iterable[str],
        role: str,
        module_id: str,
        fingerprint: str,
    ) -> tuple[list[str], list[dict[str, str]]]:
        """Split requested topics into `accepted` / `rejected` (never raises)."""
        declared_set = {str(t) for t in declared or ()}
        role_n = str(role or "")
        accepted: list[str] = []
        accepted_seen: set[str] = set()
        rejected: list[dict[str, str]] = []
        rejected_seen: set[str] = set()
        for topic in topics:
            reason = self._reject_reason(topic, declared_set, role_n, module_id, fingerprint)
            if reason is None:
                if topic not in accepted_seen:
                    accepted_seen.add(topic)
                    accepted.append(topic)
            elif topic not in rejected_seen:
                # One row per topic: a client that repeats a bad name gets the
                # same answer once, not an N-times-duplicated rejection list.
                rejected_seen.add(topic)
                rejected.append({"topic": topic, "reason": reason})
        return accepted, rejected

    def _reject_reason(
        self,
        topic: str,
        declared: set[str],
        role: str,
        module_id: str,
        fingerprint: str,
    ) -> str | None:
        if topic not in TOPICS:
            return REASONS["unknown"]
        if topic not in declared:
            return REASONS["manifest"]
        if topic in EVOLVE_TOPICS:
            # Allowlist first: it is the one permission a module cannot forge, so
            # an unlisted caller must not learn anything about the other gates.
            if not self.is_allowlisted(module_id, fingerprint):
                return REASONS["allowlist"]
            if role != "evolve":
                return REASONS["role"]
            if not self.allow_transcript:
                return REASONS["config"]
            return None
        # skill.proposed: metadata only, no transcript and no evolve allowlist --
        # a ui/admin front end is a device, not an evolve module.
        if role not in ("ui", "admin"):
            return REASONS["role"]
        return None

    # ---------------------------------------------------------------- request
    def resolve_redact(self, requested: Any, *, present: bool) -> tuple[tuple[str, ...], bool]:
        """Return `(fields, override_attempted)`.

        `allowlist` / `redact` come from core config only (design §16.7). A
        module that sends `redact: []` is trying to switch redaction off; that is
        recorded for audit and ignored. A module may narrow the set (more
        redaction) -- that is not a permission it can abuse.
        """
        if not present:
            return self.redact, False
        if not isinstance(requested, (list, tuple)):
            raise VadeError("INVALID_ARGUMENT", "redact must be an array of field names")
        wanted = tuple(str(item) for item in requested)
        if not wanted and self.redact:
            self.redact_override_attempts += 1
            return self.redact, True
        return tuple(sorted(set(wanted) | set(self.redact))), False
