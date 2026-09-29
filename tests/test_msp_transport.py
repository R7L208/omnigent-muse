"""Integration tests for the executor-facing MSP adapter."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from omnigent.community.harness.muse.inner.msp_client import (
    MspApprovalRequested,
    MspClient,
    MspItemUpdate,
    MspTurnCompleted,
)
from omnigent.community.harness.muse.inner.msp_transport import MspTransport
from omnigent.community.harness.muse.inner.muse_executor import (
    MuseApprovalRequested,
    MuseTextDelta,
    MuseToolCall,
    MuseTurnFinished,
    MuseTurnStarted,
)

FAKE_HOST = Path(__file__).parent / "fixtures" / "fake_msp_host.py"


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
            "promptTokens": 10,
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
