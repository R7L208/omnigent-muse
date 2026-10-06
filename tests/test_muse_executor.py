from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnCancelled,
    TurnComplete,
)

from omnigent.community.harness.muse.inner.muse_executor import (
    MuseApprovalChoice,
    MuseApprovalRequested,
    MuseEvent,
    MuseExecutor,
    MuseReasoningDelta,
    MuseTextDelta,
    MuseToolCall,
    MuseTransportError,
    MuseTurnFinished,
    MuseTurnStarted,
)


class FakeTransport:
    def __init__(self, events: list[MuseEvent | BaseException] | None = None) -> None:
        self.events = events or []
        self.starts: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self.decisions: list[tuple[str, str, str]] = []
        self.interrupts: list[tuple[str, str | None]] = []
        self.closed = False
        self.active_provider: str | None = None

    async def start_session(self, **kwargs: Any) -> str:
        self.starts.append(kwargs)
        return "session-1"

    async def run_turn(
        self,
        session_id: str,
        *,
        text: str,
        reasoning_effort: str | None,
    ) -> AsyncIterator[MuseEvent]:
        self.turns.append(
            {
                "session_id": session_id,
                "text": text,
                "reasoning_effort": reasoning_effort,
            }
        )
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def decide_approval(
        self, session_id: str, approval_id: str, choice_id: str
    ) -> None:
        self.decisions.append((session_id, approval_id, choice_id))

    async def interrupt_turn(self, session_id: str, turn_id: str | None) -> bool:
        self.interrupts.append((session_id, turn_id))
        return True

    async def close(self) -> None:
        self.closed = True


async def collect(executor: MuseExecutor, **kwargs: Any) -> list[object]:
    defaults = {
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [],
        "system_prompt": "Follow the project instructions.",
    }
    defaults.update(kwargs)
    return [event async for event in executor.run_turn(**defaults)]


async def test_translates_streaming_text_reasoning_tools_and_usage() -> None:
    transport = FakeTransport(
        [
            MuseTurnStarted("turn-1"),
            MuseTextDelta("Hello "),
            MuseReasoningDelta("checking"),
            MuseToolCall("call-1", "shell", {"command": "pwd"}, "started"),
            MuseToolCall("call-1", "shell", {}, "completed", output="/tmp"),
            MuseTextDelta("world"),
            MuseTurnFinished(
                "turn-1",
                "completed",
                usage={"inputTokens": 4, "outputTokens": 2, "cachedTokens": 1},
            ),
        ]
    )
    events = await collect(MuseExecutor(lambda: transport, model="muse-large"))

    assert events == [
        TextChunk("Hello "),
        ReasoningChunk("checking", "reasoning_text"),
        ToolCallRequest(
            "shell",
            {"command": "pwd"},
            metadata={"call_id": "call-1", "internally_executed": True},
        ),
        ToolCallComplete(
            "shell",
            ToolCallStatus.SUCCESS,
            result="/tmp",
            metadata={"call_id": "call-1"},
        ),
        TextChunk("world"),
        TurnComplete(
            response="Hello world",
            usage={
                "input_tokens": 4,
                "output_tokens": 2,
                "total_tokens": 6,
                "cache_read_input_tokens": 1,
            },
        ),
    ]


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("failed", ToolCallStatus.ERROR),
        ("rejected", ToolCallStatus.BLOCKED),
        ("cancelled", ToolCallStatus.CANCELLED),
        ("timedOut", ToolCallStatus.ERROR),
    ],
)
async def test_maps_tool_terminal_states(state: str, status: ToolCallStatus) -> None:
    transport = FakeTransport(
        [
            MuseToolCall("c", "edit", {}, state, error="nope"),
            MuseTurnFinished("t", "completed"),
        ]
    )
    events = await collect(MuseExecutor(lambda: transport))
    complete = next(event for event in events if isinstance(event, ToolCallComplete))
    assert complete.status is status
    assert complete.error == "nope"


