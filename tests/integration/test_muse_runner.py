"""End-to-end Muse harness tests through Omnigent's harness runner.

Each test spawns the ``muse`` harness with Omnigent's ``HarnessProcessManager``
(the runner's per-conversation process manager), which starts the shared
``_runner`` subprocess, which in turn spawns a real ``muse serve``. Turns go
over the harness HTTP contract and stream back as SSE.

The tests are opt-in: they run only when ``OMNIGENT_MUSE_INTEGRATION=1``, and
then require a ``muse`` binary at ``OMNIGENT_MUSE_PATH`` (or on ``PATH``). They
never import the plugin's modules; Omnigent discovers the installed plugin
through its entry point, exactly as in production. Provider credentials are
never needed: turns use the ``echo`` provider, or ``meta`` without a login for
the failure path.

Muse's ``echo`` provider only echoes the prompt: it never calls tools,
requests approvals, or runs long enough to interrupt, and its ``local``
provider runs on-device models rather than an endpoint a mock could serve.
Approval round trips and interrupts are therefore covered by the scripted MSP
host in the unit suite, not here (see docs/integration-tests.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_MUSE_INTEGRATION") != "1",
    reason="set OMNIGENT_MUSE_INTEGRATION=1 to run",
)

CONVERSATION = "conv_muse_integration"
TURN_TIMEOUT_S = 60.0


@pytest.fixture(scope="session")
def muse_binary() -> str:
    # Resolve the way the plugin does: OMNIGENT_MUSE_PATH, else `muse`, either
    # of which may be a bare command name looked up on PATH.
    configured = os.environ.get("OMNIGENT_MUSE_PATH")
    binary = shutil.which(configured or "muse")
    if binary is None:
        # Fail rather than skip: an enabled run without Muse must not pass.
        source = f"OMNIGENT_MUSE_PATH={configured!r}" if configured else "PATH"
        pytest.fail(f"no executable muse binary found via {source}")
    return os.path.abspath(binary)


@dataclass
class Turn:
    events: list[dict[str, Any]]

    @property
    def types(self) -> list[str]:
        return [event["type"] for event in self.events]

    @property
    def text(self) -> str:
        return "".join(
            event["delta"]
            for event in self.events
            if event["type"] == "response.output_text.delta"
        )

    @property
    def terminal(self) -> dict[str, Any]:
        return self.events[-1]


@dataclass
class MuseHarness:
    """One Omnigent-managed Muse harness process for a single conversation."""

    client: httpx.AsyncClient

    async def turn(self, text: str) -> Turn:
        events: list[dict[str, Any]] = []
        # Bound the whole turn: Omnigent heartbeats every 15s, so a per-read
        # timeout alone never fires on a stuck turn.
        async with asyncio.timeout(TURN_TIMEOUT_S):
            async with self.client.stream(
                "POST",
                f"/v1/sessions/{CONVERSATION}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "content": text,
                    "model": "muse",
                },
                timeout=TURN_TIMEOUT_S,
            ) as response:
                assert response.status_code == 200, await response.aread()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line.removeprefix("data: ")))
        return Turn(events)

    def muse_pids(self) -> list[int]:
        """PIDs of ``muse serve`` processes descended from this test process.

        Matched by program name rather than path: the installer's ``muse``
        launcher execs a versioned ``muse-bin-<version>`` binary.
        """
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        parents: dict[int, int] = {}
        serving: list[int] = []
        for line in listing.splitlines():
            pid, ppid, *argv = line.split()
            parents[int(pid)] = int(ppid)
            if (
                len(argv) > 1
                and os.path.basename(argv[0]).startswith("muse")
                and argv[1] == "serve"
            ):
                serving.append(int(pid))

        def ours(pid: int) -> bool:
            while pid > 1 and pid in parents:
                if pid == os.getpid():
                    return True
                pid = parents[pid]
            return False

        return [pid for pid in serving if ours(pid)]


@contextlib.asynccontextmanager
async def _start_harness(binary: str, provider: str) -> AsyncIterator[MuseHarness]:
    # Short, symlink-free root: the harness binds a Unix socket below it
    # (sun_path limit), and Muse refuses state roots reached through symlinks.
    root = Path(tempfile.mkdtemp(prefix="muse-it-", dir="/tmp")).resolve()
    manager = HarnessProcessManager(tmp_parent=root / "h")
    try:
        workspace, home = root / "workspace", root / "home"
        workspace.mkdir()
        home.mkdir()
        await manager.start(sweep_orphans=False)
        env = {
            "HARNESS_MUSE_PROVIDER": provider,
            "HARNESS_MUSE_CWD": str(workspace),
            "OMNIGENT_MUSE_PATH": binary,
            # Read only by the installer's `muse` launcher, which would
            # otherwise update itself mid-run. The plugin's deny-by-default
            # env drops it unless it is passed through explicitly.
            "MUSE_NO_AUTO_UPDATE": "1",
            "HARNESS_MUSE_ENV_PASSTHROUGH": "MUSE_NO_AUTO_UPDATE",
            # A private home keeps a file-stored developer Muse login out of
            # the run (see docs/integration-tests.md for macOS Keychain).
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "META_API_KEY": "",
        }
        client = await manager.get_client(CONVERSATION, "muse", env=env)
        yield MuseHarness(client=client)
    finally:
        # A no-op when start() never ran or failed.
        await manager.shutdown()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def echo_harness(muse_binary: str) -> AsyncIterator[MuseHarness]:
    async with _start_harness(muse_binary, "echo") as harness:
        yield harness


@pytest.fixture
async def unauthenticated_meta_harness(
    muse_binary: str,
) -> AsyncIterator[MuseHarness]:
    async with _start_harness(muse_binary, "meta") as harness:
        yield harness


def _assert_lifecycle(types: list[str], terminal: str) -> None:
    assert types[:2] == ["response.created", "response.in_progress"]
    assert types[-1] == terminal


async def test_spawn_handshake_and_first_turn(echo_harness: MuseHarness) -> None:
    turn = await echo_harness.turn("hello from omnigent")

    _assert_lifecycle(turn.types, "response.completed")
    assert turn.text == "echo: hello from omnigent"
    assert len(echo_harness.muse_pids()) == 1


async def test_multi_turn_streams_on_one_session(echo_harness: MuseHarness) -> None:
    first = await echo_harness.turn("first")
    pids = echo_harness.muse_pids()
    second = await echo_harness.turn("second")

    assert first.text == "echo: first"
    assert second.text == "echo: second"
    assert "response.output_text.delta" in second.types
    # Both turns ran on the same muse serve process and session.
    assert echo_harness.muse_pids() == pids
    assert first.terminal["response"]["id"] != second.terminal["response"]["id"]


async def test_usage_reaches_the_completed_response(
    echo_harness: MuseHarness,
) -> None:
    turn = await echo_harness.turn("count me")

    usage = turn.terminal["response"]["usage"]
    # Echo reports zero tokens; this pins the MSP -> Omnigent usage mapping.
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        assert usage[key] == 0


async def test_unauthenticated_provider_fails_the_turn(
    unauthenticated_meta_harness: MuseHarness,
) -> None:
    turn = await unauthenticated_meta_harness.turn("hello")

    _assert_lifecycle(turn.types, "response.failed")
    message = turn.terminal["response"]["error"]["message"]
    assert "provider=meta, authRequired" in message
    assert "muse login" in message
    # A terminal turn failure leaves the host alive for the next turn.
    assert len(unauthenticated_meta_harness.muse_pids()) == 1


async def test_respawn_after_muse_serve_is_killed(echo_harness: MuseHarness) -> None:
    await echo_harness.turn("before")
    [pid] = echo_harness.muse_pids()
    os.kill(pid, signal.SIGKILL)

    # The turn that finds the host dead fails fast instead of hanging...
    broken = await echo_harness.turn("while dead")
    _assert_lifecycle(broken.types, "response.failed")
    assert "Muse transport error" in broken.terminal["response"]["error"]["message"]

    # ...and the next turn spawns a fresh muse serve.
    recovered = await echo_harness.turn("after")
    _assert_lifecycle(recovered.types, "response.completed")
    assert recovered.text == "echo: after"
    [new_pid] = echo_harness.muse_pids()
    assert new_pid != pid
