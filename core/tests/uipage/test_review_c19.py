"""The five blocking + three should-fix findings of the C-19 review.

One section per finding, each written so that deleting the fix turns it red:

    B1 `ui/*` had no caller authentication          -> `test_b1_*`
    B2 an approval was not bound to session/device  -> `test_b2_*`
    B3 read-only sessions were not enforced         -> `test_b3_*`
    B4 ref pages > 8 KiB were refused               -> `test_b4_*`
    B5 a stale state read could overwrite a newer   -> `test_b5_*`
    S3 ref page cache was not keyed by digest       -> `test_s3_*`
    S4 `open_url` accepted a host it would not open -> `test_s4_*`
    S5 the schema did not close the node tree       -> `test_s5_*`

The probes in the review reached these through a real socket; these reach them
through the same service entry points the socket handler calls, with a
`FakeConnection` that carries the identity `transport/server.py` would have
bound (whitelist `module_id#instance_id`, or a `ui-…` browser session).
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from vade.errors import VadeError
from vade.modulepage.auth import caller_of
from vade.registry.manifest import load_schema, validate_manifest
from vade.registry.uipage import MAX_NODES, MAX_REF_BYTES, page_digest, validate_ui

from .helpers import (
    HIGH_TOOL,
    TOOL,
    FakeCaller,
    FakeConnection,
    FakeRecord,
    FakeRegistry,
    make_approval,
    make_manifest,
    make_service,
    page,
)

A = ("com.example.download", "dl-01")          # the module `make_service` registers
B = ("com.example.other", "o-01")              # a second module nobody may touch


def b_page(children: list | None = None) -> dict:
    return page("queue", children=children)


def two_module_service(*, approval=None):
    """A registry holding module A and module B, each with its own page."""
    manifest_a = make_manifest(page("queue"), module_id=A[0])
    manifest_b = make_manifest(page("queue"), module_id=B[0])
    record_a = FakeRecord(A[0], "dl-01", manifest_a, connection_id="conn-a")
    record_b = FakeRecord(B[0], "o-01", manifest_b, connection_id="conn-b")
    from types import SimpleNamespace

    from vade.modulepage.service import ModulePageService, PageLimits

    app = SimpleNamespace(
        registry=FakeRegistry(record_a, record_b),
        module_caller=FakeCaller({"state/get": {"rev": 1, "state": {"b_secret": 1}}}),
        router=None,
        approval_port=approval,
        session_handlers={},
    )
    service = ModulePageService(app, limits=PageLimits())
    return service, record_a, record_b


# ------------------------------------------------------------------------ B1
def test_b1_module_a_may_not_read_module_b_state():
    async def scenario() -> None:
        service, _a, _b = two_module_service()
        with pytest.raises(VadeError) as excinfo:
            await service.get_state(FakeConnection(), B[0], B[1], "queue")
        assert excinfo.value.code == "PERMISSION_DENIED"
        assert excinfo.value.data == {"field": "module_id"}

    asyncio.run(scenario())


def test_b1_module_a_may_not_read_module_b_page():
    async def scenario() -> None:
        service, _a, _b = two_module_service()
        with pytest.raises(VadeError) as excinfo:
            await service.get_page(FakeConnection(), B[0], B[1], "queue")
        assert excinfo.value.code == "PERMISSION_DENIED"

    asyncio.run(scenario())


def test_b1_module_a_may_not_call_module_b_tool():
    async def scenario() -> None:
        service, _a, _b = two_module_service(approval=make_approval())
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(FakeConnection(), {
                "module_id": B[0], "instance_id": B[1], "page_id": "queue",
                "tool": "refresh", "args": {}, "client_call_id": "c-1",
            })
        assert excinfo.value.code == "PERMISSION_DENIED"

    asyncio.run(scenario())


def test_b1_module_a_may_not_route_module_b_action():
    """`route_action` is a fifth entry point and had the same hole."""
    async def scenario() -> None:
        service, _a, _b = two_module_service()
        with pytest.raises(VadeError) as excinfo:
            await service.route_action(
                FakeConnection(), module_id=B[0], instance_id=B[1],
                page_id="queue", node_id="refresh-btn", event="click",
            )
        assert excinfo.value.code == "PERMISSION_DENIED"

    asyncio.run(scenario())


def test_b1_module_a_may_still_read_its_own_page_and_state():
    async def scenario() -> None:
        service, record, _caller, _router = make_service(
            page("queue"), caller=FakeCaller({"state/get": {"rev": 2, "state": {"queue_len": 2}}}),
        )
        connection = FakeConnection()
        served = await service.get_page(connection, record.module_id, record.instance_id, "queue")
        assert served["schema"]["type"] == "column"
        state = await service.get_state(connection, record.module_id, record.instance_id, "queue")
        assert state["state"] == {"queue_len": 2}

    asyncio.run(scenario())


def test_b1_browser_session_may_read_an_enabled_page_but_not_call_a_tool():
    async def scenario() -> None:
        approval = make_approval()
        service, record, _caller, router = make_service(
            page("queue"), approval=approval,
            caller=FakeCaller({"state/get": {"rev": 4, "state": {"queue_len": 1}}}),
        )
        browser = FakeConnection.browser()

        served = await service.get_page(browser, record.module_id, record.instance_id, "queue")
        assert served["schema"]["type"] == "column"
        state = await service.get_state(browser, record.module_id, record.instance_id, "queue")
        assert state["rev"] == 4

        # A browser may READ a page. A tool call from a browser is still a tool
        # call: it goes through the approval flow, not around it.
        result = await service.tool_call(browser, {
            "module_id": record.module_id, "instance_id": record.instance_id,
            "page_id": "queue", "tool": "refresh", "args": {}, "client_call_id": "c-1",
        })
        assert result["status"] == "ok"
        assert [call["tool_id"] for call in router.calls] == [f"{record.module_id}/refresh"]

    asyncio.run(scenario())


def test_b1_browser_session_may_not_read_a_disabled_module_page():
    async def scenario() -> None:
        service, record, _caller, _router = make_service(page("queue"))
        record.enabled = False
        with pytest.raises(VadeError) as excinfo:
            await service.get_page(FakeConnection.browser(), record.module_id, record.instance_id, "queue")
        assert excinfo.value.code == "PERMISSION_DENIED"
        assert "disabled" in excinfo.value.message

    asyncio.run(scenario())


def test_b1_anonymous_connection_is_refused_everywhere():
    async def scenario() -> None:
        approval = make_approval()
        service, record, _caller, _router = make_service(page("queue"), approval=approval)
        anonymous = FakeConnection.anonymous()
        for call in (
            service.get_page(anonymous, record.module_id, record.instance_id, "queue"),
            service.get_state(anonymous, record.module_id, record.instance_id, "queue"),
            service.tool_call(anonymous, {
                "module_id": record.module_id, "instance_id": record.instance_id,
                "page_id": "queue", "tool": "refresh", "args": {}, "client_call_id": "c",
            }),
            service.route_action(anonymous, module_id=record.module_id,
                                 instance_id=record.instance_id, page_id="queue",
                                 node_id="refresh-btn", event="click"),
        ):
            with pytest.raises(VadeError) as excinfo:
                await call
            assert excinfo.value.code == "PERMISSION_DENIED"

    asyncio.run(scenario())


def test_b1_identity_comes_from_the_connection_not_the_params():
    """The whole bug in one assertion: params cannot promote a caller."""
    async def scenario() -> None:
        service, _a, _b = two_module_service()
        # Module A's socket, but it claims to BE module B in every field a
        # client controls. The connection still says A.
        liar = FakeConnection(device_id=f"{A[0]}#{A[1]}")
        with pytest.raises(VadeError) as excinfo:
            await service.get_state(liar, B[0], B[1], "queue")
        assert excinfo.value.code == "PERMISSION_DENIED"

    asyncio.run(scenario())


def test_b1_caller_resolution_reads_only_the_connection():
    assert caller_of(FakeConnection()).kind == "module"
    assert caller_of(FakeConnection.browser()).is_browser
    assert caller_of(FakeConnection.anonymous()).kind == "unknown"
    # A `device_id` that is not `module_id#instance_id` is not a module: a bare
    # socket id must not become an identity.
    assert caller_of(FakeConnection(device_id="conn-1234")).kind == "unknown"


# ------------------------------------------------------------------------ B2
def high_risk_page() -> dict:
    from .helpers import button

    return page(children=[button("purge-btn", {"kind": "tools/call", "tool": "purge", "args": {"all": True}})])


def params(**overrides: object) -> dict:
    base = {
        "module_id": A[0], "instance_id": A[1], "page_id": "queue",
        "tool": "purge", "args": {"all": True}, "client_call_id": "c-1",
    }
    base.update(overrides)
    return base


def test_b2_approval_minted_in_one_session_cannot_be_redeemed_in_another():
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, router = make_service(
            high_risk_page(), tools=(HIGH_TOOL,), approval=approval,
        )
        connection = FakeConnection()
        first = await service.tool_call(connection, params(session_id="session-ALICE"))
        request_id = first["approval"]["request_id"]
        await approval.decide(request_id, "approve", "once", by=A[0], actor_kind="device_key")

        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(connection, params(
                session_id="session-BOB", call_id=first["call_id"], approval_id=request_id,
            ))
        assert excinfo.value.code == "PERMISSION_DENIED"
        assert excinfo.value.data == {"field": "session_id"}
        assert router.calls == [], "the write must not land in the other session"

    asyncio.run(scenario())


def test_b2_approval_minted_by_one_device_cannot_be_redeemed_by_another():
    """Two browser sessions: same module, different `ui-…` device."""
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, router = make_service(
            high_risk_page(), tools=(HIGH_TOOL,), approval=approval,
        )
        alice = FakeConnection.browser(device_id="ui-aaaa")
        first = await service.tool_call(alice, params(session_id="s-1"))
        request_id = first["approval"]["request_id"]
        await approval.decide(request_id, "approve", "once", by="ui-aaaa", actor_kind="device_key")

        bob = FakeConnection.browser(device_id="ui-bbbb")
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(bob, params(session_id="s-1", call_id=first["call_id"],
                                                approval_id=request_id))
        assert excinfo.value.code == "PERMISSION_DENIED"
        assert excinfo.value.data == {"field": "approver"}
        assert router.calls == []

        # The device that minted it still may — the binding is not a lockout.
        done = await service.tool_call(alice, params(session_id="s-1", call_id=first["call_id"],
                                                     approval_id=request_id))
        assert done["status"] == "ok"

    asyncio.run(scenario())


def test_b2_same_session_and_device_still_executes():
    """The binding must not break the legitimate path."""
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, router = make_service(
            high_risk_page(), tools=(HIGH_TOOL,), approval=approval,
        )
        connection = FakeConnection()
        first = await service.tool_call(connection, params(session_id="s-1"))
        request_id = first["approval"]["request_id"]
        await approval.decide(request_id, "approve", "once", by=A[0], actor_kind="device_key")
        second = await service.tool_call(connection, params(
            session_id="s-1", call_id=first["call_id"], approval_id=request_id,
        ))
        assert second["status"] == "ok"
        assert router.calls[0]["session_id"] == "s-1"

    asyncio.run(scenario())


def test_b2_a_refused_redemption_does_not_burn_the_approval():
    """A third party presenting the ids must not cancel the real owner's call."""
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, router = make_service(
            high_risk_page(), tools=(HIGH_TOOL,), approval=approval,
        )
        connection = FakeConnection()
        first = await service.tool_call(connection, params(session_id="session-ALICE"))
        request_id = first["approval"]["request_id"]
        await approval.decide(request_id, "approve", "once", by=A[0], actor_kind="device_key")

        with pytest.raises(VadeError) as denied:
            await service.tool_call(connection, params(
                session_id="session-BOB", call_id=first["call_id"], approval_id=request_id,
            ))
        assert denied.value.data == {"field": "session_id"}

        # The owner still redeems it: the wrong session did not consume the mint.
        done = await service.tool_call(connection, params(
            session_id="session-ALICE", call_id=first["call_id"], approval_id=request_id,
        ))
        assert done["status"] == "ok"
        assert len(router.calls) == 1, "exactly one execution, in the approved session"

        # And the mint is single-use: a second redemption is NOT_FOUND.
        with pytest.raises(VadeError) as gone:
            await service.tool_call(connection, params(
                session_id="session-ALICE", call_id=first["call_id"], approval_id=request_id,
            ))
        assert gone.value.code == "NOT_FOUND"
        assert len(router.calls) == 1

    asyncio.run(scenario())