async def test_starts_once_injects_system_prompt_once_and_uses_latest_user_message() -> (
    None
):
    transport = FakeTransport([MuseTurnFinished("turn-1", "completed")])
    executor = MuseExecutor(lambda: transport, cwd="/workspace", model="model-a")

    await collect(executor)
    await collect(
        executor,
        messages=[
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "new"},
        ],
        system_prompt="Do not repeat me.",
    )

    assert transport.starts == [
        {
            "workspace_root": "/workspace",
            "model": "model-a",
            "approval_mode": "onRequest",
        }
    ]
    assert transport.turns[0]["text"] == "Follow the project instructions.\n\nhello"
    assert transport.turns[1]["text"] == "new"


async def test_rejects_conflicting_per_turn_model_before_sending_prompt() -> None:
    transport = FakeTransport()
    events = await collect(
        MuseExecutor(lambda: transport, model="model-a"),
        config=ExecutorConfig(model="model-b"),
    )
    [error] = events
    assert isinstance(error, ExecutorError)
    assert "model-b" in error.message and "model-a" in error.message
    assert error.preserve_session is True
    assert transport.turns == []


async def test_rejects_invalid_per_turn_reasoning_effort_before_sending_prompt() -> (
    None
):
    transport = FakeTransport()
    events = await collect(
        MuseExecutor(lambda: transport),
        config=ExecutorConfig(extra={"reasoning_effort": "extreme"}),
    )

    [error] = events
    assert isinstance(error, ExecutorError)
    assert "reasoning_effort" in error.message
    assert "extreme" in error.message
    assert error.preserve_session is True
    assert transport.starts == []
    assert transport.turns == []


async def test_forwards_minimal_per_turn_reasoning_effort() -> None:
    transport = FakeTransport([MuseTurnFinished("turn-1", "completed")])

    await collect(
        MuseExecutor(lambda: transport),
        config=ExecutorConfig(extra={"reasoning_effort": "minimal"}),
    )

    assert transport.turns[0]["reasoning_effort"] == "minimal"


async def test_does_not_silently_apply_model_to_existing_default_session() -> None:
    transport = FakeTransport([MuseTurnFinished("turn-1", "completed")])
    executor = MuseExecutor(lambda: transport)
    await collect(executor)
    events = await collect(executor, config=ExecutorConfig(model="model-late"))
    assert len(transport.turns) == 1
    [error] = events
    assert isinstance(error, ExecutorError)
    assert "model-late" in error.message
    assert error.preserve_session is True


async def test_rejects_attachments_instead_of_silently_discarding_them() -> None:
    transport = FakeTransport()
    events = await collect(
        MuseExecutor(lambda: transport),
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "describe this"},
                    {"type": "input_image", "image_url": "data:image/png;base64,..."},
                    {"type": "file", "file_id": "file-1"},
                ],
            }
        ],
    )

    assert events == [
        ExecutorError(
            "Muse attachment forwarding is not implemented; unsupported "
            "content types: file, input_image",
            preserve_session=True,
        )
    ]
    assert transport.starts == []
    assert transport.turns == []


@dataclass
class Verdict:
    action: str


async def test_policy_deny_resolves_approval_without_prompting() -> None:
    approval = MuseApprovalRequested(
        "approval-1",
        "shell",
        {"command": "rm file"},
        choices=(
            MuseApprovalChoice("allow-once", "Allow", "allow"),
            MuseApprovalChoice("deny", "Deny", "deny"),
        ),
    )
    transport = FakeTransport([approval, MuseTurnFinished("turn-1", "completed")])
    executor = MuseExecutor(lambda: transport)

    async def policy(phase: str, data: dict[str, Any]) -> Verdict:
        assert phase == "PHASE_TOOL_CALL"
        assert data == {"name": "shell", "arguments": {"command": "rm file"}}
        return Verdict("POLICY_ACTION_DENY")

    executor._policy_evaluator = policy
    await collect(executor)
    assert transport.decisions == [("session-1", "approval-1", "deny")]


