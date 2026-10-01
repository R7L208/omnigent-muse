"""Integration tests for the executor-facing MSP adapter."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Self, cast

import pytest

from omnigent.community.harness.muse.inner.msp_client import (
    MspApprovalRequested,
    MspClient,
    MspConnectionClosed,
    MspError,
    MspItemUpdate,
    MspProtocolError,
    MspTextDelta,
    MspTokenUsage,
    MspTurnCompleted,
)
from omnigent.community.harness.muse.inner.msp_transport import MspTransport, _spawn_env
from omnigent.community.harness.muse.inner.muse_executor import (
    MuseApprovalRequested,
    MuseReasoningDelta,
    MuseTextDelta,
    MuseToolCall,
    MuseTransportError,
    MuseTurnFinished,
    MuseTurnStarted,
)

FAKE_HOST = Path(__file__).parent / "fixtures" / "fake_msp_host.py"


class _ScriptedStream:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass

    async def follow(self, turn_id: str) -> AsyncIterator[object]:
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event


class _ScriptedClient:
    def __init__(self, events: list[object] | None = None) -> None:
        self.events = events or []
        self.approval_calls: list[dict[str, Any]] = []
        self.approval_error: Exception | None = None
        self.turn_error: Exception | None = None

    def open_stream(self, session_id: str) -> _ScriptedStream:
        return _ScriptedStream(self.events)

    async def send_turn(self, *args: object, **kwargs: object) -> str:
        if self.turn_error is not None:
            raise self.turn_error
        return "turn-1"

    async def decide_approval(
        self,
        session_id: str,
        approval_id: str,
        choice_id: str,
        requirement_id: dict[str, Any],
    ) -> None:
        self.approval_calls.append(dict(requirement_id))
        if self.approval_error is not None:
            error, self.approval_error = self.approval_error, None
            raise error


def _scripted_transport(events: list[object]) -> MspTransport:
    return MspTransport(cast(Any, _ScriptedClient(events)))


async def _transport(tmp_path: Path) -> MspTransport:
    env = dict(os.environ)
    env["FAKE_MSP_LOG"] = str(tmp_path / "fake.log")
    client = await MspClient.spawn([sys.executable, "-u", str(FAKE_HOST)], env=env)
    return MspTransport(client)


def _frames(tmp_path: Path, method: str) -> list[dict]:
    return [
        frame
        for line in (tmp_path / "fake.log").read_text().splitlines()
        if (frame := json.loads(line)).get("method") == method
    ]


def test_spawn_environment_is_deny_by_default() -> None:
    env = _spawn_env(
        {
            "HOME": "/home/muse",
            "PATH": "/usr/bin",
            "HTTPS_PROXY": "http://proxy.test",
            "XDG_CONFIG_HOME": "/config",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "OPENAI_API_KEY": "secret",
            "OMNIGENT_INTERNAL_TOKEN": "secret",
            "PYTHONPATH": "/untrusted",
        }
    )

    assert env == {
        "HOME": "/home/muse",
        "PATH": "/usr/bin",
        "HTTPS_PROXY": "http://proxy.test",
        "XDG_CONFIG_HOME": "/config",
    }


async def test_adapter_runs_complete_turn(tmp_path: Path) -> None:
    transport = await _transport(tmp_path)
    try:
        session_id = await transport.start_session(
            workspace_root="/tmp/workspace",
            model="fake-model",
            approval_mode="onRequest",
        )
        events = [
            event
            async for event in transport.run_turn(
                session_id, text="hello", reasoning_effort="high"
            )
        ]
        assert isinstance(events[0], MuseTurnStarted)
        assert (
            "".join(event.text for event in events if isinstance(event, MuseTextDelta))
            == "Hello, world"
        )
        tool_events = [event for event in events if isinstance(event, MuseToolCall)]
        assert [event.state for event in tool_events] == ["inProgress", "completed"]
        assert tool_events[-1].output == "/workspace"
        finished = next(
            event for event in events if isinstance(event, MuseTurnFinished)
        )
        assert finished.state == "completed"
        assert finished.usage == {
            "inputTokens": 10,
            "outputTokens": 5,
            "totalTokens": 15,
        }
        [start] = _frames(tmp_path, "session/start")
        assert start["params"]["workspaceRoot"] == "/tmp/workspace"
        assert start["params"]["modelId"] == "fake-model"
        [turn] = _frames(tmp_path, "turn/start")
        assert turn["params"]["reasoningEffort"] == "high"
    finally:
        await transport.close()


async def test_adapter_preserves_approval_requirement_id(tmp_path: Path) -> None:
    transport = await _transport(tmp_path)
    try:
        event = transport._approval(
            MspApprovalRequested(
                session_id="sess-1",
                approval_id="approval-1",
                raw={
                    "currentRequirementId": {
                        "approvalId": "approval-1",
                        "sourceIndex": 0,
                    },
                    "toolName": "shell",
                    "arguments": {"command": "pwd"},
                    "availableChoices": [
                        {
                            "choiceId": "allow_session",
                            "label": "Allow",
                        },
                        {
                            "choiceId": "abort",
                            "label": "Deny",
                        },
                    ],
                },
            )
        )
        assert isinstance(event, MuseApprovalRequested)
        assert event.tool_name == "shell"
        assert event.arguments == {"command": "pwd"}
        assert [choice.decision for choice in event.choices] == ["allow", "deny"]

        await transport.decide_approval("sess-1", "approval-1", "allow_session")
        [decision] = _frames(tmp_path, "approval/decide")
        assert decision["params"]["requirementId"] == {
            "approvalId": "approval-1",
            "sourceIndex": 0,
        }
    finally:
        await transport.close()


async def test_adapter_interrupt_reports_host_acceptance(tmp_path: Path) -> None:
    transport = await _transport(tmp_path)
    try:
        assert await transport.interrupt_turn("sess-1", "turn-1") is True
    finally:
        await transport.close()


def test_adapter_translates_tool_lifecycle() -> None:
    transport = MspTransport()
    started = transport._item(
        MspItemUpdate(
            "started",
            {
                "itemId": "item-1",
                "callId": "call-1",
                "kind": "toolCall",
                "status": "inProgress",
                "tool": "shell",
                "args": '{"command":"pwd"}',
            },
        )
    )
    completed = transport._item(
        MspItemUpdate(
            "completed",
            {
                "itemId": "item-1",
                "callId": "call-1",
                "kind": "toolCall",
                "status": "completed",
                "tool": "shell",
                "args": '{"command":"pwd"}',
                "visibleOutput": "/workspace",
            },
        )
    )
    assert isinstance(started, MuseToolCall) and started.state == "inProgress"
    assert isinstance(completed, MuseToolCall) and completed.output == "/workspace"


def test_client_preserves_turn_terminal_state() -> None:
    event = MspClient._translate_notification(
        "session-1",
        "turn-1",
        "turn/completed",
        {
            "sessionId": "session-1",
            "turnId": "turn-1",
            "terminal": "cancelled",
            "reason": "interrupted",
        },
    )
    assert isinstance(event, MspTurnCompleted)
    assert event.terminal == "cancelled"
    assert event.reason == "interrupted"


@pytest.mark.parametrize(
    ("completed", "expected_state", "expected_error"),
    [
        (MspTurnCompleted("s", "turn-1", terminal="canceled"), "cancelled", None),
        (MspTurnCompleted("s", "turn-1", terminal="retracted"), "cancelled", None),
        (
            MspTurnCompleted(
                "s",
                "turn-1",
                error_kind="provider_error",
                error_message="provider failed",
                error_retryable=True,
            ),
            "failed",
            "provider failed",
        ),
    ],
)
async def test_adapter_translates_terminal_states(
    completed: MspTurnCompleted, expected_state: str, expected_error: str | None
) -> None:
    transport = _scripted_transport(
        [MspTokenUsage("s", prompt_tokens=3, output_tokens=2), completed]
    )
    events = [
        event
        async for event in transport.run_turn("s", text="hello", reasoning_effort=None)
    ]
    finished = next(event for event in events if isinstance(event, MuseTurnFinished))
    assert finished.state == expected_state
    assert finished.error == expected_error
    assert finished.usage == {"inputTokens": 3, "outputTokens": 2}
    assert finished.retryable is (completed.error_retryable is True)


async def test_adapter_classifies_reasoning_by_item_kind_and_summary_field() -> None:
    transport = _scripted_transport(
        [
            MspItemUpdate("started", {"itemId": "reason-1", "kind": "reasoning"}),
            MspTextDelta("thought", "reason-1"),
            MspTextDelta("summary", "message-1", "summary.text"),
            MspTextDelta("answer", "message-1", "text"),
            MspTurnCompleted("s", "turn-1"),
        ]
    )
    events = [
        event
        async for event in transport.run_turn(
            "s", text="hello", reasoning_effort="high"
        )
    ]
    assert [
        event.text for event in events if isinstance(event, MuseReasoningDelta)
    ] == [
        "thought",
        "summary",
    ]
    assert [event.text for event in events if isinstance(event, MuseTextDelta)] == [
        "answer"
    ]


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (MspConnectionClosed("closed"), True),
        (MspProtocolError("bad frame"), False),
        (MspError(-32000, "busy", data={"retryable": True}), True),
        (MspError(-32000, "rejected", data={"retryable": False}), False),
    ],
)
async def test_adapter_translates_stream_errors(
    error: Exception, retryable: bool
) -> None:
    transport = _scripted_transport([error])
    with pytest.raises(MuseTransportError) as caught:
        _ = [
            event
            async for event in transport.run_turn(
                "s", text="hello", reasoning_effort=None
            )
        ]
    assert str(caught.value) == str(error)
    assert caught.value.retryable is retryable
    assert caught.value.preserve_session is False


async def test_rejected_turn_start_preserves_healthy_session() -> None:
    client = _ScriptedClient()
    client.turn_error = MspError(-32000, "busy", data={"retryable": True})
    transport = MspTransport(cast(Any, client))

    with pytest.raises(MuseTransportError) as caught:
        _ = [
            event
            async for event in transport.run_turn(
                "s", text="hello", reasoning_effort=None
            )
        ]

    assert caught.value.retryable is True
    assert caught.value.preserve_session is True


async def test_approval_requirement_is_preserved_after_failed_decision() -> None:
    client = _ScriptedClient()
    transport = MspTransport(cast(Any, client))
    requirement_id = {"approvalId": "approval-1", "sourceIndex": 7}
    transport._approval(
        MspApprovalRequested(
            "s", "approval-1", raw={"currentRequirementId": requirement_id}
        )
    )
    client.approval_error = MspConnectionClosed("temporary failure")

    with pytest.raises(MuseTransportError):
        await transport.decide_approval("s", "approval-1", "allow")
    await transport.decide_approval("s", "approval-1", "allow")

    assert client.approval_calls == [requirement_id, requirement_id]
    assert ("s", "approval-1") not in transport._approval_requirements
