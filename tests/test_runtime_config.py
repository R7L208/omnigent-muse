from __future__ import annotations

import dataclasses
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.inner.sandbox import SandboxPolicy

from omnigent.community.harness.muse.inner import sandbox_launch
from omnigent.community.harness.muse.inner.runtime_config import (
    DEFAULT_APPROVAL_MODE,
    DEFAULT_TURN_IDLE_TIMEOUT,
    ENV_APPROVAL_MODE,
    ENV_ENV_PASSTHROUGH,
    ENV_OS_ENV,
    ENV_PROVIDER,
    ENV_REASONING_EFFORT,
    ENV_TURN_IDLE_TIMEOUT,
    load_runtime_config,
)
from omnigent.community.harness.muse.inner.sandbox_launch import (
    MuseSandbox,
    MuseSandboxError,
)

_CONFIG_ENV = (
    ENV_APPROVAL_MODE,
    ENV_PROVIDER,
    ENV_REASONING_EFFORT,
    ENV_TURN_IDLE_TIMEOUT,
    ENV_OS_ENV,
    ENV_ENV_PASSTHROUGH,
)


def _active_policy(spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
    return SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=spec.sandbox is None or spec.sandbox.allow_network,
    )


@pytest.fixture(autouse=True)
def _clear_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_preserve_existing_behavior() -> None:
    config = load_runtime_config()
    assert config.approval_mode == DEFAULT_APPROVAL_MODE
    assert config.provider is None
    assert config.reasoning_effort is None
    assert config.turn_idle_timeout == DEFAULT_TURN_IDLE_TIMEOUT
    assert config.os_env is None
    assert config.env_passthrough == ()


def test_loads_all_runtime_options(monkeypatch: pytest.MonkeyPatch) -> None:
    os_env = OSEnvSpec(
        cwd="/workspace",
        sandbox=OSEnvSandboxSpec(type="none", env_passthrough=["FROM_OS_ENV"]),
    )
    monkeypatch.setenv(ENV_APPROVAL_MODE, "allowAll")
    monkeypatch.setenv(ENV_PROVIDER, "echo")
    monkeypatch.setenv(ENV_REASONING_EFFORT, "high")
    monkeypatch.setenv(ENV_TURN_IDLE_TIMEOUT, "12.5")
    monkeypatch.setenv(ENV_OS_ENV, json.dumps(dataclasses.asdict(os_env)))
    monkeypatch.setenv(ENV_ENV_PASSTHROUGH, "EXPLICIT,FROM_OS_ENV")

    config = load_runtime_config()
    assert config.approval_mode == "allowAll"
    assert config.provider == "echo"
    assert config.reasoning_effort == "high"
    assert config.turn_idle_timeout == 12.5
    assert config.os_env is not None and config.os_env.cwd == "/workspace"
    assert config.env_passthrough == ("EXPLICIT", "FROM_OS_ENV")


@pytest.mark.parametrize(
    "reasoning_effort",
    ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
)
def test_accepts_msp_reasoning_efforts(
    monkeypatch: pytest.MonkeyPatch, reasoning_effort: str
) -> None:
    monkeypatch.setenv(ENV_REASONING_EFFORT, reasoning_effort)

    assert load_runtime_config().reasoning_effort == reasoning_effort


@pytest.mark.parametrize(
    "approval_mode",
    ["allowAll", "promptUnmatched", "onRequest", "denyUnmatched"],
)
def test_accepts_msp_approval_modes(
    monkeypatch: pytest.MonkeyPatch, approval_mode: str
) -> None:
    monkeypatch.setenv(ENV_APPROVAL_MODE, approval_mode)

    assert load_runtime_config().approval_mode == approval_mode


@pytest.mark.parametrize("provider", ["meta", "echo", "local"])
def test_accepts_muse_providers(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    monkeypatch.setenv(ENV_PROVIDER, provider)

    assert load_runtime_config().provider == provider


@pytest.mark.parametrize("approval_mode", ["always", "never", "sometimes"])
def test_rejects_invalid_approval_modes(
    monkeypatch: pytest.MonkeyPatch, approval_mode: str
) -> None:
    monkeypatch.setenv(ENV_APPROVAL_MODE, approval_mode)

    with pytest.raises(ValueError, match="must be one of"):
        load_runtime_config()


def test_active_sandbox_excludes_desktop_session_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os_env = OSEnvSpec(
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            env_passthrough=[
                "GITHUB_TOKEN",
                "DBUS_SESSION_BUS_ADDRESS",
                "XDG_RUNTIME_DIR",
            ],
        )
    )
    monkeypatch.setenv(ENV_OS_ENV, json.dumps(dataclasses.asdict(os_env)))

    assert load_runtime_config().env_passthrough == ("GITHUB_TOKEN",)