async def test_policy_ask_uses_choice_elicitation_and_exact_offered_choice() -> None:
    approval = MuseApprovalRequested(
        "approval-1",
        "shell",
        {},
        choices=(
            MuseApprovalChoice("once", "Allow once", "allow"),
            MuseApprovalChoice("never", "Reject", "deny"),
        ),
    )
    transport = FakeTransport([approval, MuseTurnFinished("turn-1", "completed")])
    executor = MuseExecutor(lambda: transport)

    async def policy(*args: Any) -> Verdict:
        return Verdict("POLICY_ACTION_ASK")

    async def choose(name: str, args: dict[str, Any], labels: Sequence[str]) -> str:
        assert list(labels) == ["Allow once", "Reject"]
        return "Allow once"

    executor._policy_evaluator = policy
    executor._elicitation_choice_handler = choose
    await collect(executor)
    assert transport.decisions == [("session-1", "approval-1", "once")]


async def test_ask_without_elicitation_fails_closed() -> None:
    approval = MuseApprovalRequested(
        "approval-1",
        "shell",
        {},
        choices=(MuseApprovalChoice("deny", "Deny", "deny"),),
    )
    transport = FakeTransport([approval, MuseTurnFinished("turn-1", "completed")])
    executor = MuseExecutor(lambda: transport)

    async def policy(*args: Any) -> Verdict:
        return Verdict("POLICY_ACTION_ASK")

    executor._policy_evaluator = policy
    await collect(executor)
    assert transport.decisions == [("session-1", "approval-1", "deny")]


async def test_standalone_without_policy_or_elicitation_fails_closed() -> None:
    approval = MuseApprovalRequested(
        "approval-1",
        "shell",
        {},
        choices=(
            MuseApprovalChoice("once", "Allow once", "allow"),
            MuseApprovalChoice("never", "Deny", "deny"),
        ),
    )
    transport = FakeTransport([approval, MuseTurnFinished("turn-1", "completed")])
    executor = MuseExecutor(lambda: transport)

    await collect(executor)

    assert transport.decisions == [("session-1", "approval-1", "never")]


async def test_cancelled_and_failed_turns_are_terminal_events() -> None:
    cancelled = FakeTransport([MuseTurnFinished("t", "cancelled", error="stopped")])
    failed = FakeTransport(
        [MuseTurnFinished("t", "failed", error="host busy", retryable=True)]
    )
    assert await collect(MuseExecutor(lambda: cancelled)) == [TurnCancelled("stopped")]
    assert await collect(MuseExecutor(lambda: failed)) == [
        ExecutorError("host busy", retryable=True, preserve_session=True)
    ]


async def test_transport_failure_becomes_executor_error() -> None:
    transport = FakeTransport([MuseTransportError("host exited", retryable=True)])
    assert await collect(MuseExecutor(lambda: transport)) == [
        ExecutorError("Muse transport error: host exited", retryable=True)
    ]


async def test_stream_without_terminal_event_requires_session_teardown() -> None:
    transport = FakeTransport([MuseTextDelta("partial")])

    assert await collect(MuseExecutor(lambda: transport)) == [
        TextChunk("partial"),
        ExecutorError("Muse stream ended without a terminal turn event"),
    ]


async def test_idle_transport_failure_preserves_session() -> None:
    transport = FakeTransport(
        [MuseTransportError("turn rejected", preserve_session=True)]
    )
    assert await collect(MuseExecutor(lambda: transport)) == [
        ExecutorError("Muse transport error: turn rejected", preserve_session=True)
    ]


async def test_close_is_idempotent_and_completed_turn_is_not_interruptible() -> None:
    transport = FakeTransport(
        [
            MuseTurnStarted("turn-9"),
            MuseTextDelta("x"),
            MuseTurnFinished("turn-9", "completed"),
        ]
    )
    executor = MuseExecutor(lambda: transport)
    await collect(executor)
    assert await executor.interrupt_session("ignored") is False
    await executor.close_session("ignored")
    await executor.close()
    await executor.close()
    assert transport.interrupts == []
    assert transport.closed


