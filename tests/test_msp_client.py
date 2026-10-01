"""Hermetic tests for the vendored MSP client.

Every test drives :class:`MspClient` against
``fixtures/fake_msp_host.py`` over real stdio pipes — no `muse` binary,
credentials, or network needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from omnigent.community.harness.muse.inner.msp_client import (
    MspApprovalRequested,
    MspClient,
    MspConnectionClosed,
    MspError,
    MspProtocolError,
    MspTextDelta,
    MspTokenUsage,
    MspTurnCompleted,
    mint_command_id,
)

FAKE_HOST = Path(__file__).parent / "fixtures" / "fake_msp_host.py"


def _spawn_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env["FAKE_MSP_LOG"] = str(tmp_path / "fake.log")
    env.update(overrides)
    return env


async def _spawn(tmp_path: Path, **overrides: str) -> MspClient:
    return await MspClient.spawn(
        [sys.executable, "-u", str(FAKE_HOST)],
        env=_spawn_env(tmp_path, **overrides),
        label="test",
    )


def _logged(tmp_path: Path) -> list[dict]:
    log = tmp_path / "fake.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _method_frames(tmp_path: Path, method: str) -> list[dict]:
    return [f for f in _logged(tmp_path) if f.get("method") == method]


async def test_spawn_records_fingerprint_and_version(tmp_path: Path) -> None:
    client = await _spawn(tmp_path)
    try:
        assert client.fingerprint == "sha256:fake"
        assert client.host_version == "0.0.0-fake"
        assert not client.closed
        # The handshake ends with the `initialized` notification.
        await client.flush()
        for _ in range(100):
            if _method_frames(tmp_path, "initialized"):
                break
            await asyncio.sleep(0.01)
        assert _method_frames(tmp_path, "initialized")
    finally:
        await client.close()


async def test_client_name_must_be_machine_identifier(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\^\[a-z0-9"):
        await MspClient.spawn(
            [sys.executable, "-u", str(FAKE_HOST)],
            env=_spawn_env(tmp_path),
            client_name="not-valid",
        )


async def test_start_session_carries_mcp_config(tmp_path: Path) -> None:
    client = await _spawn(tmp_path)
    try:
        session = await client.start_session(
            provider_id="echo",
            workspace_root="/tmp/ws",
            approval_mode="allowAll",
            mcp_servers={"omnigent": {"transport": "stdio", "command": "serve-mcp"}},
        )
        assert session["sessionId"] == "sess-1"
        starts = _method_frames(tmp_path, "session/start")
        assert len(starts) == 1
        config = starts[0]["params"]["config"]
        assert config["mcpServers"]["omnigent"]["command"] == "serve-mcp"
    finally:
        await client.close()


async def test_send_turn_and_follow_stream(tmp_path: Path) -> None:
    client = await _spawn(tmp_path)
    try:
        session = await client.start_session(provider_id="echo")
        with client.open_stream(session["sessionId"]) as stream:
            turn_id = await client.send_turn(
                session["sessionId"], [{"type": "text", "text": "hi"}]
            )
            events = [e async for e in stream.follow(turn_id)]
        deltas = [e for e in events if isinstance(e, MspTextDelta)]
        assert "".join(d.delta for d in deltas) == "Hello, world"
        [usage] = [e for e in events if isinstance(e, MspTokenUsage)]
        assert (usage.prompt_tokens, usage.output_tokens, usage.total_tokens) == (
            10,
            5,
            15,
        )
        assert usage.cumulative["totalTokens"] == 15
        assert usage.model_id == "fake-model"
        [completed] = [e for e in events if isinstance(e, MspTurnCompleted)]
        assert completed.turn_id == turn_id
        assert completed.error_kind is None
        assert completed.usage["totalTokens"] == 15
    finally:
        await client.close()


async def test_turn_and_session_helpers_smoke(tmp_path: Path) -> None:
    client = await _spawn(tmp_path)
    try:
        session_id = (await client.start_session())["sessionId"]
        assert (await client.interrupt_turn(session_id))["status"] == "accepted"
        assert (
            await client.steer_turn(session_id, "t", [{"type": "text", "text": "x"}])
        )["status"] == "accepted"
        assert (await client.cancel_turn(session_id))["status"] == "accepted"
        assert await client.list_approvals(session_id) == {
            "approvals": [],
            "userInputs": [],
        }
        assert (await client.decide_approval(session_id, "a", "c", "r"))[
            "status"
        ] == "accepted"
        assert "usage" in await client.read_usage(session_id)
        assert (await client.compact_session(session_id))["status"] == "accepted"
        assert (await client.set_model(session_id, "m"))["status"] == "accepted"
        assert (await client.set_reasoning_effort(session_id, "high"))[
            "status"
        ] == "accepted"
        assert (await client.resume_session(session_id))["sessionId"] == session_id
        assert (await client.fork_session(session_id))["sessionId"] == session_id
    finally:
        await client.close()


async def test_command_retries_backpressure_once(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_BACKPRESSURE_ONCE="1")
    try:
        session_id = (await client.start_session())["sessionId"]
        with client.open_stream(session_id) as stream:
            turn_id = await client.send_turn(
                session_id, [{"type": "text", "text": "hi"}]
            )
            events = [e async for e in stream.follow(turn_id)]
        assert any(isinstance(e, MspTurnCompleted) for e in events)
        # One failed attempt plus the same-commandId retry.
        starts = _method_frames(tmp_path, "turn/start")
        assert len(starts) == 2
        assert starts[0]["params"]["commandId"] == starts[1]["params"]["commandId"]
    finally:
        await client.close()


async def test_non_retryable_error_raises_at_once(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_DENY="turn/interrupt")
    try:
        session_id = (await client.start_session())["sessionId"]
        with pytest.raises(MspError) as exc_info:
            await client.interrupt_turn(session_id)
        assert exc_info.value.kind == "invalidParams"
        assert exc_info.value.code == -32602
        assert len(_method_frames(tmp_path, "turn/interrupt")) == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("kind", "retryable", "attempts"),
    [
        ("backpressured", False, 1),
        ("overloaded", False, 1),
        ("backpressured", None, 2),
        ("overloaded", None, 2),
        ("backpressured", "invalid", 2),
        ("temporary", True, 2),
        ("invalidParams", None, 1),
    ],
)
async def test_command_retry_flag_is_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    retryable: bool | str | None,
    attempts: int,
) -> None:
    client = await _spawn(tmp_path)
    calls: list[dict[str, Any]] = []

    async def request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        calls.append(dict(params or {}))
        if len(calls) == 1:
            data = {} if retryable is None else {"retryable": retryable}
            raise MspError(-32001, "busy", kind=kind, data=data)
        return {"status": "accepted"}

    monkeypatch.setattr(client, "request", request)
    try:
        if attempts == 1:
            with pytest.raises(MspError):
                await client.command("turn/start", {"sessionId": "s"})
        else:
            assert await client.command("turn/start", {"sessionId": "s"}) == {
                "status": "accepted"
            }
        assert len(calls) == attempts
        assert len({call["commandId"] for call in calls}) == 1
    finally:
        await client.close()


async def test_command_retry_exhaustion_preserves_command_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await _spawn(tmp_path)
    calls: list[dict[str, Any]] = []

    async def request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        calls.append(dict(params or {}))
        raise MspError(-32001, "busy", kind="backpressured", data={"retryable": True})

    monkeypatch.setattr(client, "request", request)
    try:
        with pytest.raises(MspError, match="busy"):
            await client.command("turn/start", {"sessionId": "s"}, max_attempts=2)
        assert len(calls) == 2
        assert calls[0]["commandId"] == calls[1]["commandId"]
    finally:
        await client.close()


async def test_malformed_response_is_protocol_error(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_BAD_RESPONSE="1")
    try:
        with pytest.raises(MspProtocolError, match="exactly one"):
            await client.start_session()
    finally:
        await client.close()


async def test_rejected_handshake_closes_cleanly(tmp_path: Path) -> None:
    with pytest.raises(MspConnectionClosed, match="rejected"):
        await _spawn(tmp_path, FAKE_MSP_SCENARIO="reject_init")


async def test_host_death_mid_turn(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_SCENARIO="die_on_turn")
    try:
        session_id = (await client.start_session())["sessionId"]
        with (
            client.open_stream(session_id) as stream,
            pytest.raises(MspConnectionClosed),
        ):
            turn_id = await client.send_turn(
                session_id, [{"type": "text", "text": "hi"}]
            )
            [e async for e in stream.follow(turn_id)]
    finally:
        await client.close()


async def test_request_timeout(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_HANG="usage/read")
    try:
        session_id = (await client.start_session())["sessionId"]
        with pytest.raises(TimeoutError, match="usage/read"):
            await client.read_usage(session_id, timeout=0.2)
    finally:
        await client.close()


async def test_request_timeout_clears_pending(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_HANG="usage/read")
    try:
        for _ in range(3):
            with pytest.raises(TimeoutError, match="usage/read"):
                await client.read_usage("s", timeout=0.02)
            assert client._pending == {}
    finally:
        await client.close()


async def test_request_cancellation_clears_pending(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_HANG="usage/read")
    request = asyncio.create_task(client.read_usage("s"))
    try:
        async with asyncio.timeout(2):
            while not _method_frames(tmp_path, "usage/read"):
                await asyncio.sleep(0.01)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert client._pending == {}
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await client.close()


async def test_serialization_failure_clears_pending(tmp_path: Path) -> None:
    client = await _spawn(tmp_path)
    try:
        with pytest.raises(MspProtocolError, match="JSON-encodable"):
            await client.request("usage/read", {"invalid": object()})
        assert client._pending == {}
    finally:
        await client.close()


async def test_late_response_after_timeout_is_ignored(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_HANG="usage/read")
    try:
        with pytest.raises(TimeoutError):
            await client.read_usage("s", timeout=0.02)
        [request] = _method_frames(tmp_path, "usage/read")
        frame = {"jsonrpc": "2.0", "id": request["id"], "result": {}}
        client._route_frame(frame, json.dumps(frame))
        assert client._pending == {}
        assert not client.closed
        assert (await client.start_session())["sessionId"] == "sess-1"
    finally:
        await client.close()


async def test_eof_clears_pending(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_SCENARIO="die_on_turn")
    try:
        with pytest.raises(MspConnectionClosed):
            await client.send_turn("s", [{"type": "text", "text": "hi"}])
        assert client._pending == {}
    finally:
        await client.close()


@pytest.mark.parametrize("close_client", [False, True])
async def test_pending_request_reports_closed_when_writer_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close_client: bool
) -> None:
    client = await _spawn(tmp_path)
    await client.flush()
    entered = asyncio.Event()

    async def blocked_write(encoded: bytes) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_write_once", blocked_write)
    request = asyncio.create_task(client.read_usage("s"))
    queued = asyncio.create_task(client.read_usage("s"))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        if close_client:
            await client.close()
        else:
            client._writer_task.cancel()
            await asyncio.gather(client._writer_task, return_exceptions=True)
        for task in (request, queued):
            with pytest.raises(MspConnectionClosed):
                await task
        assert client.closed
        assert client._pending == {}
        await client.flush(timeout=0.2)
    finally:
        for task in (request, queued):
            task.cancel()
        await asyncio.gather(request, queued, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize(
    "exit_mode", ["cancel", "yield", "complete", "eof", "both_ready"]
)
async def test_follow_joins_helpers_on_every_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_mode: str
) -> None:
    client = await _spawn(tmp_path)
    helpers: list[asyncio.Task[Any]] = []
    ready = asyncio.Event()
    real_wait = asyncio.wait

    async def record_wait(
        tasks: Iterable[asyncio.Task[Any]], *, return_when: str
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
        pair = tuple(tasks)
        helpers.extend(pair)
        ready.set()
        return await real_wait(pair, return_when=return_when)

    monkeypatch.setattr(asyncio, "wait", record_wait)
    with client.open_stream("s") as stream:
        iterator = stream.follow("t")
        if exit_mode == "yield":
            client._fan_out(
                "item/delta",
                {"sessionId": "s", "turnId": "t", "itemId": "i", "delta": "hello"},
            )
        elif exit_mode in {"complete", "both_ready"}:
            client._fan_out("turn/completed", {"sessionId": "s", "turnId": "t"})
            if exit_mode == "both_ready":
                client._finish(MspConnectionClosed("test EOF"))
        consumer = asyncio.create_task(anext(iterator))
        try:
            async with asyncio.timeout(2):
                await ready.wait()
                if exit_mode == "cancel":
                    consumer.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await consumer
                elif exit_mode == "eof":
                    client._proc.kill()
                    with pytest.raises(MspConnectionClosed):
                        await consumer
                else:
                    event = await consumer
                    expected = (
                        MspTextDelta if exit_mode == "yield" else MspTurnCompleted
                    )
                    assert isinstance(event, expected)
                assert len(helpers) == 2
                assert all(task.done() for task in helpers)
                if exit_mode in {"complete", "both_ready"}:
                    with pytest.raises(StopAsyncIteration):
                        await anext(iterator)
                await iterator.aclose()
        finally:
            consumer.cancel()
            for task in helpers:
                task.cancel()
            await asyncio.gather(consumer, *helpers, return_exceptions=True)
            await iterator.aclose()
            await client.close()


async def test_close_reaps_child(tmp_path: Path) -> None:
    client = await _spawn(tmp_path)
    assert client._proc is not None and client._proc.returncode is None
    await client.close()
    assert client.closed
    assert client._proc.returncode is not None
    await client.close()  # idempotent


async def test_banner_line_tolerated(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_SCENARIO="banner")
    try:
        assert client.host_version == "0.0.0-fake"
    finally:
        await client.close()


async def test_subscribers_all_see_notifications(tmp_path: Path) -> None:
    client = await _spawn(tmp_path)
    try:
        seen_a: list[str] = []
        seen_b: list[str] = []
        unsub_a = client.subscribe(lambda method, params: seen_a.append(method))
        client.subscribe(lambda method, params: seen_b.append(method))
        await client.start_session()
        assert "session/started" in seen_a
        assert "session/started" in seen_b
        unsub_a()
        await client.start_session()
        assert seen_a.count("session/started") == 1
        assert seen_b.count("session/started") == 2
    finally:
        await client.close()


async def test_server_request_default_answer(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_SCENARIO="server_request")
    try:
        session_id = (await client.start_session())["sessionId"]
        assert session_id == "sess-1"
        answers: list[dict] = []
        for _ in range(200):
            answers = [
                f for f in _logged(tmp_path) if f.get("id") == 1 and "error" in f
            ]
            if answers:
                break
            await asyncio.sleep(0.01)
        assert len(answers) == 1
        assert answers[0]["error"]["code"] == -32601
    finally:
        await client.close()


@pytest.mark.parametrize("host_dies", [False, True])
async def test_close_joins_server_request_handlers(
    tmp_path: Path, host_dies: bool
) -> None:
    client = await _spawn(tmp_path)
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def handler(method: str, params: dict) -> dict:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
        return {}

    client.set_server_request_handler(handler)
    frame = {"jsonrpc": "2.0", "id": 900, "method": "host/ping", "params": {}}
    before = asyncio.all_tasks()
    client._route_frame(frame, json.dumps(frame))
    [handler_task] = asyncio.all_tasks() - before
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        if host_dies:
            client._proc.kill()
            await client.wait_closed()
        await client.close()
        assert exited.is_set()
        assert handler_task.done()
        assert client._server_request_tasks == set()
    finally:
        handler_task.cancel()
        await asyncio.gather(handler_task, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize("response", ["success", "msp_error", "exception"])
async def test_server_request_handler_response_and_self_eviction(
    tmp_path: Path, response: str
) -> None:
    client = await _spawn(tmp_path)

    async def handler(method: str, params: dict) -> dict:
        assert (method, params) == ("host/ping", {"value": 1})
        if response == "msp_error":
            raise MspError(-32000, "denied", kind="denied", data={"retryable": False})
        if response == "exception":
            raise ValueError("handler failed")
        return {"ok": True}

    client.set_server_request_handler(handler)
    frame = {"jsonrpc": "2.0", "id": 901, "method": "host/ping", "params": {"value": 1}}
    try:
        client._route_frame(frame, json.dumps(frame))
        tasks = tuple(client._server_request_tasks)
        assert len(tasks) == 1
        await asyncio.gather(*tasks)
        assert client._server_request_tasks == set()
        await client.flush()
        async with asyncio.timeout(2):
            while not (answers := [f for f in _logged(tmp_path) if f.get("id") == 901]):
                await asyncio.sleep(0.01)
        [answer] = answers
        if response == "success":
            assert answer["result"] == {"ok": True}
        elif response == "msp_error":
            assert answer["error"]["code"] == -32000
            assert answer["error"]["data"] == {"kind": "denied", "retryable": False}
        else:
            assert answer["error"]["code"] == -32603
            assert answer["error"]["message"] == "handler failed"
    finally:
        await client.close()


async def test_server_request_handler_can_close_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await _spawn(tmp_path)
    returned = asyncio.Event()
    real_gather = asyncio.gather

    async def reject_self_join(*tasks, return_exceptions=False):
        # Catch the invalid dependency before asyncio recursively cancels it.
        assert asyncio.current_task() not in tasks, "close must not join its caller"
        return await real_gather(*tasks, return_exceptions=return_exceptions)

    monkeypatch.setattr(asyncio, "gather", reject_self_join)

    async def handler(method: str, params: dict) -> dict:
        await client.close()
        returned.set()
        return {}

    client.set_server_request_handler(handler)
    frame = {"jsonrpc": "2.0", "id": 902, "method": "host/close", "params": {}}
    client._route_frame(frame, json.dumps(frame))
    [task] = client._server_request_tasks
    try:
        async with asyncio.timeout(2):
            await real_gather(task, return_exceptions=True)
        assert returned.is_set()
        assert client._proc.returncode is not None
        assert client._server_request_tasks == set()
    finally:
        task.cancel()
        await real_gather(task, return_exceptions=True)
        await client.close()
        await client._proc.wait()
        await real_gather(
            client._reader_task,
            client._stderr_task,
            client._writer_task,
            return_exceptions=True,
        )


async def test_close_awaits_async_handler_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await _spawn(tmp_path)
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()
    release = asyncio.Event()
    joining = asyncio.Event()
    real_gather = asyncio.gather

    async def observe_join(*tasks, return_exceptions=False):
        joining.set()
        return await real_gather(*tasks, return_exceptions=return_exceptions)

    monkeypatch.setattr(asyncio, "gather", observe_join)

    async def handler(method: str, params: dict) -> dict:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release.wait()
            cleanup_finished.set()
        return {}

    client.set_server_request_handler(handler)
    frame = {"jsonrpc": "2.0", "id": 903, "method": "host/ping", "params": {}}
    client._route_frame(frame, json.dumps(frame))
    [handler_task] = client._server_request_tasks
    async with asyncio.timeout(2):
        await entered.wait()
    closing = asyncio.create_task(client.close())
    try:
        async with asyncio.timeout(2):
            await cleanup_started.wait()
            await joining.wait()
            release.set()
            await closing
        assert cleanup_finished.is_set()
        assert handler_task.done()
        assert handler_task.cancelling() == 1
    finally:
        release.set()
        await real_gather(closing, handler_task, return_exceptions=True)
        await client.close()


async def test_approval_requested_event_shape() -> None:
    # Pure translation check (no host needed): unknown shapes are dropped,
    # known ones surface the approval id.
    translate = MspClient._translate_notification
    assert (
        translate("s", "t", "approval/requested", {"sessionId": "s", "turnId": "t"})
        is None
    )
    event = translate(
        "s",
        "t",
        "approval/requested",
        {"sessionId": "s", "turnId": "t", "approval": {"approvalId": "a-1"}},
    )
    assert isinstance(event, MspApprovalRequested)
    assert event.approval_id == "a-1"


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("item/delta", {"itemId": "i", "delta": "hello"}),
        ("session/tokenUsage", {"totalTokens": 3}),
        ("approval/requested", {"approvalId": "a"}),
        ("turn/completed", {}),
        ("turn/retracted", {}),
    ],
)
@pytest.mark.parametrize(
    "scope", ["matching", "foreign_session", "foreign_turn", "missing_turn"]
)
def test_translation_checks_event_scope(
    method: str, payload: dict[str, Any], scope: str
) -> None:
    params = {"sessionId": "s", "turnId": "t", **payload}
    if scope == "foreign_session":
        params["sessionId"] = "other"
    elif scope == "foreign_turn":
        params["turnId"] = "other"
    elif scope == "missing_turn":
        params.pop("turnId")
    event = MspClient._translate_notification("s", "t", method, params)
    assert (event is not None) == (scope == "matching")


async def test_follow_correlates_items_and_rejects_other_turns(tmp_path: Path) -> None:
    client = await _spawn(tmp_path)
    original_deltas: list[dict[str, Any]] = []
    client.subscribe(
        lambda method, params: (
            original_deltas.append(params) if method == "item/delta" else None
        )
    )
    try:
        with client.open_stream("s") as first, client.open_stream("s") as second:
            for turn, item in [("foreign", "i-other"), ("t", "i-own")]:
                client._fan_out(
                    "item/started",
                    {"sessionId": "s", "item": {"itemId": item, "turnId": turn}},
                )
                client._fan_out(
                    "item/delta", {"sessionId": "s", "itemId": item, "delta": turn}
                )
                client._fan_out(
                    "session/tokenUsage",
                    {"sessionId": "s", "turnId": turn, "totalTokens": 3},
                )
                client._fan_out(
                    "approval/requested",
                    {"sessionId": "s", "turnId": turn, "approvalId": turn},
                )
            for turn in ("foreign", "t"):
                client._fan_out("turn/completed", {"sessionId": "s", "turnId": turn})

            async def collect(stream, turn: str):
                return [event async for event in stream.follow(turn)]

            async with asyncio.timeout(2):
                streams = await asyncio.gather(
                    collect(first, "t"), collect(second, "foreign")
                )
            for turn, events in zip(("t", "foreign"), streams, strict=True):
                assert [e.delta for e in events if isinstance(e, MspTextDelta)] == [
                    turn
                ]
                assert len([e for e in events if isinstance(e, MspTokenUsage)]) == 1
                assert [
                    e.approval_id for e in events if isinstance(e, MspApprovalRequested)
                ] == [turn]
                assert [
                    e.turn_id for e in events if isinstance(e, MspTurnCompleted)
                ] == [turn]
        assert all("turnId" not in params for params in original_deltas)
    finally:
        await client.close()


@pytest.mark.parametrize("item_state", ["unknown", "completed", "foreign_session"])
async def test_follow_drops_unassociated_item_deltas(
    tmp_path: Path, item_state: str
) -> None:
    client = await _spawn(tmp_path)
    try:
        with client.open_stream("s") as stream:
            item = {"itemId": "i", "turnId": "t"}
            if item_state != "unknown":
                client._fan_out(
                    "item/started",
                    {
                        "sessionId": "other"
                        if item_state == "foreign_session"
                        else "s",
                        "item": item,
                    },
                )
            if item_state == "completed":
                client._fan_out("item/completed", {"sessionId": "s", "item": item})
            client._fan_out(
                "item/delta", {"sessionId": "s", "itemId": "i", "delta": "drop"}
            )
            client._fan_out("turn/completed", {"sessionId": "s", "turnId": "t"})
            async with asyncio.timeout(2):
                events = [event async for event in stream.follow("t")]
            assert [type(event) for event in events] == [MspTurnCompleted]
    finally:
        await client.close()


def test_mint_command_id_is_uuid7() -> None:
    first, second = mint_command_id(), mint_command_id()
    pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    assert pattern.match(first)
    assert pattern.match(second)
    assert first != second
