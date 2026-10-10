"""SessionToolRelay: Omnigent tools exposed to Muse over core's MCP relay."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from omnigent.harnesses.claude_native import bridge

from omnigent.community.harness.muse.inner.omnigent_tool_relay import (
    SessionToolRelay,
)

TOOLS = [
    {
        "name": "web_fetch",
        "description": "Fetch a URL.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
    }
]


async def _tool_executor(name: str, args: dict[str, Any]) -> object:
    return {"output": "ok"}


def _bridge_dir(servers: dict[str, dict[str, Any]]) -> Path:
    [entry] = servers.values()
    args = entry["args"]
    return Path(args[args.index("--bridge-dir") + 1])


async def test_start_returns_optional_stdio_server_for_serve_mcp() -> None:
    relay = SessionToolRelay()
    try:
        servers = relay.start(TOOLS, _tool_executor, asyncio.get_running_loop())

        assert servers is not None
        [(name, entry)] = servers.items()
        assert name == "omnigent"
        # Muse's session/start schema: "transport", never "type"; "mode":
        # "optional" keeps a failed server from failing every turn.
        assert set(entry) == {"transport", "command", "args", "env", "mode"}
        assert entry["transport"] == "stdio"
        assert entry["mode"] == "optional"
        assert entry["args"][:4] == [
            "-I",
            "-m",
            "omnigent.harnesses.claude_native.bridge",
            "serve-mcp",
        ]
        assert (_bridge_dir(servers) / "tool_relay.json").is_file()
    finally:
        relay.close()


@pytest.mark.parametrize(
    ("tools", "executor", "sandboxed"),
    [
        ([], _tool_executor, False),
        (TOOLS, None, False),
        (TOOLS, _tool_executor, True),
    ],
    ids=["no-tools", "no-tool-executor", "sandboxed"],
)
async def test_start_skips_relay_when_it_cannot_serve(
    monkeypatch: pytest.MonkeyPatch,
    tools: list[dict[str, Any]],
    executor: Any,
    sandboxed: bool,
) -> None:
    def unexpected() -> Path:
        raise AssertionError("no bridge dir may be created")

    monkeypatch.setattr(bridge, "prepare_acp_mcp_bridge_dir", unexpected)
    relay = SessionToolRelay(sandboxed=sandboxed)

    assert relay.start(tools, executor, asyncio.get_running_loop()) is None
    assert not relay.is_relayed("mcp__omnigent__web_fetch")


async def test_setup_failure_returns_none_and_removes_bridge_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[Path] = []
    prepare = bridge.prepare_acp_mcp_bridge_dir

    def recording_prepare() -> Path:
        created.append(prepare())
        return created[-1]

    def broken_relay(**kwargs: object) -> object:
        raise OSError("no free port")

    monkeypatch.setattr(bridge, "prepare_acp_mcp_bridge_dir", recording_prepare)
    monkeypatch.setattr(bridge, "start_tool_relay", broken_relay)
    relay = SessionToolRelay()

    assert relay.start(TOOLS, _tool_executor, asyncio.get_running_loop()) is None
    assert len(created) == 1
    assert not created[0].exists()
    assert not relay.is_relayed("mcp__omnigent__web_fetch")


async def test_close_removes_bridge_dir_and_is_idempotent() -> None:
    relay = SessionToolRelay()
    servers = relay.start(TOOLS, _tool_executor, asyncio.get_running_loop())
    assert servers is not None
    bridge_dir = _bridge_dir(servers)

    relay.close()
    relay.close()

    assert not bridge_dir.exists()
    assert not relay.is_relayed("mcp__omnigent__web_fetch")


async def test_is_relayed_matches_only_advertised_tools_under_the_relay_prefix() -> (
    None
):
    relay = SessionToolRelay()
    try:
        relay.start(TOOLS, _tool_executor, asyncio.get_running_loop())

        assert relay.is_relayed("mcp__omnigent__web_fetch")
        # A project .mcp.json server that is also named "omnigent" must not
        # inherit the relay's automatic approval.
        assert not relay.is_relayed("mcp__omnigent__delete_everything")
        assert not relay.is_relayed("web_fetch")
        assert not relay.is_relayed("mcp__other__web_fetch")
    finally:
        relay.close()