async def test_interrupt_delegates_for_live_turn() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingTransport(FakeTransport):
        async def run_turn(
            self,
            session_id: str,
            *,
            text: str,
            reasoning_effort: str | None,
        ) -> AsyncIterator[MuseEvent]:
            yield MuseTurnStarted("turn-live")
            started.set()
            await release.wait()
            yield MuseTurnFinished("turn-live", "cancelled")

    transport = BlockingTransport()
    executor = MuseExecutor(lambda: transport)
    task = asyncio.create_task(collect(executor))
    await started.wait()
    assert await executor.interrupt_session("ignored") is True
    assert transport.interrupts == [("session-1", "turn-live")]
    release.set()
    await task


async def test_dead_transport_during_interrupt_clears_stale_session() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class DeadInterruptTransport(FakeTransport):
        async def run_turn(
            self,
            session_id: str,
            *,
            text: str,
            reasoning_effort: str | None,
        ) -> AsyncIterator[MuseEvent]:
            yield MuseTurnStarted("turn-live")
            started.set()
            await release.wait()
            raise MuseTransportError("host exited", retryable=True, transport_dead=True)

        async def interrupt_turn(self, session_id: str, turn_id: str | None) -> bool:
            raise MuseTransportError("host exited", retryable=True, transport_dead=True)

    transport = DeadInterruptTransport()
    executor = MuseExecutor(lambda: transport)
    task = asyncio.create_task(collect(executor))
    await started.wait()

    interrupted = await executor.interrupt_session("ignored")
    try:
        assert interrupted is False
        assert transport.closed is True
        assert executor._transport is None
        assert executor._session_id is None
        assert executor._needs_replay is True
    finally:
        release.set()
        await task


class FailFirstTurnTransport(FakeTransport):
    """Raises on the first run_turn (host dies mid-turn), succeeds afterward."""

    def __init__(self) -> None:
        super().__init__()
        self.turn_calls = 0

    async def run_turn(
        self,
        session_id: str,
        *,
        text: str,
        reasoning_effort: str | None,
    ) -> AsyncIterator[MuseEvent]:
        self.turns.append(
            {
                "session_id": session_id,
                "text": text,
                "reasoning_effort": reasoning_effort,
            }
        )
        self.turn_calls += 1
        if self.turn_calls == 1:
            raise MuseTransportError("host died mid-turn")
        yield MuseTurnFinished("turn-2", "completed")


class FailStartTransport(FakeTransport):
    """start_session fails after the factory has spawned the host."""

    async def start_session(self, **kwargs: Any) -> str:
        self.starts.append(kwargs)
        raise MuseTransportError("handshake rejected")


async def test_system_prompt_resent_after_failed_first_turn() -> None:
    transport = FailFirstTurnTransport()
    executor = MuseExecutor(lambda: transport)

    first = await collect(executor)
    assert isinstance(first[0], ExecutorError)

    await collect(executor)

    # The first turn never reached the host, so the prompt must ride along again.
    assert transport.turns[0]["text"] == "Follow the project instructions.\n\nhello"
    assert transport.turns[1]["text"] == "Follow the project instructions.\n\nhello"


async def test_transport_closed_when_start_session_fails() -> None:
    transport = FailStartTransport()
    events = await collect(MuseExecutor(lambda: transport))

    [error] = events
    assert isinstance(error, ExecutorError)
    assert "startup failed" in error.message.lower()
    assert transport.closed is True


async def test_dead_transport_is_replaced_on_next_turn() -> None:
    dead = FakeTransport(
        [MuseTransportError("host exited", retryable=True, transport_dead=True)]
    )
    replacement = FakeTransport([MuseTurnFinished("turn-2", "completed")])
    transports = iter((dead, replacement))
    executor = MuseExecutor(lambda: next(transports))

    [error] = await collect(executor)
    assert isinstance(error, ExecutorError)
    assert dead.closed is True

    [complete] = await collect(
        executor,
        messages=[
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "follow-up"},
        ],
    )
    assert isinstance(complete, TurnComplete)
    assert replacement.starts == [
        {"workspace_root": None, "model": None, "approval_mode": "onRequest"}
    ]
    replay = replacement.turns[0]["text"]
    assert replay.startswith("Follow the project instructions.\n\n")
    assert "session restarted after its transport was lost" in replay
    assert json.loads(replay.rsplit("\n\n", 1)[-1]) == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "follow-up"},
    ]