# ------------------------------------------------------------------------ B3
def test_b3_read_only_session_may_not_call_a_low_risk_tool():
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, router = make_service(page("queue"), approval=approval)
        readonly = FakeConnection(read_only=True)
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(readonly, params(tool="refresh", args={}))
        assert excinfo.value.code == "READ_ONLY_SESSION"
        assert router.calls == []

    asyncio.run(scenario())


def test_b3_read_only_session_may_not_route_a_tool_action_either():
    async def scenario() -> None:
        approval = make_approval()
        service, record, _caller, router = make_service(page("queue"), approval=approval)
        with pytest.raises(VadeError) as excinfo:
            await service.route_action(
                FakeConnection(read_only=True), module_id=record.module_id,
                instance_id=record.instance_id, page_id="queue",
                node_id="refresh-btn", event="click",
            )
        assert excinfo.value.code == "READ_ONLY_SESSION"
        assert router.calls == []

    asyncio.run(scenario())


def test_b3_read_only_session_may_not_ui_submit():
    """`session/rpc.py::ui_submit` had no read-only check at all."""
    from types import SimpleNamespace

    from vade.session.rpc import SessionRPC

    submitted: list[dict] = []

    class _Log:
        async def submit(self, session_id, content, client_msg_id, *, device_id=None):
            submitted.append({"session_id": session_id, "device_id": device_id})
            return SimpleNamespace(id="t-1"), 0

    rpc = SessionRPC(_Log())
    with pytest.raises(VadeError) as excinfo:
        asyncio.run(rpc.ui_submit(
            FakeConnection(read_only=True),
            {"session_id": "s-1", "client_msg_id": "m-1",
             "content": [{"type": "text", "text": "hi"}]},
        ))
    assert excinfo.value.code == "READ_ONLY_SESSION"
    assert submitted == []


