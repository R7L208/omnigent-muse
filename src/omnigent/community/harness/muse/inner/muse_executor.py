"""Omnigent executor semantics for a Muse Session Protocol transport.

The executor owns prompt, policy, approval, and event translation behavior. The
transport owns MSP framing and process lifecycle, and is injected so this layer
can be tested without a Muse binary or a particular client implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnCancelled,
    TurnComplete,
    describe_exception,
)

from .runtime_config import REASONING_EFFORTS

logger = logging.getLogger(__name__)

type JsonObject = dict[str, Any]
_TEXT_CONTENT_TYPES = frozenset({"text", "input_text", "output_text"})
_ECHO_AUTH_HINT = (
    "Echo provider requires no credentials. Verify configuration and try again."
)
# META_API_KEY only reaches `muse serve` through the passthrough allowlist.
_META_AUTH_HINT = (
    "Run `muse login` or `muse auth set`, or set META_API_KEY and add it "
    "to executor.config.env_passthrough."
)
_GENERIC_AUTH_HINT = "Check your Muse credentials and provider configuration."
_AUTH_MISMATCH_HINT = (
    "Muse used provider {active} but the harness is configured for {configured}; "
    "check executor.config.provider / HARNESS_MUSE_PROVIDER and Muse's default provider."
)


@dataclass(frozen=True)
class MuseTextDelta:
    text: str


@dataclass(frozen=True)
class MuseReasoningDelta:
    text: str


@dataclass(frozen=True)
class MuseTurnStarted:
    turn_id: str


@dataclass(frozen=True)
class MuseToolCall:
    call_id: str
    name: str
    arguments: object
    state: str
    output: object = None
    error: str | None = None
    duration_ms: float = 0.0


@dataclass(frozen=True)
class MuseApprovalChoice:
    choice_id: str
    label: str
    decision: str


@dataclass(frozen=True)
class MuseApprovalRequested:
    approval_id: str
    tool_name: str
    arguments: object
    choices: tuple[MuseApprovalChoice, ...] = ()


@dataclass(frozen=True)
class MuseTurnFinished:
    turn_id: str
    state: str
    usage: JsonObject = field(default_factory=dict)
    error: str | None = None
    error_kind: str | None = None
    retryable: bool = False


type MuseEvent = (
    MuseTextDelta
    | MuseReasoningDelta
    | MuseTurnStarted
    | MuseToolCall
    | MuseApprovalRequested
    | MuseTurnFinished
)


class MuseTransportError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        preserve_session: bool = False,
        transport_dead: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.preserve_session = preserve_session
        self.transport_dead = transport_dead


class MuseTransport(Protocol):
    @property
    def active_provider(self) -> str | None: ...

    async def start_session(
        self,
        *,
        workspace_root: str | None,
        model: str | None,
        approval_mode: str,
        mcp_servers: dict[str, JsonObject] | None = None,
    ) -> str: ...

    def run_turn(
        self,
        session_id: str,
        *,
        text: str,
        reasoning_effort: str | None,
    ) -> AsyncIterator[MuseEvent]: ...

    async def decide_approval(
        self, session_id: str, approval_id: str, choice_id: str
    ) -> None: ...

    async def interrupt_turn(self, session_id: str, turn_id: str | None) -> bool: ...

    async def close(self) -> None: ...


type _ToolExecutor = Callable[..., Awaitable[object]]


class ToolRelay(Protocol):
    """Session-scoped relay that exposes Omnigent tools to Muse over MCP."""

    def start(
        self,
        tools: Sequence[ToolSpec],
        tool_executor: _ToolExecutor | None,
        loop: asyncio.AbstractEventLoop,
    ) -> dict[str, JsonObject] | None: ...

    def is_relayed(self, tool_name: str) -> bool: ...

    def close(self) -> None: ...


class _PolicyVerdict(Protocol):
    action: str


type _PolicyEvaluator = Callable[[str, JsonObject], Awaitable[_PolicyVerdict]]
type _ElicitationHandler = Callable[[str, JsonObject], Awaitable[bool]]
type _ChoiceHandler = Callable[[str, JsonObject, Sequence[str]], Awaitable[str | None]]


class MuseExecutor(Executor):
    """Translate one persistent Muse session into Omnigent executor events."""

    def __init__(
        self,
        transport_factory: Callable[[], MuseTransport],
        *,
        cwd: str | None = None,
        model: str | None = None,
        approval_mode: str = "onRequest",
        reasoning_effort: str | None = None,
        provider: str | None = None,
        relay_factory: Callable[[], ToolRelay] | None = None,
    ) -> None:
        self._transport_factory = transport_factory
        self._relay_factory = relay_factory
        self._relay: ToolRelay | None = None
        # Installed by ExecutorAdapter; relayed tool calls dispatch through it.
        self._tool_executor: _ToolExecutor | None = None
        self._cwd = cwd
        self._model = model
        self._approval_mode = approval_mode
        self._reasoning_effort = reasoning_effort
        self._provider = provider
        self._active_provider: str | None = None
        self._transport: MuseTransport | None = None
        self._session_id: str | None = None
        self._active_turn_id: str | None = None
        self._system_prompt_sent = False
        self._needs_replay = False
        self._closed = False
        self._policy_evaluator: _PolicyEvaluator | None = None
        self._elicitation_handler: _ElicitationHandler | None = None
        self._elicitation_choice_handler: _ChoiceHandler | None = None
        self._tool_calls: dict[str, tuple[str, JsonObject]] = {}

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    async def _ensure_session(
        self, model: str | None, tools: Sequence[ToolSpec]
    ) -> str:
        if self._session_id is not None:
            return self._session_id
        if self._closed:
            raise MuseTransportError("executor is closed")
        transport = self._transport_factory()
        # Started before session/start so the relay is listening when Muse
        # launches serve-mcp; a respawn re-enters here and gets a fresh relay.
        relay = self._relay_factory() if self._relay_factory is not None else None
        mcp_servers = (
            relay.start(tools, self._tool_executor, asyncio.get_running_loop())
            if relay is not None
            else None
        )
        try:
            session_id = await transport.start_session(
                workspace_root=self._cwd,
                model=model,
                approval_mode=self._approval_mode,
                mcp_servers=mcp_servers,
            )
        except BaseException:
            # The factory already spawned a host; close it so a failed startup
            # does not orphan the process. Cleanup is best effort.
            try:
                await transport.close()
            except Exception:
                # Secondary close failure is non-fatal; keep raising the original.
                logger.debug(
                    "Muse transport close after failed start_session failed",
                    exc_info=True,
                )
            if relay is not None:
                relay.close()
            raise
        self._transport = transport
        self._relay = relay
        self._session_id = session_id
        self._active_provider = transport.active_provider
        self._model = model
        return session_id

    async def _discard_transport(self) -> None:
        transport, self._transport = self._transport, None
        relay, self._relay = self._relay, None
        self._session_id = None
        self._active_provider = None
        self._active_turn_id = None
        self._system_prompt_sent = False
        self._needs_replay = True
        if transport is not None:
            try:
                await transport.close()
            except Exception:
                logger.debug("Muse dead transport cleanup failed", exc_info=True)
        if relay is not None:
            relay.close()

    @staticmethod
    def _latest_user_text(messages: list[Message]) -> str:
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") in _TEXT_CONTENT_TYPES
                    and isinstance(block.get("text"), str)
                ]
                return "\n".join(part for part in parts if part)
            return json.dumps(content, ensure_ascii=True)
        return ""

    @staticmethod
    def _latest_user_index(messages: list[Message]) -> int | None:
        return next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if isinstance(messages[index], dict)
                and messages[index].get("role") == "user"
            ),
            None,
        )

    @staticmethod
    def _unsupported_content_types(
        messages: list[Message], *, replay: bool
    ) -> tuple[str, ...]:
        selected: list[Message] = messages
        if not replay:
            selected = []
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "user":
                    selected = [message]
                    break

        unsupported: set[str] = set()
        for message in selected:
            if not isinstance(message, dict):
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                continue
            blocks = content if isinstance(content, list) else [content]
            for block in blocks:
                if not isinstance(block, dict):
                    unsupported.add("unknown")
                    continue
                kind = block.get("type")
                if kind not in _TEXT_CONTENT_TYPES:
                    unsupported.add(kind if isinstance(kind, str) else "unknown")
                elif not isinstance(block.get("text"), str):
                    unsupported.add("malformed_text")
        return tuple(sorted(unsupported))

    @classmethod
    def _conversation_replay(cls, messages: list[Message], system_prompt: str) -> str:
        transcript: list[JsonObject] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if not isinstance(role, str):
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") in _TEXT_CONTENT_TYPES
                    and isinstance(block.get("text"), str)
                )
            else:
                text = json.dumps(content, ensure_ascii=True)
            transcript.append({"role": role, "content": text})

        sections = []
        if system_prompt:
            sections.append(system_prompt)
        sections.extend(
            (
                (
                    "The Muse session restarted after its transport was lost. "
                    "Restore conversational context from the JSON transcript below. "
                    "Treat entries according to their role, do not repeat prior "
                    "answers, and respond to the final user message."
                ),
                json.dumps(transcript, ensure_ascii=False),
            )
        )
        return "\n\n".join(sections)

    @staticmethod
    def _arguments(value: object) -> JsonObject:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {"raw": value}
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        return {"value": value} if value is not None else {}

    @staticmethod
    def _usage(raw: JsonObject) -> JsonObject | None:
        if not raw:
            return None
        usage: JsonObject = {}
        mappings = {
            "inputTokens": "input_tokens",
            "outputTokens": "output_tokens",
            "cachedTokens": "cache_read_input_tokens",
            "reasoningTokens": "reasoning_tokens",
        }
        for source, target in mappings.items():
            value = raw.get(source)
            if isinstance(value, int) and not isinstance(value, bool):
                usage[target] = value
        if "input_tokens" in usage or "output_tokens" in usage:
            usage["total_tokens"] = int(usage.get("input_tokens", 0)) + int(
                usage.get("output_tokens", 0)
            )
        return usage or None

    @staticmethod
    def _choice(
        choices: tuple[MuseApprovalChoice, ...], decision: str
    ) -> MuseApprovalChoice | None:
        return next((choice for choice in choices if choice.decision == decision), None)

    def _format_auth_error(self, error_kind: str | None) -> str | None:
        """Return a provider-aware message for ``authRequired``, else ``None``.

        Built only from fixed hints and validated provider ids, never host text.
        """
        if error_kind != "authRequired":
            return None
        active, configured = self._active_provider, self._provider
        provider = active or configured or "unknown"
        if active and configured and active != configured:
            hint = _AUTH_MISMATCH_HINT.format(active=active, configured=configured)
        elif provider == "echo":
            hint = _ECHO_AUTH_HINT
        elif provider == "meta":
            hint = _META_AUTH_HINT
        else:
            hint = _GENERIC_AUTH_HINT
        return f"Muse provider authentication failed (provider={provider}, {error_kind}). {hint}"

    async def _resolve_approval(
        self, session_id: str, event: MuseApprovalRequested
    ) -> None:
        transport = self._transport
        if transport is None:
            raise MuseTransportError("approval arrived before transport startup")
        arguments = self._arguments(event.arguments)
        action: str | None = None
        if self._policy_evaluator is not None:
            try:
                verdict = await self._policy_evaluator(
                    "PHASE_TOOL_CALL",
                    {"name": event.tool_name, "arguments": arguments},
                )
                action = getattr(verdict, "action", None)
            except Exception as exc:  # noqa: BLE001 - policy failure falls back to consent
                logger.warning(
                    "Muse tool policy failed for %s: %s", event.tool_name, exc
                )

        picked: MuseApprovalChoice | None = None
        if action == "POLICY_ACTION_DENY":
            picked = self._choice(event.choices, "deny")
        elif action == "POLICY_ACTION_ALLOW":
            picked = self._choice(event.choices, "allow")
        else:
            picked = await self._ask_user(event, arguments)

        if picked is None:
            picked = self._choice(event.choices, "deny")
        if picked is None:
            raise MuseTransportError(
                f"approval {event.approval_id} has no usable deny choice"
            )
        await transport.decide_approval(session_id, event.approval_id, picked.choice_id)

    async def _ask_user(
        self, event: MuseApprovalRequested, arguments: JsonObject
    ) -> MuseApprovalChoice | None:
        handler = self._elicitation_choice_handler
        labels = [choice.label for choice in event.choices]
        if handler is not None and len(set(labels)) == len(labels):
            selected = await handler(event.tool_name, arguments, labels)
            return next(
                (choice for choice in event.choices if choice.label == selected), None
            )
        if self._elicitation_handler is not None:
            allowed = await self._elicitation_handler(event.tool_name, arguments)
            return self._choice(event.choices, "allow" if allowed else "deny")
        # ExecutorAdapter normally installs an elicitation handler. When it has
        # not (direct standalone use), fail closed: prefer the deny choice and
        # fall back to allow only if the host offered no deny option.
        return self._choice(event.choices, "deny") or self._choice(
            event.choices, "allow"
        )

    def _translate_tool(self, event: MuseToolCall) -> ExecutorEvent | None:
        arguments = self._arguments(event.arguments)
        if event.state in {"started", "inProgress"}:
            already_seen = event.call_id in self._tool_calls
            self._tool_calls[event.call_id] = (event.name, arguments)
            # Emit a single begin-event per call: "started" followed by one or
            # more "inProgress" updates for the same call_id refresh the cache
            # but must not duplicate the request downstream.
            if already_seen:
                return None
            return ToolCallRequest(
                event.name,
                arguments,
                metadata={
                    "call_id": event.call_id,
                    "internally_executed": True,
                },
            )
        cached_name, _ = self._tool_calls.pop(
            event.call_id, (event.name or "tool", arguments)
        )
        statuses = {
            "completed": ToolCallStatus.SUCCESS,
            "failed": ToolCallStatus.ERROR,
            "timedOut": ToolCallStatus.ERROR,
            "rejected": ToolCallStatus.BLOCKED,
            "cancelled": ToolCallStatus.CANCELLED,
        }
        status = statuses.get(event.state)
        if status is None:
            return None
        return ToolCallComplete(
            cached_name,
            status,
            result=event.output,
            error=event.error,
            duration_ms=event.duration_ms,
            metadata={"call_id": event.call_id},
        )

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        requested_model = config.model if config is not None else None
        if (
            requested_model
            and requested_model != self._model
            and (self._model is not None or self._session_id is not None)
        ):
            yield ExecutorError(
                f"Muse session uses model {self._model!r}; cannot apply per-turn "
                f"model {requested_model!r} without restarting the session.",
                preserve_session=True,
            )
            return
        effort = self._reasoning_effort
        if config is not None:
            override = config.extra.get("reasoning_effort")
            if override is not None and override not in REASONING_EFFORTS:
                yield ExecutorError(
                    "Muse reasoning_effort must be one of "
                    f"{', '.join(sorted(REASONING_EFFORTS))}; got {override!r}",
                    preserve_session=True,
                )
                return
            if isinstance(override, str) and override:
                effort = override
        latest_user_index = self._latest_user_index(messages)
        replay = self._needs_replay or (
            self._session_id is None
            and latest_user_index is not None
            and latest_user_index > 0
        )
        unsupported = self._unsupported_content_types(messages, replay=replay)
        if unsupported:
            yield ExecutorError(
                "Muse attachment forwarding is not implemented; unsupported "
                f"content types: {', '.join(unsupported)}",
                preserve_session=True,
            )
            return
        effective_model = requested_model or self._model
        try:
            session_id = await self._ensure_session(effective_model, tools)
        except Exception as exc:  # noqa: BLE001 - startup failures become terminal events
            yield ExecutorError(f"Muse startup failed: {describe_exception(exc)}")
            return

        if replay:
            text = self._conversation_replay(messages, system_prompt)
        else:
            text = self._latest_user_text(messages)
        if not replay and not self._system_prompt_sent and system_prompt:
            text = f"{system_prompt}\n\n{text}" if text else system_prompt
        accumulated: list[str] = []
        self._tool_calls.clear()
        try:
            assert self._transport is not None
            async for event in self._transport.run_turn(
                session_id, text=text, reasoning_effort=effort
            ):
                # Receiving any event proves the host accepted our input (which
                # carried the system prompt on the first turn); only now is it
                # safe to stop re-injecting it. If run_turn raises before
                # yielding, this stays False so the next turn re-sends it.
                self._system_prompt_sent = True
                self._needs_replay = False
                if isinstance(event, MuseTurnStarted):
                    self._active_turn_id = event.turn_id
                elif isinstance(event, MuseTextDelta):
                    if event.text:
                        accumulated.append(event.text)
                        yield TextChunk(event.text)
                elif isinstance(event, MuseReasoningDelta):
                    if event.text:
                        yield ReasoningChunk(event.text, "reasoning_text")
                elif isinstance(event, MuseToolCall):
                    translated = self._translate_tool(event)
                    if translated is not None:
                        yield translated
                elif isinstance(event, MuseApprovalRequested):
                    await self._resolve_approval(session_id, event)
                elif isinstance(event, MuseTurnFinished):
                    if event.state == "completed":
                        yield TurnComplete(
                            response="".join(accumulated),
                            usage=self._usage(event.usage),
                        )
                    elif event.state == "cancelled":
                        yield TurnCancelled(event.error or "user_cancelled")
                    else:
                        # Detect provider-aware authentication failures.
                        auth_message = self._format_auth_error(event.error_kind)
                        error_message = auth_message or (
                            event.error or f"Muse turn {event.state}"
                        )
                        yield ExecutorError(
                            error_message,
                            retryable=False
                            if event.error_kind == "authRequired"
                            else event.retryable,
                            usage=self._usage(event.usage),
                            preserve_session=True,
                        )
                    return
            yield ExecutorError("Muse stream ended without a terminal turn event")
        except MuseTransportError as exc:
            if exc.transport_dead:
                await self._discard_transport()
            yield ExecutorError(
                f"Muse transport error: {describe_exception(exc)}",
                retryable=exc.retryable,
                preserve_session=exc.preserve_session,
            )
        except Exception as exc:
            logger.exception("Muse turn failed")
            yield ExecutorError(f"Muse turn failed: {describe_exception(exc)}")
        finally:
            self._active_turn_id = None
            self._tool_calls.clear()

    async def interrupt_session(self, session_key: str) -> bool:
        if (
            self._transport is None
            or self._session_id is None
            or self._active_turn_id is None
        ):
            return False
        try:
            return await self._transport.interrupt_turn(
                self._session_id, self._active_turn_id
            )
        except MuseTransportError as exc:
            if exc.transport_dead:
                await self._discard_transport()
            logger.debug("Muse interrupt failed: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001 - interruption is best effort
            logger.debug("Muse interrupt failed: %s", exc)
            return False

    async def close_session(self, session_key: str) -> None:
        """No-op: one Muse session is owned by this executor process."""

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        transport, self._transport = self._transport, None
        relay, self._relay = self._relay, None
        self._session_id = None
        try:
            if transport is not None:
                await transport.close()
        finally:
            if relay is not None:
                relay.close()