async def test_fresh_executor_replays_history_after_adapter_replacement() -> None:
    transport = FakeTransport([MuseTurnFinished("turn-1", "completed")])
    executor = MuseExecutor(lambda: transport)
    messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "retry after replacement"},
    ]

    await collect(executor, messages=messages)

    replay = transport.turns[0]["text"]
    assert "session restarted after its transport was lost" in replay
    assert json.loads(replay.rsplit("\n\n", 1)[-1]) == messages


async def test_partial_output_before_transport_death_replays_on_next_retry() -> None:
    dead = FakeTransport(
        [
            MuseTextDelta("partial answer"),
            MuseTransportError("host exited", retryable=True, transport_dead=True),
        ]
    )
    replacement = FakeTransport([MuseTurnFinished("turn-2", "completed")])
    transports = iter((dead, replacement))
    executor = MuseExecutor(lambda: next(transports))

    assert await collect(executor) == [
        TextChunk("partial answer"),
        ExecutorError("Muse transport error: host exited", retryable=True),
    ]
    assert replacement.starts == []

    await collect(
        executor,
        messages=[
            {"role": "user", "content": "original question"},
            {"role": "assistant", "content": "confirmed prior answer"},
            {"role": "user", "content": "retry this turn"},
        ],
    )
    assert (
        "session restarted after its transport was lost" in replacement.turns[0]["text"]
    )


async def test_recovery_rejects_attachments_anywhere_in_replayed_history() -> None:
    dead = FakeTransport(
        [MuseTransportError("host exited", retryable=True, transport_dead=True)]
    )
    replacement = FakeTransport()
    transports = iter((dead, replacement))
    executor = MuseExecutor(lambda: next(transports))
    await collect(executor)

    events = await collect(
        executor,
        messages=[
            {
                "role": "user",
                "content": [{"type": "input_image", "image_url": "image"}],
            },
            {"role": "assistant", "content": "prior answer"},
            {"role": "user", "content": "follow-up"},
        ],
    )

    assert events == [
        ExecutorError(
            "Muse attachment forwarding is not implemented; unsupported "
            "content types: input_image",
            preserve_session=True,
        )
    ]
    assert replacement.starts == []


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ([{"type": "input_text"}], "malformed_text"),
        ([{"type": "custom_blob", "value": "data"}], "custom_blob"),
        (["untyped block"], "unknown"),
    ],
)
async def test_rejects_malformed_and_unknown_content_blocks(
    content: list[object], kind: str
) -> None:
    transport = FakeTransport()

    [error] = await collect(
        MuseExecutor(lambda: transport),
        messages=[{"role": "user", "content": content}],
    )

    assert isinstance(error, ExecutorError)
    assert f"content types: {kind}" in error.message
    assert transport.starts == []


async def test_healthy_session_ignores_attachments_in_prior_history() -> None:
    transport = FakeTransport([MuseTurnFinished("turn-1", "completed")])
    executor = MuseExecutor(lambda: transport)
    await collect(executor)

    events = await collect(
        executor,
        messages=[
            {
                "role": "user",
                "content": [{"type": "input_image", "image_url": "already-seen"}],
            },
            {"role": "assistant", "content": "prior answer"},
            {"role": "user", "content": "follow-up"},
        ],
    )

    assert isinstance(events[-1], TurnComplete)
    assert transport.turns[-1]["text"] == "follow-up"


async def test_tool_call_request_emitted_once_across_started_and_in_progress() -> None:
    transport = FakeTransport(
        [
            MuseToolCall("call-1", "shell", {"command": "pwd"}, "started"),
            MuseToolCall("call-1", "shell", {"command": "pwd"}, "inProgress"),
            MuseToolCall("call-1", "shell", {}, "completed", output="/tmp"),
            MuseTurnFinished("turn-1", "completed"),
        ]
    )
    events = await collect(MuseExecutor(lambda: transport))

    requests = [event for event in events if isinstance(event, ToolCallRequest)]
    completes = [event for event in events if isinstance(event, ToolCallComplete)]
    assert len(requests) == 1
    assert requests[0].metadata == {
        "call_id": "call-1",
        "internally_executed": True,
    }
    assert len(completes) == 1
    assert completes[0].status is ToolCallStatus.SUCCESS