def test_b3_writable_session_still_submits_and_calls():
    async def scenario() -> None:
        from types import SimpleNamespace

        from vade.session.rpc import SessionRPC

        submitted: list[dict] = []

        class _Log:
            async def submit(self, session_id, content, client_msg_id, *, device_id=None):
                submitted.append({"session_id": session_id, "device_id": device_id})
                return SimpleNamespace(id="t-1"), 0

        rpc = SessionRPC(_Log())
        result = await rpc.ui_submit(
            FakeConnection(),
            {"session_id": "s-1", "client_msg_id": "m-1",
             "content": [{"type": "text", "text": "hi"}]},
        )
        assert result == {"turn_id": "t-1", "queue_pos": 0}
        assert submitted[0]["device_id"] == f"{A[0]}#{A[1]}"

        approval = make_approval()
        service, _record, _caller, router = make_service(page("queue"), approval=approval)
        out = await service.tool_call(FakeConnection(), params(tool="refresh", args={}))
        assert out["status"] == "ok" and len(router.calls) == 1

    asyncio.run(scenario())


def test_b3_a_read_only_principal_is_read_only_too():
    """`read_only` on the principal counts (AMEND-22 #14), not only on the socket."""
    async def scenario() -> None:
        from types import SimpleNamespace

        approval = make_approval()
        service, _record, _caller, _router = make_service(page("queue"), approval=approval)
        connection = FakeConnection()
        connection.principal = SimpleNamespace(read_only=True)
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(connection, params(tool="refresh", args={}))
        assert excinfo.value.code == "READ_ONLY_SESSION"

    asyncio.run(scenario())


