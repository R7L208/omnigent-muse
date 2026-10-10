"""Session-scoped MCP relay that exposes Omnigent tools to Muse.

Reuses core's Omnigent tool relay, the one the ACP harnesses share: a
localhost HTTP relay forwards each call into the executor's tool bridge, and
Muse launches core's ``serve-mcp`` stdio server (named in ``session/start``
``config.mcpServers``) to reach it. Omnigent policy runs when the bridge
dispatches the call, not here.

The relay is additive: when it cannot start, the session runs without
Omnigent tools rather than failing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.harnesses.claude_native.bridge import ClaudeNativeToolRelay

logger = logging.getLogger(__name__)

type JsonObject = dict[str, Any]
type ToolExecutor = Callable[..., Awaitable[object]]


class SessionToolRelay:
    """One Muse session's Omnigent tool relay and its MSP server entry.

    :param sandboxed: Whether Muse runs inside the OS sandbox, which hides the
        relay's bridge directory from ``serve-mcp``; no relay is started then.
    """

    def __init__(self, *, sandboxed: bool = False) -> None:
        self._sandboxed = sandboxed
        self._relay: ClaudeNativeToolRelay | None = None
        self._bridge_dir: Path | None = None
        self._prefix: str | None = None
        self._tool_names: frozenset[str] = frozenset()

    def start(
        self,
        tools: Sequence[JsonObject],
        tool_executor: ToolExecutor | None,
        loop: asyncio.AbstractEventLoop,
    ) -> dict[str, JsonObject] | None:
        """Start the relay; return ``session/start`` ``mcpServers``, or ``None``.

        :param tools: Flat Omnigent tool specs (``name``, ``description``,
            ``parameters``) to advertise.
        :param tool_executor: The adapter's tool bridge, or ``None`` when no
            adapter installed one.
        :param loop: The running loop that owns ``tool_executor``.
        """
        if not tools or tool_executor is None:
            return None
        if self._sandboxed:
            logger.info(
                "Omnigent tools are not yet exposed to sandboxed Muse sessions; "
                "the session runs with Muse's built-in tools only"
            )
            return None
        try:
            from omnigent.harnesses.claude_native import bridge

            self._bridge_dir = bridge.prepare_acp_mcp_bridge_dir()
            self._relay = bridge.start_tool_relay(
                bridge_dir=self._bridge_dir,
                tools=list(tools),
                tool_executor=tool_executor,
                loop=loop,
            )
            servers = bridge.build_mcp_config(self._bridge_dir)["mcpServers"]
            if not isinstance(servers, dict):
                raise TypeError(f"unexpected serve-mcp config: {servers!r}")
            [(name, spec)] = servers.items()
            if not isinstance(name, str) or not isinstance(spec, dict):
                raise TypeError(f"unexpected serve-mcp entry: {name!r}")
            entry: JsonObject = {
                "transport": "stdio",
                "command": spec["command"],
                "args": list(spec.get("args", [])),
                "env": dict(spec.get("env") or {}),
                "mode": "optional",
            }
        except Exception as exc:  # noqa: BLE001 - the relay is additive
            logger.warning(
                "Omnigent tool relay setup failed; Muse runs without Omnigent "
                "tools: %s",
                exc,
            )
            self.close()
            return None
        self._prefix = f"mcp__{name}__"
        self._tool_names = frozenset(
            str(tool["name"]) for tool in tools if tool.get("name")
        )
        return {name: entry}

    def is_relayed(self, tool_name: str) -> bool:
        """Whether Muse's ``tool_name`` is a tool this relay advertised."""
        if self._prefix is None or not tool_name.startswith(self._prefix):
            return False
        return tool_name[len(self._prefix) :] in self._tool_names

    def close(self) -> None:
        """Stop the relay and remove its bridge directory (idempotent)."""
        relay, self._relay = self._relay, None
        bridge_dir, self._bridge_dir = self._bridge_dir, None
        self._prefix = None
        self._tool_names = frozenset()
        if relay is not None:
            with contextlib.suppress(Exception):
                relay.close()
        if bridge_dir is not None:
            # The relay removes only its advertisement, not the directory.
            shutil.rmtree(bridge_dir, ignore_errors=True)
