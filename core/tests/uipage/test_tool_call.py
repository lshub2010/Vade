"""`ui/tool.call` two-stage approval + the four action kinds (RFC-0009 §6).

A3/A4 (approval semantics, digest binding) and the routing contract: a page
action either goes through an existing RPC surface or is resolved for the host —
never a fourth dispatch path, never a URL the client did not validate.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from vade.errors import VadeError
from vade.registry.uipage import validate_page

from .helpers import (
    HIGH_TOOL,
    FakeConnection,
    button,
    make_approval,
    make_service,
    page,
)

CALL = {
    "module_id": "com.example.download",
    "instance_id": "dl-01",
    "page_id": "queue",
}


def call_params(**overrides: object) -> dict:
    params = {**CALL, "tool": "purge", "args": {"all": True}, "client_call_id": "c-1"}
    params.update(overrides)
    return params


def high_risk_page() -> dict:
    return page(children=[
        button("purge-btn", {"kind": "tools/call", "tool": "purge", "args": {"all": True}}),
    ])


# ----------------------------------------------------------- two-stage approval
def test_high_risk_call_mints_an_approval_and_does_not_execute():
    async def scenario() -> None:
        approval = make_approval()
        service, record, _caller, router = make_service(high_risk_page(), tools=(HIGH_TOOL,), approval=approval)
        connection = FakeConnection()

        first = await service.tool_call(connection, call_params())
        assert first["status"] == "approval_required"
        assert router.calls == [], "stage one may never execute the tool"
        approval_obj = first["approval"]
        assert approval_obj["tool"] == "com.example.download/purge"
        assert approval_obj["risk"] == "high"
        assert approval_obj["args_digest"].startswith("sha256:")
        assert approval_obj["request_id"]

        await approval.decide(approval_obj["request_id"], "approve", "once",
                              by="dev-1", actor_kind="device_key")
        second = await service.tool_call(connection, call_params(
            call_id=first["call_id"], approval_id=approval_obj["request_id"],
        ))
        assert second["status"] == "ok"
        assert [call["tool_id"] for call in router.calls] == ["com.example.download/purge"]
        assert router.calls[0]["turn_id"] is None, "a page action is not a turn"

    asyncio.run(scenario())


def test_denied_approval_never_executes():
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, router = make_service(high_risk_page(), tools=(HIGH_TOOL,), approval=approval)
        connection = FakeConnection()
        first = await service.tool_call(connection, call_params())
        await approval.decide(first["approval"]["request_id"], "deny", "once",
                              by="dev-1", actor_kind="device_key")
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(connection, call_params(
                call_id=first["call_id"], approval_id=first["approval"]["request_id"],
            ))
        assert excinfo.value.code == "PERMISSION_DENIED"
        assert router.calls == []

    asyncio.run(scenario())


def test_second_stage_with_different_args_is_refused():
    """A4: the approver saw args A; sending B is PERMISSION_DENIED, not "close enough"."""
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, router = make_service(high_risk_page(), tools=(HIGH_TOOL,), approval=approval)
        connection = FakeConnection()
        first = await service.tool_call(connection, call_params())
        await approval.decide(first["approval"]["request_id"], "approve", "once",
                              by="dev-1", actor_kind="device_key")
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(connection, call_params(
                call_id=first["call_id"], approval_id=first["approval"]["request_id"],
                args={"all": False},
            ))
        assert excinfo.value.code == "PERMISSION_DENIED"
        assert "digest" in excinfo.value.message
        assert router.calls == []

    asyncio.run(scenario())


def test_second_stage_before_a_decision_is_refused():
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, router = make_service(high_risk_page(), tools=(HIGH_TOOL,), approval=approval)
        connection = FakeConnection()
        first = await service.tool_call(connection, call_params())
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(connection, call_params(
                call_id=first["call_id"], approval_id=first["approval"]["request_id"],
            ))
        assert excinfo.value.code == "PERMISSION_DENIED"
        assert router.calls == []

    asyncio.run(scenario())


def test_low_risk_call_executes_in_one_stage():
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, router = make_service(page("queue"), approval=approval)
        result = await service.tool_call(FakeConnection(), call_params(tool="refresh"))
        assert result["status"] == "ok"
        assert len(router.calls) == 1

    asyncio.run(scenario())


def test_high_risk_call_without_an_approval_port_fails_closed():
    async def scenario() -> None:
        service, _record, _caller, router = make_service(high_risk_page(), tools=(HIGH_TOOL,))
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(FakeConnection(), call_params())
        assert excinfo.value.code == "UNAVAILABLE"
        assert router.calls == [], "no approval port must never mean 'no approval needed'"

    asyncio.run(scenario())


def test_pending_call_table_refuses_instead_of_evicting():
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, _router = make_service(
            high_risk_page(), tools=(HIGH_TOOL,), approval=approval, pending_calls=1,
        )
        await service.tool_call(FakeConnection(), call_params())
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(FakeConnection(), call_params(client_call_id="c-2"))
        assert excinfo.value.code == "OVERLOADED"

    asyncio.run(scenario())


def test_undeclared_tool_is_not_found():
    async def scenario() -> None:
        service, _record, _caller, router = make_service(page("queue"))
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(FakeConnection(), call_params(tool="rm_rf"))
        assert excinfo.value.code == "NOT_FOUND"
        assert router.calls == []

    asyncio.run(scenario())


def test_unknown_page_is_not_found():
    async def scenario() -> None:
        service, _record, _caller, _router = make_service(page("queue"))
        with pytest.raises(VadeError) as excinfo:
            await service.tool_call(FakeConnection(), call_params(page_id="ghost"))
        assert excinfo.value.code == "NOT_FOUND"

    asyncio.run(scenario())


def test_session_scope_grant_covers_the_same_tool_and_args_only():
    async def scenario() -> None:
        approval = make_approval()
        service, _record, _caller, router = make_service(high_risk_page(), tools=(HIGH_TOOL,), approval=approval)
        connection = FakeConnection()
        first = await service.tool_call(connection, call_params())
        request_id = first["approval"]["request_id"]
        await approval.decide(request_id, "approve", "session", by="dev-1", actor_kind="device_key")
        await service.tool_call(connection, call_params(call_id=first["call_id"], approval_id=request_id))

        # Same tool, same args, same session: the grant applies (§6.2).
        again = await service.tool_call(connection, call_params(client_call_id="c-2"))
        assert again["status"] == "ok"
        assert len(router.calls) == 2

        # A different args digest is a different approval, even for the same tool.
        harder = await service.tool_call(FakeConnection(), call_params(client_call_id="c-3", args={"all": False}))
        assert harder["status"] == "approval_required"

    asyncio.run(scenario())


# ------------------------------------------------------------------- navigate
def test_navigate_in_bounds_is_resolved_not_executed():
    async def scenario() -> None:
        goto = page("stats", children=[
            button("go", {"kind": "navigate", "to": "module:queue"}),
        ])
        service, record, caller, router = make_service(page("queue"), goto)
        result = await service.route_action(
            FakeConnection(), module_id=record.module_id, instance_id=record.instance_id,
            page_id="stats", node_id="go", event="click",
        )
        assert result == {"kind": "navigate", "to": "module:queue", "external": False}
        assert caller.calls == [] and router.calls == []

    asyncio.run(scenario())


def test_navigate_to_a_page_that_no_longer_exists_is_refused():
    """Registration allowed it when the page existed; routing happens now."""
    async def scenario() -> None:
        service, record, _caller, _router = make_service(page("queue"))
        goto = page("stats", children=[button("go", {"kind": "navigate", "to": "module:ghost"})])
        stale = validate_page(goto, module_tools=("refresh",), page_ids=["queue", "stats", "ghost"])
        record.manifest = replace(record.manifest, ui_pages=(stale,))

        with pytest.raises(VadeError) as excinfo:
            await service.route_action(
                FakeConnection(), module_id=record.module_id, instance_id=record.instance_id,
                page_id="stats", node_id="go", event="click",
            )
        assert excinfo.value.code == "MANIFEST_INVALID"

    asyncio.run(scenario())


def test_open_url_is_http_only_at_routing_time():
    async def scenario() -> None:
        service, record, _caller, _router = make_service(page("queue"))
        external = page("stats", children=[
            button("open", {"kind": "open_url", "to": "https://example.com/docs"}),
        ])
        validated = validate_page(external, module_tools=("refresh",), page_ids=["queue", "stats"])
        record.manifest = replace(record.manifest, ui_pages=(validated,))

        result = await service.route_action(
            FakeConnection(), module_id=record.module_id, instance_id=record.instance_id,
            page_id="stats", node_id="open", event="click",
        )
        assert result["external"] is True and result["to"].startswith("https://")

        node = next(item for item in validated.walk() if item.id == "open")
        action = node.actions[0][1]
        poisoned_action = replace(action, target="javascript:alert(1)")
        root = validated.root
        assert root is not None and root.children
        poisoned = replace(
            validated,
            root=replace(root, children=(
                replace(root.children[0], actions=(("click", poisoned_action),)),
            )),
        )
        record.manifest = replace(record.manifest, ui_pages=(poisoned,))
        with pytest.raises(VadeError) as excinfo:
            await service.route_action(
                FakeConnection(), module_id=record.module_id, instance_id=record.instance_id,
                page_id="stats", node_id="open", event="click",
            )
        assert excinfo.value.code == "MANIFEST_INVALID"

    asyncio.run(scenario())


# --------------------------------------------------------------------- submit
def test_submit_delegates_to_the_existing_ui_submit_handler():
    async def scenario() -> None:
        submit_page = page("chat", requires=["session"], children=[
            button("send", {"kind": "ui/submit", "content": [{"type": "text", "text": "hello"}]}),
        ])
        service, record, _caller, _router = make_service(submit_page)
        seen: list[dict] = []

        async def fake_submit(connection, params):  # the C3 handler's shape
            seen.append(dict(params))
            return {"turn_id": "t-1", "queue_pos": 0}

        service._app.session_handlers["ui/submit"] = fake_submit
        result = await service.route_action(
            FakeConnection(), module_id=record.module_id, instance_id=record.instance_id,
            page_id="chat", node_id="send", event="click", session_id="s-1", client_msg_id="m-1",
        )
        assert result == {"turn_id": "t-1", "queue_pos": 0}
        assert seen[0]["session_id"] == "s-1"
        assert seen[0]["client_msg_id"] == "m-1"
        assert seen[0]["content"] == [{"type": "text", "text": "hello"}]

    asyncio.run(scenario())


def test_action_with_unresolved_placeholder_is_refused():
    async def scenario() -> None:
        submit_page = page("chat", requires=["session"], children=[
            button("send", {"kind": "ui/submit", "content": [{"type": "text", "text": {"input": "q"}}]}),
        ])
        service, record, _caller, _router = make_service(submit_page)
        with pytest.raises(VadeError) as excinfo:
            await service.route_action(
                FakeConnection(), module_id=record.module_id, instance_id=record.instance_id,
                page_id="chat", node_id="send", event="click", session_id="s-1",
            )
        assert excinfo.value.code == "INVALID_ARGUMENT"

    asyncio.run(scenario())


def test_tool_action_routes_through_the_same_call_path():
    async def scenario() -> None:
        service, record, _caller, router = make_service(page("queue"), approval=make_approval())
        result = await service.route_action(
            FakeConnection(), module_id=record.module_id, instance_id=record.instance_id,
            page_id="queue", node_id="refresh-btn", event="click", session_id="s-1",
        )
        assert result["status"] == "ok"
        assert router.calls[0]["args"] == {"all": True}

    asyncio.run(scenario())
