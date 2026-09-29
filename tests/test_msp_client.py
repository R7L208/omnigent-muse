"""Hermetic tests for the vendored MSP client.

Every test drives :class:`MspClient` against
``fixtures/fake_msp_host.py`` over real stdio pipes — no `muse` binary,
credentials, or network needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from pathlib import Path

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
    env = dict(os.environ)
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
        with client.open_stream(session_id) as stream:
            with pytest.raises(MspConnectionClosed):
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


# ---------------------------------------------------------------------------
# Regression tests for two bugs found by code review in PR #1's msp_client.py.
# They are xfail today because the fixes live on PR #1's branch; once that
# author lands the fix, each test flips to xpass — remove the marker then.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="PR #1 finding #4: request() timeout never pops the pending future "
    "from self._pending, leaking an entry per timed-out request. Remove this "
    "marker once the fix lands.",
    strict=False,
)
async def test_request_timeout_clears_pending(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_HANG="usage/read")
    try:
        session_id = (await client.start_session())["sessionId"]
        with pytest.raises(TimeoutError, match="usage/read"):
            await client.read_usage(session_id, timeout=0.2)
        # The completed start_session request was popped by the reader; the
        # timed-out request must be popped too, or a long-lived client grows
        # self._pending without bound.
        assert client._pending == {}
    finally:
        await client.close()


@pytest.mark.xfail(
    reason="PR #1 finding #5: cancelling the writer mid-drain sets CancelledError "
    "on the in-flight request's future, so the caller sees CancelledError instead "
    "of MspConnectionClosed. Remove this marker once the fix lands.",
    strict=False,
)
async def test_pending_request_reports_closed_when_writer_cancelled(
    tmp_path: Path,
) -> None:
    client = await _spawn(tmp_path)
    assert client._proc is not None
    real_stdin = client._proc.stdin
    try:
        # Wedge the in-flight write inside drain() so the writer task is parked
        # on our request's frame when teardown cancels it.
        class _BlockingStdin:
            def write(self, data: bytes) -> None: ...

            async def drain(self) -> None:
                await asyncio.Event().wait()

            def close(self) -> None: ...

        # Drain the handshake's queued frames first so the writer parks on our
        # request below, not on a leftover future-less notification.
        await client.flush()
        client._proc.stdin = _BlockingStdin()  # type: ignore[assignment]

        pending = asyncio.ensure_future(
            client.request("usage/read", {"sessionId": "s"}, timeout=5)
        )
        # Let the request enqueue its frame and the writer park in drain().
        await asyncio.sleep(0.05)

        # Teardown cancels the writer while our write is wedged in drain().
        client._writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await client._writer_task

        # The caller should learn the connection is closing, not inherit the
        # writer's own cancellation.
        with pytest.raises(MspConnectionClosed):
            await pending
    finally:
        client._proc.stdin = real_stdin
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


async def test_approval_requested_event_shape() -> None:
    # Pure translation check (no host needed): unknown shapes are dropped,
    # known ones surface the approval id.
    translate = MspClient._translate_notification
    assert translate("s", "t", "approval/requested", {"sessionId": "s"}) is None
    event = translate(
        "s",
        "t",
        "approval/requested",
        {"sessionId": "s", "approval": {"approvalId": "a-1"}},
    )
    assert isinstance(event, MspApprovalRequested)
    assert event.approval_id == "a-1"


def test_mint_command_id_is_uuid7() -> None:
    first, second = mint_command_id(), mint_command_id()
    pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    assert pattern.match(first)
    assert pattern.match(second)
    assert first != second
