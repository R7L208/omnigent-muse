"""Concrete :class:`MuseTransport` backed by the vendored MSP client."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from .msp_client import (
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
from .muse_executor import (
    MuseApprovalChoice,
    MuseApprovalRequested,
    MuseEvent,
    MuseReasoningDelta,
    MuseTextDelta,
    MuseToolCall,
    MuseTransportError,
    MuseTurnFinished,
    MuseTurnStarted,
)

JsonObject = dict[str, Any]

# Keep the long-lived agent process isolated from credentials and runtime knobs
# belonging to the harness. Additions should be limited to variables Muse needs
# to locate user state, execute tools, or establish its network connection.
_SPAWN_ENV_ALLOWLIST = frozenset(
    {
        "COLORTERM",
        "FORCE_COLOR",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NO_COLOR",
        "NO_PROXY",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)


def _spawn_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the deny-by-default environment supplied to ``muse serve``."""
    values = os.environ if source is None else source
    return {name: values[name] for name in _SPAWN_ENV_ALLOWLIST if name in values}


def _package_version() -> str:
    try:
        return version("omnigent-muse")
    except PackageNotFoundError:
        return "0.0.0"


def _object(value: object) -> JsonObject:
    return dict(value) if isinstance(value, dict) else {}


def _first_str(*values: object) -> str | None:
    return next((value for value in values if isinstance(value, str) and value), None)