async def test_ask_without_elicitation_allows_when_no_deny_offered() -> None:
    approval = MuseApprovalRequested(
        "approval-1",
        "shell",
        {},
        choices=(MuseApprovalChoice("only", "Allow", "allow"),),
    )
    transport = FakeTransport([approval, MuseTurnFinished("turn-1", "completed")])
    executor = MuseExecutor(lambda: transport)

    async def policy(*args: Any) -> Verdict:
        return Verdict("POLICY_ACTION_ASK")

    executor._policy_evaluator = policy
    await collect(executor)
    assert transport.decisions == [("session-1", "approval-1", "only")]


def test_declares_first_class_agent_loop_capabilities() -> None:
    executor = MuseExecutor(lambda: FakeTransport())
    assert executor.supports_streaming()
    assert executor.supports_tool_calling()
    assert executor.handles_tools_internally()


# ── Provider-aware authentication diagnostics (#13) ───────────────────────


async def test_auth_required_with_meta_provider() -> None:
    """Meta provider auth failure gets login hints."""
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="authentication required",
                error_kind="authRequired",
                retryable=False,
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider="meta")
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    assert error.message == (
        "Muse provider authentication failed (provider=meta, authRequired). "
        "Run `muse login` or `muse auth set`, or set META_API_KEY and add it "
        "to executor.config.env_passthrough."
    )
    assert error.retryable is False
    assert error.preserve_session is True


async def test_auth_required_with_echo_provider() -> None:
    """Echo provider (credential-free) does not suggest login."""
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="authentication required",
                error_kind="authRequired",
                retryable=False,
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider="echo")
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    assert error.message == (
        "Muse provider authentication failed (provider=echo, authRequired). "
        "Echo provider requires no credentials. Verify configuration and try again."
    )
    assert "muse login" not in error.message
    assert error.retryable is False
    assert error.preserve_session is True


async def test_auth_required_with_unknown_provider() -> None:
    """No provider configured defaults to generic message."""
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="authentication required",
                error_kind="authRequired",
                retryable=False,
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider=None)
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    assert error.message == (
        "Muse provider authentication failed (provider=unknown, authRequired). "
        "Check your Muse credentials and provider configuration."
    )
    assert error.retryable is False
    assert error.preserve_session is True


async def test_auth_required_preserves_session() -> None:
    """Auth failures preserve healthy session for retry after login."""
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="no credentials",
                error_kind="authRequired",
                retryable=False,
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider="meta")
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    assert error.preserve_session is True
    # Session should still be valid, so no cleanup should happen
    assert executor._session_id == "session-1"


async def test_non_auth_error_unchanged() -> None:
    """Non-auth errors pass through without special handling."""
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="model not found",
                error_kind="modelError",
                retryable=True,
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider="meta")
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    assert error.message == "model not found"
    assert "authentication failed" not in error.message
    assert error.retryable is True
    assert error.preserve_session is True


async def test_auth_error_sets_retryable_false_even_if_msp_says_true() -> None:
    """Auth errors are never retryable, even if MSP suggests otherwise."""
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="auth failed",
                error_kind="authRequired",
                retryable=True,  # MSP might say true, but we override
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider="meta")
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    assert error.retryable is False


async def test_auth_message_contains_exact_provider_name() -> None:
    """Exact message format matches contract: provider=<name>, <error code>."""
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="logged out",
                error_kind="authRequired",
                retryable=False,
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider="meta")
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    # Contract format: "Muse provider authentication failed (provider=<provider>, <error code>). <hint>"
    assert error.message.startswith(
        "Muse provider authentication failed (provider=meta, authRequired). "
    )


async def test_local_provider_auth_failure() -> None:
    """Local provider shows generic hint."""
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="auth failed",
                error_kind="authRequired",
                retryable=False,
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider="local")
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    assert error.message == (
        "Muse provider authentication failed (provider=local, authRequired). "
        "Check your Muse credentials and provider configuration."
    )


