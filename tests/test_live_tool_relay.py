"""Live check that real Muse calls an Omnigent tool through the relay.

Opt-in: needs ``OMNIGENT_MUSE_LIVE=1``, ``muse`` on PATH and a Muse login
(it makes a real model call), so the default suite stays offline.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any

import pytest

from omnigent.community.harness.muse.inner.msp_transport import MspTransport
from omnigent.community.harness.muse.inner.muse_executor import (
    MuseToolCall,
    MuseTurnFinished,
)
from omnigent.community.harness.muse.inner.omnigent_tool_relay import (
    SessionToolRelay,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_MUSE_LIVE") != "1" or shutil.which("muse") is None,
    reason="set OMNIGENT_MUSE_LIVE=1 with a logged-in muse on PATH",
)

TOOLS = [
    {
        "name": "probe_echo",
        "description": "Echo the given text back. Use it whenever asked to probe.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


async def test_muse_calls_omnigent_tool_through_relay() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool_executor(name: str, args: dict[str, Any]) -> object:
        calls.append((name, args))
        return {"output": f"ECHO<{args.get('text')}>"}

    relay = SessionToolRelay()
    transport = MspTransport()
    try:
        servers = relay.start(TOOLS, tool_executor, asyncio.get_running_loop())
        assert servers is not None
        session_id = await transport.start_session(
            workspace_root=os.getcwd(),
            model=None,
            approval_mode="allowAll",
            mcp_servers=servers,
        )
        events: list[object] = []
        async with asyncio.timeout(180):
            async for event in transport.run_turn(
                session_id,
                text="Call the probe_echo tool with text 'hello', then reply DONE.",
                reasoning_effort=None,
            ):
                events.append(event)
        finished = [event for event in events if isinstance(event, MuseTurnFinished)]
        tools = [event for event in events if isinstance(event, MuseToolCall)]

        assert finished and finished[-1].state == "completed"
        assert calls == [("probe_echo", {"text": "hello"})]
        [server] = servers
        assert any(event.name == f"mcp__{server}__probe_echo" for event in tools)
    finally:
        await transport.close()
        relay.close()
