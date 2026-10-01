"""Minimal async MSP client over a ``muse serve`` stdio host.

Vendored transport for the Muse harness: NDJSON JSON-RPC 2.0 framing,
the ``initialize`` handshake, request/response correlation, and
notification fan-out, plus typed helpers for the session / turn /
approval / usage surface the executor needs.

This module is deliberately free of Omnigent concepts (no sessions,
policies, or tools): it speaks bytes to one host process. That keeps it
swappable if a future published SDK becomes usable.

The served schema fingerprint and host version are recorded, never
gated: callers that need version policy compare
:attr:`MspClient.fingerprint` against their own known-good set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Self

from omnigent.inner import _proc

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]

# The StreamReader limit is raised to 16 MiB so a large session/resume
# payload or tool-output line can't hit the default 64 KiB per-line cap.
_STREAM_LIMIT = 16 * 1024 * 1024
_DEFAULT_REQUEST_TIMEOUT = 30.0
_DEFAULT_COMMAND_ATTEMPTS = 3
_COMMAND_RETRYABLE_KINDS = frozenset({"backpressured", "overloaded"})
_COMMAND_RETRY_BASE_DELAY = 0.2
_STDERR_LINE_LIMIT = 2000
_STDERR_RING_SIZE = 50
_STDERR_QUOTED_LINES = 5
_STDERR_QUOTED_LIMIT = 2000
# `clientInfo.name` must be a machine identifier (host rejects anything else).
_CLIENT_NAME_RE = re.compile(r"^[a-z0-9_]+$")


class MspError(Exception):
    """A server-authored MSP error (``error`` response object).

    Branch on :attr:`kind`, never on message text.
    """

    def __init__(
        self,
        code: int,
        message: str,
        kind: str = "unknown",
        data: JsonObject | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.kind = kind
        self.data = dict(data) if data else {}
        retryable = self.data.get("retryable")
        self.retryable: bool | None = retryable if isinstance(retryable, bool) else None


class MspProtocolError(Exception):
    """A local framing/correlation violation, never a server-authored error."""


class MspConnectionClosed(Exception):
    """The host is gone (EOF / close); no further traffic is possible."""


# ---------------------------------------------------------------------------
# Turn-stream events
# ---------------------------------------------------------------------------


@dataclass
class MspTextDelta:
    """One streaming text append to an open item."""

    delta: str
    item_id: str
    field: str | None = None


@dataclass
class MspTokenUsage:
    """Per-turn and cumulative token counters from ``session/tokenUsage``."""

    session_id: str
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cumulative: dict[str, int] = field(default_factory=dict)
    duration_ms: int | None = None
    model_id: str | None = None


@dataclass
class MspTurnCompleted:
    """A turn reached its terminal record (success or failure)."""

    session_id: str
    turn_id: str | None = None
    duration_ms: int | None = None
    usage: dict[str, int] = field(default_factory=dict)
    error_kind: str | None = None
    error_message: str | None = None
    error_retryable: bool | None = None


@dataclass
class MspApprovalRequested:
    """The host parked on an approval (tool call, question, or input)."""

    session_id: str
    approval_id: str
    raw: JsonObject = field(default_factory=dict)


MspEvent = MspTextDelta | MspTokenUsage | MspTurnCompleted | MspApprovalRequested


def mint_command_id() -> str:
    """Mint a UUIDv7 command id (time-ordered, per the wire contract)."""
    millis = (time.time_ns() // 1_000_000) & 0xFFFFFFFFFFFF
    rand_a = random.getrandbits(12)
    rand_b = random.getrandbits(62)
    inten = (millis << 80) | (7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    hexed = f"{inten:032x}"
    return f"{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:]}"


NotificationHandler = Callable[[str, JsonObject], None]
ServerRequestHandler = Callable[[str, JsonObject], Awaitable[JsonObject]]


class MspClient:
    """One owned ``muse serve`` host plus a correlated view of its traffic.

    Create with :meth:`spawn`, which runs the ``initialize`` handshake.
    ``request`` / ``command`` send client-initiated traffic;
    ``subscribe`` taps the notification stream (fan-out: every subscriber
    sees every notification, so observers never starve each other).
    """

    # Instances are initialized by the async factory rather than __init__.
    _proc: asyncio.subprocess.Process
    _label: str
    _pending: dict[int, asyncio.Future[JsonObject]]
    _next_request_id: int
    _write_queue: asyncio.Queue[tuple[bytes, asyncio.Future[JsonObject] | None]]
    _subscribers: list[NotificationHandler]
    _server_request_handler: ServerRequestHandler | None
    _server_request_tasks: set[asyncio.Task[None]]
    _recent_stderr: deque[str]
    _closed: asyncio.Event
    _close_error: BaseException | None
    _did_close: bool
    fingerprint: str | None
    host_version: str | None
    _reader_task: asyncio.Task[None]
    _stderr_task: asyncio.Task[None]
    _writer_task: asyncio.Task[None]

    def __init__(self) -> None:
        raise TypeError("Use MspClient.spawn()")

    @classmethod
    async def _create(
        cls,
        proc: asyncio.subprocess.Process,
        *,
        init_timeout: float,
        client_name: str,
        client_version: str,
        client_title: str | None,
        requested_capabilities: Sequence[str],
        label: str,
    ) -> MspClient:
        self = cls.__new__(cls)
        self._proc = proc
        self._label = label
        self._pending = {}
        self._next_request_id = 0
        self._write_queue = asyncio.Queue()
        self._subscribers = []
        self._server_request_handler = None
        self._server_request_tasks = set()
        self._recent_stderr = deque(maxlen=_STDERR_RING_SIZE)
        self._closed = asyncio.Event()
        self._close_error = None
        self._did_close = False
        self.fingerprint = None
        self.host_version = None
        self._reader_task = asyncio.create_task(self._read_loop(), name="msp-stdout")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="msp-stderr")
        self._writer_task = asyncio.create_task(self._writer_loop(), name="msp-stdin")
        try:
            await self._initialize(
                init_timeout,
                client_name=client_name,
                client_version=client_version,
                client_title=client_title,
                requested_capabilities=requested_capabilities,
            )
        except BaseException:
            # Handshake failures must not orphan the child: the object is
            # fully built, so close() reaps it before the error propagates.
            await self.close()
            raise
        return self

    @classmethod
    async def spawn(
        cls,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        client_name: str = "omnigent_muse",
        client_version: str = "0.0.0",
        client_title: str | None = None,
        requested_capabilities: Sequence[str] = ("sessionMcp",),
        init_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        label: str = "muse",
    ) -> MspClient:
        """Spawn a host and run the ``initialize`` handshake.

        :param argv: Host argv, e.g. ``["muse", "serve"]``. Tests point this
            at a fake host; the binary is never resolved here.
        :param requested_capabilities: Grants to ask for; ``sessionMcp``
            is what lets ``session/start`` carry an ``mcpServers`` config.
        :raises MspProtocolError: The handshake frame was malformed.
        :raises MspConnectionClosed: The host died during the handshake.
        :raises ValueError: ``client_name`` is not a machine identifier.
        """
        if not _CLIENT_NAME_RE.match(client_name):
            raise ValueError(
                f"client_name {client_name!r} must match ^[a-z0-9_]+$ (host rule)"
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                argv[0],
                *argv[1:],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                limit=_STREAM_LIMIT,
                **_proc.spawn_kwargs(),
            )
        except OSError as exc:
            raise MspConnectionClosed(f"could not spawn {argv[0]!r}: {exc}") from exc
        return await cls._create(
            proc,
            init_timeout=init_timeout,
            client_name=client_name,
            client_version=client_version,
            client_title=client_title,
            requested_capabilities=requested_capabilities,
            label=label,
        )

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _initialize(
        self,
        timeout: float,
        *,
        client_name: str,
        client_version: str,
        client_title: str | None,
        requested_capabilities: Sequence[str],
    ) -> None:
        client_info: JsonObject = {"name": client_name, "version": client_version}
        if client_title is not None:
            client_info["title"] = client_title
        params: JsonObject = {"clientInfo": client_info}
        if requested_capabilities:
            params["capabilities"] = {
                "requestedCapabilities": list(requested_capabilities)
            }
        try:
            result = await self.request("initialize", params, timeout=timeout)
        except (TimeoutError, MspConnectionClosed, MspProtocolError) as exc:
            raise MspConnectionClosed(self._startup_error_message(exc)) from exc
        except MspError as exc:
            raise MspConnectionClosed(
                f"{self._label}: initialize rejected ({exc.kind}): {exc.message}"
            ) from exc
        schema_info = result.get("schemaInfo") or {}
        server_info = result.get("serverInfo") or {}
        self.fingerprint = schema_info.get("fingerprint")
        self.host_version = server_info.get("version")
        logger.info(
            "%s: host version %s fingerprint %s",
            self._label,
            self.host_version,
            self.fingerprint,
        )
        self.notify("initialized")

    def _startup_error_message(self, exc: BaseException) -> str:
        detail = f"{self._label}: initialize failed ({exc})"
        if self._recent_stderr:
            tail = " | ".join(list(self._recent_stderr)[-_STDERR_QUOTED_LINES:])
            if len(tail) > _STDERR_QUOTED_LIMIT:
                tail = tail[:_STDERR_QUOTED_LIMIT] + "...[truncated]"
            detail = f"{detail}; host stderr: {tail}"
        return detail

    # ------------------------------------------------------------------
    # Low-level traffic
    # ------------------------------------------------------------------

    def subscribe(self, handler: NotificationHandler) -> Callable[[], None]:
        """Tap every inbound notification; returns an unsubscribe callable."""
        self._subscribers.append(handler)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(handler)
            except ValueError:
                pass

        return _unsubscribe

    def set_server_request_handler(self, handler: ServerRequestHandler) -> None:
        """Handle host-initiated requests (default answers method-not-found)."""
        self._server_request_handler = handler

    @property
    def closed(self) -> bool:
        """Whether the host is gone and no further traffic is possible."""
        return self._closed.is_set()

    async def wait_closed(self) -> None:
        """Wait until the host is gone (EOF or :meth:`close`)."""
        await self._closed.wait()

    async def request(
        self,
        method: str,
        params: JsonObject | None = None,
        *,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> JsonObject:
        """Send one request and await its ``result`` object.

        :raises MspError: The host answered with an ``error`` object.
        :raises MspProtocolError: The response frame was malformed.
        :raises MspConnectionClosed: The host died waiting.
        :raises TimeoutError: No answer within ``timeout`` seconds.
        """
        if self._closed.is_set():
            raise MspConnectionClosed(f"{self._label}: connection is closed")
        request_id, future = self._request_raw(method, params or {})
        try:
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as exc:
            raise TimeoutError(
                f"{self._label}: no answer to {method} within {timeout:g}s"
            ) from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    def _request_raw(
        self, method: str, params: JsonObject
    ) -> tuple[int, asyncio.Future[JsonObject]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[JsonObject] = loop.create_future()
        self._next_request_id += 1
        request_id = self._next_request_id
        self._pending[request_id] = future
        frame: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params:
            frame["params"] = params
        self._enqueue_write(frame, future)
        return request_id, future

    async def command(
        self,
        method: str,
        params: JsonObject,
        *,
        command_id: str | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        max_attempts: int = _DEFAULT_COMMAND_ATTEMPTS,
    ) -> JsonObject:
        """Send one idempotent command (same ``commandId`` across retries).

        Retries the bounded attempt budget when the host reports
        an explicit ``retryable=True`` flag. The backpressure kinds
        (``backpressured`` / ``overloaded``) are a fallback only when the
        flag is absent or not boolean; explicit ``False`` never retries.
        """
        command_params = dict(params)
        command_params["commandId"] = command_id or mint_command_id()
        attempts = max(1, max_attempts)
        last_error: MspError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self.request(method, command_params, timeout=timeout)
            except MspError as exc:
                last_error = exc
                retryable = (
                    exc.retryable
                    if exc.retryable is not None
                    else exc.kind in _COMMAND_RETRYABLE_KINDS
                )
                if not retryable or attempt == attempts:
                    raise
                logger.debug(
                    "%s: %s backpressured (%s), retry %d/%d",
                    self._label,
                    method,
                    exc.kind,
                    attempt,
                    attempts,
                )
                await asyncio.sleep(_COMMAND_RETRY_BASE_DELAY * attempt)
        assert last_error is not None  # attempts >= 1 always runs once
        raise last_error

    def notify(self, method: str, params: JsonObject | None = None) -> None:
        """Fire one notification (no response is ever expected)."""
        frame: JsonObject = {"jsonrpc": "2.0", "method": method}
        if params:
            frame["params"] = params
        self._enqueue_write(frame)

    def _enqueue_write(
        self, frame: JsonObject, future: asyncio.Future[JsonObject] | None = None
    ) -> None:
        if self._closed.is_set():
            error = MspConnectionClosed(f"{self._label}: connection is closed")
            if future is not None and not future.done():
                future.set_exception(error)
            return
        try:
            encoded = (json.dumps(frame) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            error = MspProtocolError(f"outbound frame is not JSON-encodable: {exc}")
            if future is not None and not future.done():
                future.set_exception(error)
            return
        self._write_queue.put_nowait((encoded, future))

    async def flush(self, timeout: float = _DEFAULT_REQUEST_TIMEOUT) -> None:
        """Wait until all enqueued bytes reached the host's stdin."""
        async with asyncio.timeout(timeout):
            await self._write_queue.join()

    async def _writer_loop(self) -> None:
        """Write outbound frames strictly in enqueue order."""
        try:
            while True:
                encoded, future = await self._write_queue.get()
                try:
                    await self._write_once(encoded)
                except BaseException as exc:
                    # Writer cancellation belongs to the transport, not to
                    # callers waiting for a response to an in-flight write.
                    error = (
                        MspConnectionClosed(f"{self._label}: connection is closed")
                        if isinstance(exc, asyncio.CancelledError)
                        else exc
                    )
                    if future is not None and not future.done():
                        future.set_exception(error)
                    self._finish(error)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return
                finally:
                    self._write_queue.task_done()
        except asyncio.CancelledError:
            self._finish(MspConnectionClosed(f"{self._label}: connection is closed"))

    async def _write_once(self, encoded: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._closed.is_set():
            raise MspConnectionClosed(f"{self._label}: connection is closed")
        try:
            proc.stdin.write(encoded)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise MspConnectionClosed(f"{self._label}: host went away: {exc}") from exc

    # ------------------------------------------------------------------
    # Read pumps
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Route stdout frames: responses settle futures, the rest fan out."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    self._finish(
                        MspConnectionClosed(f"{self._label}: host closed stdout")
                    )
                    return
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "%s: non-JSON stdout line: %r", self._label, line[:200]
                    )
                    continue
                if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
                    logger.warning(
                        "%s: dropping non-2.0 frame: %r", self._label, line[:200]
                    )
                    continue
                self._route_frame(frame, line)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # The loop must fail futures, never die silent.
            logger.exception("%s: stdout reader error", self._label)
            self._finish(exc)

    def _route_frame(self, frame: JsonObject, line: str) -> None:
        if self.closed:
            return
        method = frame.get("method")
        frame_id = frame.get("id")
        if isinstance(method, str):
            if isinstance(frame_id, int) and not isinstance(frame_id, bool):
                task = asyncio.get_running_loop().create_task(
                    self._answer_server_request(
                        frame_id, method, frame.get("params") or {}
                    )
                )
                self._server_request_tasks.add(task)
                task.add_done_callback(self._log_task_error)
                return
            params = frame.get("params")
            self._fan_out(method, params if isinstance(params, dict) else {})
            return
        if not isinstance(frame_id, int) or isinstance(frame_id, bool):
            logger.warning("%s: response has no usable id: %r", self._label, line[:200])
            return
        future = self._pending.pop(frame_id, None)
        if future is None:
            logger.warning(
                "%s: response for unknown request id %r", self._label, frame_id
            )
            return
        if future.done():
            return  # Caller timed out or cancelled; the late reply is dropped.
        has_result = "result" in frame
        has_error = "error" in frame
        if has_result == has_error:
            future.set_exception(
                MspProtocolError("response must carry exactly one of result or error")
            )
            return
        if has_result:
            result = frame.get("result")
            if not isinstance(result, dict):
                future.set_exception(
                    MspProtocolError("response result must be an object")
                )
                return
            future.set_result(result)
            return
        error = frame.get("error")
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), int)
            or isinstance(error.get("code"), bool)
            or not isinstance(error.get("message"), str)
        ):
            future.set_exception(MspProtocolError("error response is malformed"))
            return
        data = error.get("data")
        data_dict = data if isinstance(data, dict) else {}
        kind = data_dict.get("kind")
        future.set_exception(
            MspError(
                code=error["code"],
                message=error["message"],
                kind=kind if isinstance(kind, str) else "unknown",
                data=data_dict,
            )
        )

    def _fan_out(self, method: str, params: JsonObject) -> None:
        for handler in list(self._subscribers):
            try:
                handler(method, params)
            except Exception:  # One bad subscriber must not starve others.
                logger.exception("%s: notification subscriber failed", self._label)

    async def _answer_server_request(
        self, request_id: int, method: str, params: JsonObject
    ) -> None:
        if self._server_request_handler is None:
            response: JsonObject = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"no handler for {method}",
                    "data": {"kind": "method_not_found"},
                },
            }
        else:
            try:
                result = await self._server_request_handler(method, params)
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except MspError as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "data": {"kind": exc.kind, **exc.data},
                    },
                }
            except Exception as exc:  # noqa: BLE001 — must answer, never hang the host
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32603,
                        "message": str(exc),
                        "data": {"kind": "internal_error"},
                    },
                }
        self._enqueue_write(response)

    def _log_task_error(self, done: asyncio.Task[None]) -> None:
        self._server_request_tasks.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.exception("%s: server-request task failed: %s", self._label, error)

    async def _stderr_loop(self) -> None:
        """Drain stderr so a chatty host can't stall on a full pipe buffer."""
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            while True:
                raw_line = await proc.stderr.readline()
                if not raw_line:
                    return
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    if len(line) > _STDERR_LINE_LIMIT:
                        line = line[:_STDERR_LINE_LIMIT] + "...[truncated]"
                    self._recent_stderr.append(line)
                    logger.debug("%s stderr: %s", self._label, line)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — diagnostics only; never fatal
            logger.debug("%s: stderr reader stopped: %s", self._label, exc)

    def _finish(self, error: BaseException) -> None:
        if self._closed.is_set():
            return
        self._close_error = error
        self._closed.set()
        caller = asyncio.current_task()
        for task in tuple(self._server_request_tasks):
            if task is not caller and not task.done() and not task.cancelling():
                task.cancel()
        pending = tuple(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)
        # Balance the queue so a concurrent flush() can't hang forever.
        while True:
            try:
                self._write_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._write_queue.task_done()

    async def close(self) -> None:
        """Shut the host down: stdin EOF first, then terminate, then kill.

        Never orphans the child: every path ends with the process reaped.
        Idempotent.
        """
        if self._did_close:
            return
        self._did_close = True
        self._finish(MspConnectionClosed(f"{self._label}: client closed"))
        proc = self._proc
        self._writer_task.cancel()
        try:
            if proc is not None and proc.returncode is None and proc.stdin is not None:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    proc.stdin.close()
                try:
                    async with asyncio.timeout(30):
                        await proc.wait()
                except TimeoutError:
                    pass
            if proc is not None and proc.returncode is None:
                _proc.terminate_tree(proc, grace=5)
                try:
                    async with asyncio.timeout(10):
                        await proc.wait()
                except TimeoutError:
                    _proc.kill_tree(proc)
                    with contextlib.suppress(OSError):
                        await proc.wait()
        finally:
            self._finish(MspConnectionClosed(f"{self._label}: client closed"))
            caller = asyncio.current_task()
            tasks = (
                self._reader_task,
                self._stderr_task,
                self._writer_task,
                *(task for task in self._server_request_tasks if task is not caller),
            )
            for task in tasks:
                # A handler may already be awaiting its cancellation cleanup.
                # Cancelling it again would interrupt that cleanup.
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._server_request_tasks.clear()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Typed session / turn helpers
    # ------------------------------------------------------------------

    async def start_session(
        self,
        *,
        provider_id: str | None = None,
        workspace_root: str | None = None,
        approval_mode: str | None = None,
        model_id: str | None = None,
        mcp_servers: dict[str, JsonObject] | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> JsonObject:
        """Start a session; returns the host's session object.

        ``mcp_servers`` adds native MCP servers to this session only
        (requires the ``sessionMcp`` capability grant from :meth:`spawn`).
        """
        params: JsonObject = {}
        if provider_id is not None:
            params["providerId"] = provider_id
        if workspace_root is not None:
            params["workspaceRoot"] = workspace_root
        if approval_mode is not None:
            params["approvalMode"] = approval_mode
        if model_id is not None:
            params["modelId"] = model_id
        if mcp_servers:
            params["config"] = {"mcpServers": mcp_servers}
        result = await self.command("session/start", params, timeout=timeout)
        session = result.get("session")
        if not isinstance(session, dict):
            raise MspProtocolError("session/start result has no session object")
        return session

    async def resume_session(
        self,
        session_id: str,
        *,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> JsonObject:
        """Resume a durable session; returns the host's session object."""
        result = await self.command(
            "session/resume", {"sessionId": session_id}, timeout=timeout
        )
        session = result.get("session")
        if not isinstance(session, dict):
            raise MspProtocolError("session/resume result has no session object")
        return session

    async def send_turn(
        self,
        session_id: str,
        parts: Sequence[JsonObject],
        *,
        reasoning_effort: str | None = None,
        if_busy: str | None = None,
        display_text: str | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> str:
        """Submit a turn; returns the turn id (``== commandId`` for fresh turns).

        ``parts`` are wire input parts, e.g. ``[{"type": "text", "text": ...}]``
        or ``[{"type": "image", "base64Data": ..., "mediaType": ...}]``.
        """
        params: JsonObject = {"sessionId": session_id, "input": list(parts)}
        if reasoning_effort is not None:
            params["reasoningEffort"] = reasoning_effort
        if if_busy is not None:
            params["ifBusy"] = if_busy
        if display_text is not None:
            params["displayText"] = display_text
        command_id = mint_command_id()
        await self.command("turn/start", params, command_id=command_id, timeout=timeout)
        return command_id

    def open_stream(self, session_id: str) -> TurnStream:
        """Subscribe to a session's notification stream.

        Subscribe *before* submitting the turn — the host emits deltas
        immediately, so subscribing after ``send_turn`` returns can miss
        the opening events.
        """
        return TurnStream(self, session_id)

    @staticmethod
    def _translate_notification(
        session_id: str, turn_id: str, method: str, params: JsonObject
    ) -> MspEvent | None:
        if params.get("sessionId") != session_id or params.get("turnId") != turn_id:
            return None
        if method == "item/delta":
            delta = params.get("delta")
            if not isinstance(delta, str) or not delta:
                return None
            item_id = params.get("itemId")
            field_name = params.get("field")
            return MspTextDelta(
                delta=delta,
                item_id=item_id if isinstance(item_id, str) else "",
                field=field_name if isinstance(field_name, str) else None,
            )
        if method == "session/tokenUsage":
            return MspTokenUsage(
                session_id=session_id,
                prompt_tokens=_as_int(params.get("promptTokens")),
                output_tokens=_as_int(params.get("outputTokens")),
                total_tokens=_as_int(params.get("totalTokens")),
                cumulative=_as_int_map(params.get("cumulative")),
                duration_ms=_as_int(params.get("durationMs")),
                model_id=params.get("modelId")
                if isinstance(params.get("modelId"), str)
                else None,
            )
        if method == "turn/completed":
            usage = params.get("usage")
            error = params.get("error") if isinstance(params.get("error"), dict) else {}
            retryable = error.get("retryable")
            return MspTurnCompleted(
                session_id=session_id,
                turn_id=params.get("turnId")
                if isinstance(params.get("turnId"), str)
                else turn_id,
                duration_ms=_as_int(params.get("durationMs")),
                usage=_as_int_map(usage),
                error_kind=error.get("kind")
                if isinstance(error.get("kind"), str)
                else None,
                error_message=error.get("message")
                if isinstance(error.get("message"), str)
                else None,
                error_retryable=retryable if isinstance(retryable, bool) else None,
            )
        if method == "turn/retracted":
            return MspTurnCompleted(
                session_id=session_id,
                turn_id=turn_id,
                error_kind="retracted",
                error_message="turn retracted",
            )
        if method == "approval/requested":
            approval = params.get("approval")
            approval_id = params.get("approvalId")
            if isinstance(approval, dict):
                approval_id = approval.get("approvalId", approval_id)
            if not isinstance(approval_id, str):
                return None
            return MspApprovalRequested(
                session_id=session_id, approval_id=approval_id, raw=dict(params)
            )
        return None

    async def interrupt_turn(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> JsonObject:
        """Interrupt the running turn (no-op shape when nothing runs)."""
        params: JsonObject = {"sessionId": session_id}
        if turn_id is not None:
            params["turnId"] = turn_id
        return await self.command("turn/interrupt", params, timeout=timeout)

    async def steer_turn(
        self,
        session_id: str,
        turn_id: str,
        parts: Sequence[JsonObject],
        *,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> JsonObject:
        """Append steering input to the running turn."""
        return await self.command(
            "turn/steer",
            {"sessionId": session_id, "expectedTurnId": turn_id, "input": list(parts)},
            timeout=timeout,
        )

    async def cancel_turn(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> JsonObject:
        """Cancel a queued (not yet running) turn."""
        params: JsonObject = {"sessionId": session_id}
        if turn_id is not None:
            params["turnId"] = turn_id
        return await self.command("turn/cancel", params, timeout=timeout)

    async def list_approvals(
        self, session_id: str, *, timeout: float = _DEFAULT_REQUEST_TIMEOUT
    ) -> JsonObject:
        """List pending approvals (and user inputs) for a session."""
        return await self.request(
            "approval/listPending", {"sessionId": session_id}, timeout=timeout
        )

    async def decide_approval(
        self,
        session_id: str,
        approval_id: str,
        choice_id: str,
        requirement_id: str,
        *,
        feedback: str | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> JsonObject:
        """Resolve one pending approval with the given choice."""
        params: JsonObject = {
            "sessionId": session_id,
            "approvalId": approval_id,
            "choiceId": choice_id,
            "requirementId": requirement_id,
        }
        if feedback is not None:
            params["feedback"] = feedback
        return await self.command("approval/decide", params, timeout=timeout)

    async def read_usage(
        self, session_id: str, *, timeout: float = _DEFAULT_REQUEST_TIMEOUT
    ) -> JsonObject:
        """Read subscription/quota usage for a session."""
        return await self.request(
            "usage/read", {"sessionId": session_id}, timeout=timeout
        )

    async def compact_session(
        self, session_id: str, *, timeout: float = _DEFAULT_REQUEST_TIMEOUT
    ) -> JsonObject:
        """Compact a session's context (answer may be ``"noop"``)."""
        return await self.command(
            "session/compact", {"sessionId": session_id}, timeout=timeout
        )

    async def set_model(
        self, session_id: str, model: str, *, timeout: float = _DEFAULT_REQUEST_TIMEOUT
    ) -> JsonObject:
        """Switch a session's model mid-conversation."""
        return await self.command(
            "session/setModel",
            {"sessionId": session_id, "model": model},
            timeout=timeout,
        )

    async def set_reasoning_effort(
        self, session_id: str, effort: str, *, timeout: float = _DEFAULT_REQUEST_TIMEOUT
    ) -> JsonObject:
        """Set a session-wide reasoning-effort default."""
        return await self.command(
            "session/setReasoningEffort",
            {"sessionId": session_id, "reasoningEffort": effort},
            timeout=timeout,
        )

    async def fork_session(
        self, session_id: str, *, timeout: float = _DEFAULT_REQUEST_TIMEOUT
    ) -> JsonObject:
        """Fork a session; returns the fork's session object."""
        result = await self.command(
            "session/fork", {"sessionId": session_id}, timeout=timeout
        )
        session = result.get("session")
        if not isinstance(session, dict):
            raise MspProtocolError("session/fork result has no session object")
        return session


class TurnStream:
    """A session-scoped live subscription, opened before the turn is sent.

    Use as a context manager around submit + follow::

        with client.open_stream(session_id) as stream:
            turn_id = await client.send_turn(session_id, parts)
            async for event in stream.follow(turn_id):
                ...
    """

    def __init__(self, client: MspClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._queue: asyncio.Queue[tuple[str, JsonObject]] = asyncio.Queue()
        self._item_turns: dict[str, str] = {}
        self._unsubscribe = client.subscribe(self._tap)
        self._closed = False

    def _tap(self, method: str, params: JsonObject) -> None:
        if params.get("sessionId") != self._session_id:
            return
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if isinstance(item, dict):
                item_id = item.get("itemId")
                turn_id = item.get("turnId")
                if isinstance(item_id, str):
                    if method == "item/completed":
                        self._item_turns.pop(item_id, None)
                    elif isinstance(turn_id, str):
                        self._item_turns[item_id] = turn_id
            return
        if method == "item/delta":
            item_id = params.get("itemId")
            turn_id = params.get("turnId")
            if not isinstance(turn_id, str):
                turn_id = (
                    self._item_turns.get(item_id) if isinstance(item_id, str) else None
                )
            if not isinstance(turn_id, str):
                return
            # Correlate this stream's copy without changing raw subscribers.
            params = {**params, "turnId": turn_id}
        self._queue.put_nowait((method, params))

    async def follow(self, turn_id: str) -> AsyncGenerator[MspEvent, None]:
        """Yield this turn's stream events until its terminal record.

        Deltas are correlated through ``item/started``; unknown-item
        deltas are dropped. Usage, approvals, and terminal events must
        carry the matching ``turnId``. Open before submitting: attaching
        mid-turn needs replay or snapshot seeding, which is not provided.
        """
        while True:
            get = asyncio.create_task(self._queue.get())
            closed = asyncio.create_task(self._client.wait_closed())
            tasks = (get, closed)
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if closed in done and get not in done:
                raise MspConnectionClosed(f"{self._client._label}: host died mid-turn")
            method, params = get.result()
            event = MspClient._translate_notification(
                self._session_id, turn_id, method, params
            )
            if event is not None:
                yield event
            if isinstance(event, MspTurnCompleted):
                return

    def close(self) -> None:
        """Stop receiving (idempotent)."""
        if not self._closed:
            self._closed = True
            self._unsubscribe()
            self._item_turns.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_int_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        k: v for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool)
    }