async def test_auth_required_with_missing_provider_metadata() -> None:
    """When provider is not configured, message uses 'unknown' provider.

    Neither a configured provider nor a session providerId is available.
    """
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="no credentials",
                error_kind="authRequired",
                retryable=False,
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider=None)
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    assert "provider=unknown" in error.message
    assert error.retryable is False
    assert error.preserve_session is True


async def test_dead_transport_during_turn_clears_session() -> None:
    """A transport that dies mid-turn is discarded rather than preserved.

    Contrasts with authRequired on a healthy transport, which keeps the session.
    """

    class DeadTransport(FakeTransport):
        async def run_turn(
            self,
            session_id: str,
            *,
            text: str,
            reasoning_effort: str | None,
        ) -> AsyncIterator[MuseEvent]:
            self.turns.append(
                {
                    "session_id": session_id,
                    "text": text,
                    "reasoning_effort": reasoning_effort,
                }
            )
            raise MuseTransportError(
                "host exited during turn",
                retryable=False,
                preserve_session=False,
                transport_dead=True,
            )
            # This yield is unreachable but satisfies type checker for async generator
            yield  # type: ignore

    executor = MuseExecutor(lambda: DeadTransport(), provider="meta")
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    # Dead transport sets preserve_session=False from MuseTransportError
    assert error.preserve_session is False
    # Session should be cleared after dead transport
    assert executor._session_id is None


async def test_auth_error_on_live_transport_preserves_session() -> None:
    """Auth failures on healthy transport preserve session for retry.

    When we successfully receive a turn/completed notification with
    authRequired, the MSP connection is still healthy (since we received
    the full event), so we preserve the session for retry after login.
    """
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="no credentials",
                error_kind="authRequired",
                retryable=False,
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider="meta")
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    # Session preserved because transport successfully yielded the event
    assert error.preserve_session is True
    assert executor._session_id == "session-1"
    # Transport should still be available for next turn
    assert executor._transport is not None


async def test_auth_error_with_provider_mismatch() -> None:
    """When active provider differs from configured, message names both.

    This tests the case where Muse selected a different provider (e.g., 'echo')
    than what the harness is configured for (e.g., 'meta'). The message includes
    both provider names and guidance to check the configuration.
    """
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="no credentials",
                error_kind="authRequired",
                retryable=False,
            ),
        ]
    )
    transport.active_provider = "echo"  # reported by session/start
    executor = MuseExecutor(lambda: transport, provider="meta")
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    assert error.message == (
        "Muse provider authentication failed (provider=echo, authRequired). "
        "Muse used provider echo but the harness is configured for meta; check "
        "executor.config.provider / HARNESS_MUSE_PROVIDER and Muse's default provider."
    )
    assert "muse login" not in error.message
    assert error.retryable is False
    assert error.preserve_session is True


async def test_auth_error_uses_active_provider_when_unconfigured() -> None:
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="no credentials",
                error_kind="authRequired",
            ),
        ]
    )
    transport.active_provider = "meta"
    executor = MuseExecutor(lambda: transport)
    [error] = await collect(executor)

    assert isinstance(error, ExecutorError)
    assert error.message.startswith(
        "Muse provider authentication failed (provider=meta, authRequired). Run `muse login`"
    )


async def test_auth_error_with_missing_active_provider_metadata() -> None:
    """When active provider is unknown but configured provider is known.

    This tests the case where session providerId is not available (None or missing)
    but the harness has a configured provider. Use configured provider in the message.
    """
    transport = FakeTransport(
        [
            MuseTurnFinished(
                "turn-1",
                "failed",
                error="no credentials",
                error_kind="authRequired",
                retryable=False,
            ),
        ]
    )
    executor = MuseExecutor(lambda: transport, provider="meta")
    # _active_provider remains None (not captured from session)
    events = await collect(executor)

    [error] = events
    assert isinstance(error, ExecutorError)
    # Falls back to configured provider
    assert error.message.startswith(
        "Muse provider authentication failed (provider=meta, authRequired)."
    )