def test_unsandboxed_environment_allows_explicit_desktop_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    os_env = OSEnvSpec(
        sandbox=OSEnvSandboxSpec(
            type="none",
            env_passthrough=["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"],
        )
    )
    monkeypatch.setenv(ENV_OS_ENV, json.dumps(dataclasses.asdict(os_env)))

    assert load_runtime_config().env_passthrough == (
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
    )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (ENV_REASONING_EFFORT, "extreme", "must be one of"),
        (ENV_PROVIDER, "unknown", "must be one of"),
        (ENV_TURN_IDLE_TIMEOUT, "0", "greater than zero"),
        (ENV_TURN_IDLE_TIMEOUT, "NaN", "finite"),
        (ENV_OS_ENV, "not-json", "valid JSON"),
        (ENV_OS_ENV, '"string"', "encode an object"),
        (ENV_OS_ENV, json.dumps({"type": "container"}), r"\.type must be"),
        (ENV_OS_ENV, json.dumps({"cwd": 42}), r"\.cwd must be"),
        (ENV_OS_ENV, json.dumps({"fork": "false"}), r"\.fork must be a boolean"),
        (
            ENV_OS_ENV,
            json.dumps({"start_in_scratch": 1}),
            r"\.start_in_scratch must be a boolean",
        ),
        (ENV_OS_ENV, json.dumps({"sandbox": []}), r"\.sandbox must be"),
        (
            ENV_OS_ENV,
            json.dumps({"sandbox": {"read_paths": "not-a-list"}}),
            "is invalid",
        ),
        (ENV_ENV_PASSTHROUGH, "GOOD,BAD-NAME", "invalid environment-variable"),
    ],
)
def test_invalid_options_fail_before_transport_start(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises((TypeError, ValueError), match=message):
        load_runtime_config()


def test_spawn_env_uses_spec_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.community.harness.muse.plugin import build_spawn_env

    monkeypatch.setenv("ALLOWED_TOKEN", "secret")
    spec = SimpleNamespace(
        executor=SimpleNamespace(
            model="muse-large",
            reasoning_effort="medium",
            config={
                "approval_mode": "allowAll",
                "provider": "echo",
                "turn_idle_timeout": 30,
                "env_passthrough": ["ALLOWED_TOKEN"],
            },
        ),
        model=None,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none")),
    )

    env = build_spawn_env(spec)
    assert env[ENV_APPROVAL_MODE] == "allowAll"
    assert env[ENV_PROVIDER] == "echo"
    assert env[ENV_REASONING_EFFORT] == "medium"
    assert env[ENV_TURN_IDLE_TIMEOUT] == "30"
    assert json.loads(env[ENV_OS_ENV])["sandbox"]["type"] == "none"
    assert env[ENV_ENV_PASSTHROUGH] == "ALLOWED_TOKEN"
    assert "ALLOWED_TOKEN" not in env


@pytest.mark.parametrize(
    "ambient_name",
    [
        "HARNESS_MUSE_MODEL",
        "HARNESS_MUSE_CWD",
        ENV_APPROVAL_MODE,
        ENV_PROVIDER,
        ENV_REASONING_EFFORT,
        ENV_TURN_IDLE_TIMEOUT,
        ENV_OS_ENV,
        ENV_ENV_PASSTHROUGH,
    ],
)
def test_ambient_environment_takes_precedence_over_each_spec_option(
    monkeypatch: pytest.MonkeyPatch, ambient_name: str
) -> None:
    from omnigent.community.harness.muse.plugin import build_spawn_env

    monkeypatch.setenv(ambient_name, "ambient-value")
    spec = SimpleNamespace(
        executor=SimpleNamespace(
            model="spec-model",
            reasoning_effort="high",
            config={
                "approval_mode": "allowAll",
                "provider": "echo",
                "turn_idle_timeout": 30,
                "env_passthrough": ["OPTED_IN"],
            },
        ),
        model=None,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none")),
    )

    env = build_spawn_env(spec, cwd=Path("/spec/workspace"))

    assert ambient_name not in env
    assert os.environ[ambient_name] == "ambient-value"


def test_executor_factory_applies_validated_defaults_to_respawn_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.community.harness.muse.inner.msp_transport import MspTransport
    from omnigent.community.harness.muse.inner.muse_executor import MuseExecutor
    from omnigent.community.harness.muse.inner.muse_harness import (
        _build_muse_executor,
    )

    monkeypatch.setenv(ENV_APPROVAL_MODE, "denyUnmatched")
    monkeypatch.setenv(ENV_PROVIDER, "local")
    monkeypatch.setenv(ENV_REASONING_EFFORT, "medium")
    monkeypatch.setenv(ENV_TURN_IDLE_TIMEOUT, "17")
    monkeypatch.setenv(ENV_ENV_PASSTHROUGH, "OPTED_IN")

    executor = cast(MuseExecutor, _build_muse_executor())
    transport = cast(MspTransport, executor._transport_factory())

    assert executor._approval_mode == "denyUnmatched"
    assert executor._reasoning_effort == "medium"
    assert transport._idle_timeout == 17
    assert transport._env_passthrough == ("OPTED_IN",)
    assert transport._provider == "local"


def test_executor_factory_resolves_sandbox_against_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.community.harness.muse.inner.msp_transport import MspTransport
    from omnigent.community.harness.muse.inner.muse_executor import MuseExecutor
    from omnigent.community.harness.muse.inner.muse_harness import (
        _build_muse_executor,
    )

    workspaces: list[Path] = []

    def record(spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        workspaces.append(cwd)
        return _active_policy(spec, cwd)

    monkeypatch.setattr(sandbox_launch, "resolve_sandbox", record)
    monkeypatch.setenv("HARNESS_MUSE_CWD", str(tmp_path))
    monkeypatch.setenv(
        ENV_OS_ENV,
        json.dumps(
            dataclasses.asdict(OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")))
        ),
    )

    executor = cast(MuseExecutor, _build_muse_executor())
    first = cast(MspTransport, executor._transport_factory())
    second = cast(MspTransport, executor._transport_factory())

    assert workspaces == [tmp_path.resolve()]
    assert isinstance(first._sandbox, MuseSandbox)
    assert second._sandbox is first._sandbox


def test_relative_workspace_is_made_absolute_for_muse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.community.harness.muse.inner.muse_executor import MuseExecutor
    from omnigent.community.harness.muse.inner.muse_harness import (
        _build_muse_executor,
    )

    (tmp_path / "proj").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARNESS_MUSE_CWD", "proj")
    monkeypatch.delenv(ENV_OS_ENV, raising=False)

    executor = cast(MuseExecutor, _build_muse_executor())

    # A sandboxed Muse runs inside the workspace, so a relative session
    # root would resolve to proj/proj.
    assert executor._cwd == str(tmp_path / "proj")


def test_executor_without_os_env_is_unsandboxed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.community.harness.muse.inner.msp_transport import MspTransport
    from omnigent.community.harness.muse.inner.muse_executor import MuseExecutor
    from omnigent.community.harness.muse.inner.muse_harness import (
        _build_muse_executor,
    )

    executor = cast(MuseExecutor, _build_muse_executor())

    assert cast(MspTransport, executor._transport_factory())._sandbox is None


def test_invalid_sandbox_fails_before_transport_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.community.harness.muse.inner.muse_harness import (
        _build_muse_executor,
    )

    monkeypatch.setattr(sandbox_launch, "resolve_sandbox", _active_policy)
    monkeypatch.setenv(ENV_PROVIDER, "meta")
    monkeypatch.setenv(
        ENV_OS_ENV,
        json.dumps(
            dataclasses.asdict(
                OSEnvSpec(
                    sandbox=OSEnvSandboxSpec(type="linux_bwrap", allow_network=False)
                )
            )
        ),
    )

    with pytest.raises(MuseSandboxError, match="allow_network"):
        _build_muse_executor()


async def test_recovery_retains_non_default_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.community.harness.muse.inner import muse_harness
    from omnigent.community.harness.muse.inner.muse_executor import (
        MuseTransportError,
        MuseTurnFinished,
    )

    transports: list[Any] = []

    class RecoveryTransport:
        def __init__(
            self,
            *,
            idle_timeout: float,
            env_passthrough: tuple[str, ...],
            provider: str | None,
            sandbox: object,
        ) -> None:
            self.idle_timeout = idle_timeout
            self.sandbox = sandbox
            self.env_passthrough = env_passthrough
            self.provider = provider
            self.starts: list[dict[str, Any]] = []
            self.turns: list[dict[str, Any]] = []
            self.closed = False
            self.active_provider: str | None = None
            transports.append(self)

        async def start_session(self, **kwargs: Any) -> str:
            self.starts.append(kwargs)
            return f"session-{len(transports)}"

        async def run_turn(
            self,
            session_id: str,
            *,
            text: str,
            reasoning_effort: str | None,
        ) -> AsyncIterator[Any]:
            self.turns.append(
                {
                    "session_id": session_id,
                    "text": text,
                    "reasoning_effort": reasoning_effort,
                }
            )
            if len(transports) == 1:
                raise MuseTransportError(
                    "host exited", retryable=True, transport_dead=True
                )
            yield MuseTurnFinished("turn-2", "completed")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setenv(ENV_APPROVAL_MODE, "denyUnmatched")
    monkeypatch.setenv(ENV_PROVIDER, "local")
    monkeypatch.setenv(ENV_REASONING_EFFORT, "ultra")
    monkeypatch.setenv(ENV_TURN_IDLE_TIMEOUT, "17")
    monkeypatch.setenv(ENV_ENV_PASSTHROUGH, "OPTED_IN")
    monkeypatch.setenv(
        ENV_OS_ENV,
        json.dumps(
            dataclasses.asdict(OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")))
        ),
    )
    monkeypatch.setattr(sandbox_launch, "resolve_sandbox", _active_policy)
    monkeypatch.setattr(muse_harness, "MspTransport", RecoveryTransport)

    executor = muse_harness._build_muse_executor()
    turn = {
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [],
        "system_prompt": "Be concise.",
    }
    first = [event async for event in executor.run_turn(**turn)]
    second = [event async for event in executor.run_turn(**turn)]

    assert len(transports) == 2
    assert isinstance(transports[0].sandbox, MuseSandbox)
    assert transports[1].sandbox is transports[0].sandbox
    for transport in transports:
        assert transport.idle_timeout == 17
        assert transport.env_passthrough == ("OPTED_IN",)
        assert transport.provider == "local"
        assert transport.starts[0]["approval_mode"] == "denyUnmatched"
        assert transport.turns[0]["reasoning_effort"] == "ultra"
    assert isinstance(first[-1], ExecutorError)
    assert isinstance(second[-1], TurnComplete)


@pytest.mark.parametrize(
    ("approval_mode", "reasoning_effort"),
    [
        ("allowAll", "high"),
        ("promptUnmatched", "high"),
        ("onRequest", "high"),
        ("denyUnmatched", "high"),
        ("onRequest", "max"),
        ("onRequest", "ultra"),
    ],
)
async def test_declarative_config_reaches_real_msp_session_and_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    approval_mode: str,
    reasoning_effort: str,
) -> None:
    from omnigent.community.harness.muse.inner.msp_client import MspClient
    from omnigent.community.harness.muse.inner.muse_executor import MuseExecutor
    from omnigent.community.harness.muse.inner.muse_harness import (
        _build_muse_executor,
    )
    from omnigent.community.harness.muse.plugin import build_spawn_env

    log_path = tmp_path / "msp.jsonl"
    monkeypatch.setenv("FAKE_MSP_LOG", str(log_path))
    spec = SimpleNamespace(
        executor=SimpleNamespace(
            model="configured-model",
            reasoning_effort=reasoning_effort,
            config={
                "approval_mode": approval_mode,
                "provider": "echo",
                "turn_idle_timeout": 19,
                "env_passthrough": ["FAKE_MSP_LOG"],
            },
        ),
        model=None,
        os_env=None,
    )
    for name, value in build_spawn_env(spec, cwd=tmp_path).items():
        monkeypatch.setenv(name, value)

    real_spawn = MspClient.spawn
    fake_host = Path(__file__).parent / "fixtures" / "fake_msp_host.py"

    spawn_argv: list[str] = []

    async def spawn_fake_host(argv: object, **kwargs: Any) -> MspClient:
        spawn_argv.extend(cast(list[str], argv))
        return await real_spawn([sys.executable, str(fake_host)], **kwargs)

    monkeypatch.setattr(MspClient, "spawn", staticmethod(spawn_fake_host))
    executor = cast(MuseExecutor, _build_muse_executor())
    try:
        events = [
            event
            async for event in executor.run_turn(
                messages=[{"role": "user", "content": "hello"}],
                tools=[],
                system_prompt="Be concise.",
            )
        ]
    finally:
        await executor.close()

    frames = [json.loads(line) for line in log_path.read_text().splitlines()]
    session = next(frame for frame in frames if frame.get("method") == "session/start")
    turn = next(frame for frame in frames if frame.get("method") == "turn/start")
    assert session["params"]["workspaceRoot"] == str(tmp_path)
    assert spawn_argv[-2:] == ["--provider", "echo"]
    assert session["params"]["approvalMode"] == approval_mode
    assert session["params"]["modelId"] == "configured-model"
    assert turn["params"]["reasoningEffort"] == reasoning_effort
    assert turn["params"]["input"] == [{"type": "text", "text": "Be concise.\n\nhello"}]
    assert any(isinstance(event, TurnComplete) for event in events)