class MspTransport:
    """Adapt one owned ``muse serve`` process to executor-level events."""

    def __init__(
        self,
        client: MspClient | None = None,
        *,
        executable: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._executable = executable
        self._cwd = cwd
        self._env = env
        self._approval_requirements: dict[tuple[str, str], JsonObject] = {}
        self._item_kinds: dict[str, str] = {}

    @classmethod
    async def spawn(
        cls,
        *,
        executable: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> MspTransport:
        binary = executable or os.environ.get("OMNIGENT_MUSE_PATH") or "muse"
        client = await MspClient.spawn(
            [binary, "serve"],
            cwd=cwd,
            env=_spawn_env(env),
            client_version=_package_version(),
            client_title="Omnigent Muse",
        )
        return cls(client)

    async def _get_client(self) -> MspClient:
        if self._client is None:
            binary = self._executable or os.environ.get("OMNIGENT_MUSE_PATH") or "muse"
            self._client = await MspClient.spawn(
                [binary, "serve"],
                cwd=self._cwd,
                env=_spawn_env(self._env),
                client_version=_package_version(),
                client_title="Omnigent Muse",
            )
        return self._client

    async def start_session(
        self,
        *,
        workspace_root: str | None,
        model: str | None,
        approval_mode: str,
    ) -> str:
        try:
            client = await self._get_client()
            session = await client.start_session(
                workspace_root=workspace_root,
                model_id=model,
                approval_mode=approval_mode,
            )
            session_id = _first_str(session.get("sessionId"), session.get("id"))
            if session_id is None:
                raise MspProtocolError("session/start returned no session id")
            return session_id
        except (MspConnectionClosed, MspError, MspProtocolError) as exc:
            raise self._error(exc) from exc

    async def run_turn(
        self,
        session_id: str,
        *,
        text: str,
        reasoning_effort: str | None,
    ) -> AsyncIterator[MuseEvent]:
        try:
            client = await self._get_client()
            with client.open_stream(session_id) as stream:
                try:
                    turn_id = await client.send_turn(
                        session_id,
                        [{"type": "text", "text": text}],
                        reasoning_effort=reasoning_effort,
                    )
                except MspError as exc:
                    # A rejected turn/start request leaves the established
                    # session idle and available for another turn.
                    raise self._error(exc, preserve_session=True) from exc
                yield MuseTurnStarted(turn_id)
                latest_usage: JsonObject = {}
                async for event in stream.follow(turn_id):
                    if isinstance(event, MspTextDelta):
                        kind = self._item_kinds.get(event.item_id)
                        if kind == "reasoning" or (
                            event.field is not None
                            and event.field.startswith("summary.")
                        ):
                            yield MuseReasoningDelta(event.delta)
                        elif kind in {None, "agentMessage"} and event.field in {
                            None,
                            "text",
                        }:
                            yield MuseTextDelta(event.delta)
                    elif isinstance(event, MspItemUpdate):
                        item_event = self._item(event)
                        if item_event is not None:
                            yield item_event
                    elif isinstance(event, MspTokenUsage):
                        latest_usage = self._usage(
                            prompt_tokens=event.prompt_tokens,
                            output_tokens=event.output_tokens,
                            total_tokens=event.total_tokens,
                        )
                    elif isinstance(event, MspApprovalRequested):
                        yield self._approval(event)
                    elif isinstance(event, MspTurnCompleted):
                        usage = self._usage(
                            prompt_tokens=event.usage.get("promptTokens"),
                            output_tokens=event.usage.get("outputTokens"),
                            total_tokens=event.usage.get("totalTokens"),
                            cached_tokens=event.usage.get("cachedTokens"),
                            reasoning_tokens=event.usage.get("reasoningTokens"),
                        ) or latest_usage
                        state = event.terminal or "completed"
                        if state in {"canceled", "retracted"}:
                            state = "cancelled"
                        elif event.error_kind is not None and state == "completed":
                            state = "failed"
                        yield MuseTurnFinished(
                            turn_id=event.turn_id or turn_id,
                            state=state,
                            usage=usage,
                            error=event.error_message or event.reason,
                            retryable=event.error_retryable is True,
                        )
        except (MspConnectionClosed, MspError, MspProtocolError) as exc:
            raise self._error(exc) from exc

    async def decide_approval(
        self, session_id: str, approval_id: str, choice_id: str
    ) -> None:
        key = (session_id, approval_id)
        requirement_id = self._approval_requirements.get(
            key,
            {"approvalId": approval_id, "sourceIndex": 0},
        )
        try:
            client = await self._get_client()
            await client.decide_approval(
                session_id, approval_id, choice_id, requirement_id
            )
        except (MspConnectionClosed, MspError, MspProtocolError) as exc:
            raise self._error(exc) from exc
        self._approval_requirements.pop(key, None)

    async def interrupt_turn(self, session_id: str, turn_id: str | None) -> bool:
        try:
            client = await self._get_client()
            result = await client.interrupt_turn(session_id, turn_id=turn_id)
        except (MspConnectionClosed, MspError, MspProtocolError) as exc:
            raise self._error(exc) from exc
        status = result.get("status")
        return status not in {"noop", "notRunning", "not_running", False}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _approval(self, event: MspApprovalRequested) -> MuseApprovalRequested:
        raw = event.raw
        approval = _object(raw.get("approval"))
        requirement = _object(approval.get("requirement") or raw.get("requirement"))
        tool = _object(
            requirement.get("toolCall")
            or approval.get("toolCall")
            or raw.get("toolCall")
            or requirement.get("tool")
        )
        requirement_id = _object(
            raw.get("currentRequirementId")
            or requirement.get("requirementId")
            or requirement.get("id")
            or approval.get("requirementId")
            or raw.get("requirementId")
        )
        if not requirement_id:
            requirement_id = {"approvalId": event.approval_id, "sourceIndex": 0}
        self._approval_requirements[(event.session_id, event.approval_id)] = (
            requirement_id
        )
        raw_choices = (
            raw.get("availableChoices")
            or approval.get("choices")
            or requirement.get("choices")
            or raw.get("choices")
            or []
        )
        choices: list[MuseApprovalChoice] = []
        if isinstance(raw_choices, Sequence) and not isinstance(raw_choices, str):
            for value in raw_choices:
                choice = _object(value)
                choice_id = _first_str(choice.get("choiceId"), choice.get("id"))
                if choice_id is None:
                    continue
                label = (
                    _first_str(choice.get("label"), choice.get("title")) or choice_id
                )
                decision = _first_str(
                    choice.get("decision"), choice.get("kind"), choice.get("value")
                ) or self._decision(choice_id)
                choices.append(MuseApprovalChoice(choice_id, label, decision.lower()))
        return MuseApprovalRequested(
            approval_id=event.approval_id,
            tool_name=_first_str(
                tool.get("name"),
                requirement.get("toolName"),
                approval.get("toolName"),
                raw.get("toolName"),
            )
            or "muse_action",
            arguments=tool.get(
                "arguments",
                raw.get(
                    "arguments",
                    requirement.get(
                        "arguments", approval.get("arguments", raw.get("subject", {}))
                    ),
                ),
            ),
            choices=tuple(choices),
        )

    @staticmethod
    def _decision(choice_id: str) -> str:
        normalized = choice_id.lower()
        if any(word in normalized for word in ("deny", "reject", "abort")):
            return "deny"
        if any(word in normalized for word in ("allow", "approve", "accept")):
            return "allow"
        return normalized

    def _item(self, event: MspItemUpdate) -> MuseToolCall | None:
        item = event.item
        item_id = _first_str(item.get("itemId"), item.get("callId"))
        kind = _first_str(item.get("kind"))
        if item_id is not None and kind is not None:
            self._item_kinds[item_id] = kind
        if kind != "toolCall" or item_id is None:
            return None
        status = _first_str(item.get("status")) or (
            "started" if event.phase == "started" else "completed"
        )
        state = {
            "in_progress": "inProgress",
            "succeeded": "completed",
            "timed_out": "timedOut",
        }.get(status, status)
        return MuseToolCall(
            call_id=_first_str(item.get("callId"), item_id) or item_id,
            name=_first_str(item.get("tool")) or "tool",
            arguments=item.get("args", {}),
            state=state,
            output=item.get("visibleOutput"),
            error=_first_str(item.get("failureReason"), item.get("failureKind")),
            duration_ms=float(item.get("durationMs", 0) or 0),
        )

    @staticmethod
    def _usage(
        *,
        prompt_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        cached_tokens: int | None = None,
        reasoning_tokens: int | None = None,
    ) -> JsonObject:
        usage: JsonObject = {}
        if prompt_tokens is not None:
            usage["inputTokens"] = prompt_tokens
        if output_tokens is not None:
            usage["outputTokens"] = output_tokens
        if total_tokens is not None:
            usage["totalTokens"] = total_tokens
        if cached_tokens is not None:
            usage["cachedTokens"] = cached_tokens
        if reasoning_tokens is not None:
            usage["reasoningTokens"] = reasoning_tokens
        return usage

    @staticmethod
    def _error(
        exc: Exception, *, preserve_session: bool = False
    ) -> MuseTransportError:
        retryable = isinstance(exc, MspConnectionClosed) or (
            isinstance(exc, MspError) and exc.retryable is True
        )
        return MuseTransportError(
            str(exc), retryable=retryable, preserve_session=preserve_session
        )