# ------------------------------------------------------------------------ B4
def _tree_of_bytes(target: int, *, page_id: str = "big") -> dict:
    """A legal node tree (≤32 children/node, ≤8 deep, ≤256 nodes) of `target` bytes.

    Legal shape matters: the point of B4 is that a big-but-valid body was
    refused, so a tree that also broke a structural limit would prove nothing.
    """
    # root + 7 branches + 7*30 leaves = 218 nodes, depth 3.
    leaves: list[dict] = []
    branches: list[dict] = []
    for _ in range(7):
        branch_leaves = [{"type": "text", "props": {"text": "x"}} for _ in range(30)]
        branches.append({"type": "column", "props": {"gap": "none"}, "children": branch_leaves})
        leaves.extend(branch_leaves)
    tree: dict = {"type": "column", "props": {"gap": "none"}, "children": branches}

    def size() -> int:
        return len(json.dumps(tree, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode())

    # Grow the text of successive leaves (max 4096 chars each) until `target`.
    for leaf in leaves:
        if size() >= target:
            break
        needed = target - size()
        take = min(4095, max(0, needed))
        leaf["props"]["text"] = "x" * (1 + take)
    return tree


def _ref_service(tree: dict, *, caller: FakeCaller | None = None):
    ref_page = {
        "id": "big", "title": "big page", "version": 1,
        "schema": {"ref": {"page_id": "big", "sha256": page_digest(tree)}},
    }
    caller = caller or FakeCaller({"page/get": {"schema": tree}})
    return make_service(ref_page, caller=caller)


@pytest.mark.parametrize("size", [9_770, 15_614, 23_406, 29_250])
def test_b4_ref_body_above_the_inline_ceiling_is_served(size):
    """These are the four sizes the review measured; all were refused."""
    async def scenario() -> None:
        tree = _tree_of_bytes(size)
        service, record, _caller, _router = _ref_service(tree)
        result = await service.get_page(FakeConnection(), record.module_id, record.instance_id, "big")
        assert result["sha256"] == page_digest(tree)
        assert result["schema"] == tree

    asyncio.run(scenario())


def test_b4_ref_body_over_the_ref_ceiling_is_still_refused():
    async def scenario() -> None:
        tree = _tree_of_bytes(MAX_REF_BYTES + 2_000)
        service, record, _caller, _router = _ref_service(tree)
        with pytest.raises(VadeError) as excinfo:
            await service.get_page(FakeConnection(), record.module_id, record.instance_id, "big")
        assert excinfo.value.code == "MANIFEST_INVALID"
        assert "64" in excinfo.value.message or "bytes" in excinfo.value.message

    asyncio.run(scenario())


def test_b4_node_and_depth_limits_still_apply_to_a_ref_body():
    """Raising the byte ceiling must not raise the structural ones."""
    async def scenario() -> None:
        # 33 siblings: legal text, over MAX_CHILDREN (32).
        wide = {"type": "column", "children": [{"type": "text", "props": {"text": "x"}} for _ in range(33)]}
        service, record, _caller, _router = _ref_service(wide)
        with pytest.raises(VadeError) as excinfo:
            await service.get_page(FakeConnection(), record.module_id, record.instance_id, "big")
        assert excinfo.value.code == "MANIFEST_INVALID"
        assert "children" in excinfo.value.message

        # 257 nodes in legal shape (9 branches × 28 leaves + root): over MAX_NODES.
        forest = {"type": "column", "children": [
            {"type": "column", "children": [{"type": "text", "props": {"text": "x"}} for _ in range(28)]}
            for _ in range(9)
        ]}
        service, record, _caller, _router = _ref_service(forest)
        with pytest.raises(VadeError) as excinfo:
            await service.get_page(FakeConnection(), record.module_id, record.instance_id, "big")
        assert excinfo.value.code == "MANIFEST_INVALID"
        assert str(MAX_NODES) in excinfo.value.message

        deep: dict = {"type": "text", "props": {"text": "x"}}
        for _ in range(9):
            deep = {"type": "column", "children": [deep]}
        service, record, _caller, _router = _ref_service(deep)
        with pytest.raises(VadeError) as excinfo:
            await service.get_page(FakeConnection(), record.module_id, record.instance_id, "big")
        assert excinfo.value.code == "MANIFEST_INVALID"
        assert "depth" in excinfo.value.message

    asyncio.run(scenario())


def test_b4_inline_declarations_keep_the_8_kib_ceiling():
    """The override is for fetched bodies only; the manifest budget is unchanged."""
    big = _tree_of_bytes(9_770)
    pages, warnings = validate_ui({"pages": [{
        "id": "big", "title": "t", "version": 1, "schema": big,
    }]})
    assert pages == ()
    assert "8192" in warnings[0]


# ------------------------------------------------------------------------ B5
class _SlowCaller(FakeCaller):
    """A caller whose replies can be released in a chosen order."""

    def __init__(self, replies: list[dict]) -> None:
        super().__init__({})
        self._queue = list(replies)
        self.gates: list[asyncio.Event] = [asyncio.Event() for _ in replies]
        self.started = [asyncio.Event() for _ in replies]
        self._index = 0

    async def call(self, target, method, params, deadline_ms):
        index = self._index
        self._index += 1
        if index >= len(self._queue):
            return dict(self._queue[-1])
        reply = self._queue[index]
        self.started[index].set()
        await self.gates[index].wait()
        return dict(reply)


def test_b5_a_slow_stale_answer_never_overwrites_a_newer_one():
    """rev 9 answers first, rev 3 answers second — the cache must hold rev 9."""
    async def scenario() -> None:
        caller = _SlowCaller([
            {"rev": 9, "state": {"v": "new"}},
            {"rev": 3, "state": {"v": "old"}},
        ])
        service, record, _caller, _router = make_service(
            page("queue"), caller=caller, state_cache_s=0,
        )
        connection = FakeConnection()

        new_read = asyncio.create_task(
            service.get_state(connection, record.module_id, record.instance_id, "queue")
        )
        await caller.started[0].wait()
        old_read = asyncio.create_task(
            service.get_state(connection, record.module_id, record.instance_id, "queue")
        )
        await caller.started[1].wait()

        caller.gates[0].set()          # the NEW answer lands first
        fresh = await new_read
        assert fresh["rev"] == 9 and fresh["state"] == {"v": "new"}

        caller.gates[1].set()          # the OLD answer lands second
        stale = await old_read
        assert stale["rev"] == 9 and stale["state"] == {"v": "new"}, (
            "an older rev must not replace a newer one, and must not be relabelled"
        )

        cached = await service.get_state(connection, record.module_id, record.instance_id, "queue")
        assert cached["rev"] == 9 and cached["state"] == {"v": "new"}

    asyncio.run(scenario())


def test_b5_the_lock_makes_the_compare_and_swap_atomic():
    """Without the lock, two interleaved writes can both read the same old value.

    The interleaving: both reads finish the module round trip, then both
    enter the critical section. With a lock the second sees the first's write
    (rev 9) and refuses to downgrade; without it both see `None` and the later
    write wins, which is how the old code ended up with rev 3 under a rev 9 read.
    """
    async def scenario() -> None:
        caller = _SlowCaller([
            {"rev": 9, "state": {"v": "new"}},
            {"rev": 3, "state": {"v": "old"}},
        ])
        service, record, _caller, _router = make_service(
            page("queue"), caller=caller, state_cache_s=0,
        )
        connection = FakeConnection()
        first = asyncio.create_task(service.get_state(connection, record.module_id, record.instance_id, "queue"))
        await caller.started[0].wait()
        second = asyncio.create_task(service.get_state(connection, record.module_id, record.instance_id, "queue"))
        await caller.started[1].wait()
        # Release the OLD answer first so the stale write enters the section
        # while the new one is still parked; the lock is what orders them.
        caller.gates[1].set()
        caller.gates[0].set()
        stale = await second
        fresh = await first
        assert fresh["rev"] == 9 and fresh["state"] == {"v": "new"}
        assert stale["rev"] in (3, 9)  # it may have won the race; the cache decides
        cached = await service.get_state(connection, record.module_id, record.instance_id, "queue")
        assert (cached["rev"], cached["state"]) == (9, {"v": "new"}), (
            "whatever order the two answers arrive in, the cache must hold the newer rev"
        )

    asyncio.run(scenario())


def test_b5_a_backwards_rev_is_never_relabelled_upwards():
    """`max(rev, cached.rev + 1)` fabricated a rev around older data."""
    async def scenario() -> None:
        caller = FakeCaller({"state/get": {"rev": 10, "state": {"v": "new"}}})
        service, record, _c, _r = make_service(page("queue"), caller=caller, state_cache_s=0)
        connection = FakeConnection()
        first = await service.get_state(connection, record.module_id, record.instance_id, "queue")
        assert first["rev"] == 10

        caller.replies["state/get"] = {"rev": 3, "state": {"v": "old"}}
        second = await service.get_state(connection, record.module_id, record.instance_id, "queue")
        assert second["rev"] == 10, "the rev must not be inflated to 11"
        assert second["state"] == {"v": "new"}
        assert second["unchanged"] is True

    asyncio.run(scenario())


def test_b5_an_equal_rev_does_not_rewrite_the_cache():
    async def scenario() -> None:
        caller = FakeCaller({"state/get": {"rev": 5, "state": {"v": "kept"}}})
        service, record, _c, _r = make_service(page("queue"), caller=caller, state_cache_s=0)
        connection = FakeConnection()
        await service.get_state(connection, record.module_id, record.instance_id, "queue")

        caller.replies["state/get"] = {"rev": 5, "state": {"v": "different-but-same-rev"}}
        second = await service.get_state(connection, record.module_id, record.instance_id, "queue")
        assert second["rev"] == 5
        assert second["state"] == {"v": "kept"}, "same rev means same data; the cached value stands"
        assert second["unchanged"] is True

    asyncio.run(scenario())


def test_b5_two_pages_do_not_block_each_other():
    """The lock is per key, not one global lock."""
    async def scenario() -> None:
        caller = _SlowCaller([
            {"rev": 1, "state": {"v": "a"}},
            {"rev": 1, "state": {"v": "b"}},
        ])
        manifest = make_manifest(page("queue"), page("stats"), module_id=A[0])
        record = FakeRecord(A[0], "dl-01", manifest)
        from types import SimpleNamespace

        from vade.modulepage.service import ModulePageService, PageLimits

        app = SimpleNamespace(registry=FakeRegistry(record), module_caller=caller,
                              router=None, approval_port=None, session_handlers={})
        service = ModulePageService(app, limits=PageLimits(state_cache_s=0))
        connection = FakeConnection()

        first = asyncio.create_task(service.get_state(connection, A[0], A[1], "queue"))
        await caller.started[0].wait()
        second = asyncio.create_task(service.get_state(connection, A[0], A[1], "stats"))
        await caller.started[1].wait()   # both in flight: no cross-key serialisation
        caller.gates[0].set()
        caller.gates[1].set()
        assert (await first)["state"] == {"v": "a"}
        assert (await second)["state"] == {"v": "b"}

    asyncio.run(scenario())


# ------------------------------------------------------------------------ S3
def test_s3_a_capabilities_update_invalidates_the_ref_cache():
    """Pin A -> update to B -> the body served is B, not the cached A."""
    async def scenario() -> None:
        body_a = {"type": "column", "children": [{"type": "text", "props": {"text": "A"}}]}
        body_b = {"type": "column", "children": [{"type": "text", "props": {"text": "B"}}]}
        ref_page_a = {
            "id": "big", "title": "big page", "version": 1,
            "schema": {"ref": {"page_id": "big", "sha256": page_digest(body_a)}},
        }
        ref_page_b = copy.deepcopy(ref_page_a)
        ref_page_b["schema"]["ref"]["sha256"] = page_digest(body_b)

        manifest_a = make_manifest(ref_page_a, module_id=A[0])
        record = FakeRecord(A[0], "dl-01", manifest_a)
        caller = FakeCaller({"page/get": {"schema": body_a}})
        from types import SimpleNamespace

        from vade.modulepage.service import ModulePageService, PageLimits

        app = SimpleNamespace(registry=FakeRegistry(record), module_caller=caller,
                              router=None, approval_port=None, session_handlers={})
        service = ModulePageService(app, limits=PageLimits())
        connection = FakeConnection()

        first = await service.get_page(connection, A[0], A[1], "big")
        assert first["schema"] == body_a

        # `capabilities.update` swaps the declaration: same page id, new digest.
        record.manifest = make_manifest(ref_page_b, module_id=A[0])
        caller.replies["page/get"] = {"schema": body_b}
        second = await service.get_page(connection, A[0], A[1], "big")
        assert second["sha256"] == page_digest(body_b)
        assert second["schema"] == body_b, "a cached body must not survive a digest change"
        assert [m for m, _p in caller.calls] == ["page/get", "page/get"]

    asyncio.run(scenario())


def test_s3_the_same_digest_is_still_cached():
    async def scenario() -> None:
        body = {"type": "column", "children": [{"type": "text", "props": {"text": "A"}}]}
        service, record, caller, _router = _ref_service(body)
        connection = FakeConnection()
        await service.get_page(connection, record.module_id, record.instance_id, "big")
        await service.get_page(connection, record.module_id, record.instance_id, "big")
        assert [m for m, _p in caller.calls] == ["page/get"]

    asyncio.run(scenario())


# ------------------------------------------------------------------------ S4
@pytest.mark.parametrize(
    "target",
    [
        "https://user:pw@ok.example/x",
        "https://***@evil.com/x",
        "http://127.0.0.1/x",
        "http://127.1/x",
        "http://[::1]/x",
        "http://10.1.2.3/x",
        "http://192.168.1.1/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://172.20.0.1/x",
        "http://localhost/x",
        "https://ok.example/x%0aInjected: 1",
        "https://ok.example/x%0d%0a",
        "https://ok.example/page#javascript:alert(1)",
    ],
)
def test_s4_open_url_refuses_a_host_the_user_would_not_see(target):
    from vade.registry.uipage import UIPageError, check_open_url

    with pytest.raises(UIPageError):
        check_open_url(target, pointer="/to")


@pytest.mark.parametrize(
    "target",
    ["https://example.com/x", "http://example.com:8443/a?b=c#frag", "https://127.0.0.1.example.com/x"],
)
def test_s4_open_url_still_accepts_a_public_url(target):
    from vade.registry.uipage import check_open_url

    check_open_url(target, pointer="/to")  # must not raise


def test_s4_routing_rechecks_the_target():
    """Registration and routing both call the check; a poisoned action dies at routing."""
    from dataclasses import replace

    async def scenario() -> None:
        external = page("stats", children=[
            __import__("core.tests.uipage.helpers", fromlist=["button"]).button(
                "open", {"kind": "open_url", "to": "https://example.com/docs"},
            ),
        ])
        from vade.registry.uipage import validate_page

        validated = validate_page(external, module_tools=("refresh",), page_ids=["queue", "stats"])
        service, record, _caller, _router = make_service(page("queue"))
        record.manifest = replace(record.manifest, ui_pages=(validated,))

        node = next(item for item in validated.walk() if item.id == "open")
        poisoned = replace(validated, root=replace(
            validated.root,
            children=(replace(validated.root.children[0],
                              actions=(("click", replace(node.actions[0][1], target="http://127.0.0.1/x")),)),),
        ))
        record.manifest = replace(record.manifest, ui_pages=(poisoned,))
        with pytest.raises(VadeError) as excinfo:
            await service.route_action(FakeConnection(), module_id=record.module_id,
                                       instance_id=record.instance_id, page_id="stats",
                                       node_id="open", event="click")
        assert excinfo.value.code == "MANIFEST_INVALID"

    asyncio.run(scenario())


# ------------------------------------------------------------------------ S5
def _manifest_with(tree: dict) -> dict:
    return {
        "module_id": "com.example.download", "instance_id": "dl-01",
        "version": "1.0.0", "protocol": "vade/v1",
        "capabilities": {"tools": [dict(TOOL)]},
        "limits": {"max_concurrency": 1, "max_payload": 65519},
        "ui": {"pages": [{"id": "queue", "title": "queue", "version": 1, "schema": tree}]},
    }


def test_s5_the_schema_alone_refuses_a_node_level_onclick():
    """A consumer that only validates `manifest.schema.json` must be protected."""
    from vade._jsonschema import SchemaError, validate as js_validate

    payload = _manifest_with({"type": "column", "children": [
        {"type": "component", "component": "Button", "props": {"text": "Go"},
         "onClick": {"kind": "tools/call", "tool": "refresh", "args": {}}},
    ]})
    with pytest.raises(SchemaError) as excinfo:
        js_validate(payload, load_schema())
    assert "onClick" in str(excinfo.value)


def test_s5_the_schema_alone_refuses_an_unknown_prop():
    from vade._jsonschema import SchemaError, validate as js_validate

    payload = _manifest_with({"type": "column", "children": [
        {"type": "component", "component": "Button",
         "props": {"text": "Go", "onclick": "alert(1)"}},
    ]})
    with pytest.raises(SchemaError):
        js_validate(payload, load_schema())


def test_s5_the_schema_alone_refuses_an_unknown_action_key():
    from vade._jsonschema import SchemaError, validate as js_validate

    payload = _manifest_with({"type": "column", "children": [
        {"type": "component", "component": "Button", "props": {"text": "Go"},
         "on": {"click": {"kind": "tools/call", "tool": "refresh",
                          "then": {"kind": "navigate", "to": "module:queue"}}}},
    ]})
    with pytest.raises(SchemaError):
        js_validate(payload, load_schema())


def test_s5_the_schema_alone_refuses_a_host_component():
    from vade._jsonschema import SchemaError, validate as js_validate

    payload = _manifest_with({"type": "column", "children": [
        {"type": "component", "component": "ApprovalDialog", "props": {"text": "Go"}},
    ]})
    with pytest.raises(SchemaError):
        js_validate(payload, load_schema())


def test_s5_the_schema_alone_refuses_an_unknown_node_key():
    from vade._jsonschema import SchemaError, validate as js_validate

    payload = _manifest_with({"type": "column", "children": [
        {"type": "text", "props": {"text": "x"}, "script": "alert(1)"},
    ]})
    with pytest.raises(SchemaError):
        js_validate(payload, load_schema())


def test_s5_the_schema_still_accepts_a_legal_page():
    from vade._jsonschema import validate as js_validate

    payload = _manifest_with({
        "type": "column", "props": {"gap": "m"}, "children": [
            {"type": "text", "data_ref": "page.state.queue_len", "props": {"format": "int"}},
            {"type": "component", "component": "Button", "props": {"text": "Go", "style": "primary"},
             "on": {"click": {"kind": "tools/call", "tool": "refresh", "args": {}}}},
            {"type": "list", "data_ref": "page.state.items", "props": {"empty_text": "empty"},
             "children": [{"type": "component", "component": "ListItem",
                           "props": {"title": {"bind": "item.name", "format": "text"}}}]},
        ],
    })
    js_validate(payload, load_schema())


def test_s5_a_bad_page_is_still_only_a_dropped_page():
    """Closing the schema must not turn one bad page into a failed registration."""
    bad = {"type": "column", "children": [
        {"type": "component", "component": "Button", "props": {"text": "Go"},
         "onClick": {"kind": "tools/call", "tool": "refresh", "args": {}}},
    ]}
    raw = _manifest_with({"type": "column", "children": [{"type": "text", "props": {"text": "ok"}}]})
    raw["ui"]["pages"].append({"id": "evil", "title": "evil", "version": 1, "schema": bad})
    manifest = validate_manifest(raw)
    assert [p.page_id for p in manifest.ui_pages] == ["queue"]
    assert manifest.tool_names == ("refresh",)
    assert any("evil" in warning for warning in manifest.ui_warnings)
