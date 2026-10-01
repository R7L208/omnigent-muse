from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import cast

import pytest
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

from omnigent.community.harness.muse.inner.runtime_config import (
    DEFAULT_APPROVAL_MODE,
    DEFAULT_TURN_IDLE_TIMEOUT,
    ENV_APPROVAL_MODE,
    ENV_ENV_PASSTHROUGH,
    ENV_OS_ENV,
    ENV_REASONING_EFFORT,
    ENV_TURN_IDLE_TIMEOUT,
    load_runtime_config,
)

_CONFIG_ENV = (
    ENV_APPROVAL_MODE,
    ENV_REASONING_EFFORT,
    ENV_TURN_IDLE_TIMEOUT,
    ENV_OS_ENV,
    ENV_ENV_PASSTHROUGH,
)


@pytest.fixture(autouse=True)
def _clear_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_preserve_existing_behavior() -> None:
    config = load_runtime_config()
    assert config.approval_mode == DEFAULT_APPROVAL_MODE
    assert config.reasoning_effort is None
    assert config.turn_idle_timeout == DEFAULT_TURN_IDLE_TIMEOUT
    assert config.os_env is None
    assert config.env_passthrough == ()


def test_loads_all_runtime_options(monkeypatch: pytest.MonkeyPatch) -> None:
    os_env = OSEnvSpec(
        cwd="/workspace",
        sandbox=OSEnvSandboxSpec(type="none", env_passthrough=["FROM_OS_ENV"]),
    )
    monkeypatch.setenv(ENV_APPROVAL_MODE, "always")
    monkeypatch.setenv(ENV_REASONING_EFFORT, "high")
    monkeypatch.setenv(ENV_TURN_IDLE_TIMEOUT, "12.5")
    monkeypatch.setenv(ENV_OS_ENV, json.dumps(dataclasses.asdict(os_env)))
    monkeypatch.setenv(ENV_ENV_PASSTHROUGH, "EXPLICIT,FROM_OS_ENV")

    config = load_runtime_config()
    assert config.approval_mode == "always"
    assert config.reasoning_effort == "high"
    assert config.turn_idle_timeout == 12.5
    assert config.os_env is not None and config.os_env.cwd == "/workspace"
    assert config.env_passthrough == ("EXPLICIT", "FROM_OS_ENV")


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (ENV_APPROVAL_MODE, "sometimes", "must be one of"),
        (ENV_REASONING_EFFORT, "extreme", "must be one of"),
        (ENV_TURN_IDLE_TIMEOUT, "0", "greater than zero"),
        (ENV_TURN_IDLE_TIMEOUT, "NaN", "finite"),
        (ENV_OS_ENV, "not-json", "valid JSON"),
        (ENV_OS_ENV, '"string"', "encode an object"),
        (ENV_ENV_PASSTHROUGH, "GOOD,BAD-NAME", "invalid environment-variable"),
    ],
)
def test_invalid_options_fail_before_transport_start(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises((TypeError, ValueError), match=message):
        load_runtime_config()


def test_spawn_env_uses_spec_values_but_ambient_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.community.harness.muse.plugin import build_spawn_env

    monkeypatch.setenv(ENV_APPROVAL_MODE, "never")
    monkeypatch.setenv("ALLOWED_TOKEN", "secret")
    spec = SimpleNamespace(
        executor=SimpleNamespace(
            model="muse-large",
            reasoning_effort="medium",
            config={
                "approval_mode": "always",
                "turn_idle_timeout": 30,
                "env_passthrough": ["ALLOWED_TOKEN"],
            },
        ),
        model=None,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none")),
    )

    env = build_spawn_env(spec)
    assert ENV_APPROVAL_MODE not in env
    assert env[ENV_REASONING_EFFORT] == "medium"
    assert env[ENV_TURN_IDLE_TIMEOUT] == "30"
    assert json.loads(env[ENV_OS_ENV])["sandbox"]["type"] == "none"
    assert env[ENV_ENV_PASSTHROUGH] == "ALLOWED_TOKEN"
    assert env["ALLOWED_TOKEN"] == "secret"


def test_executor_factory_applies_validated_defaults_to_respawn_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.community.harness.muse.inner.msp_transport import MspTransport
    from omnigent.community.harness.muse.inner.muse_executor import MuseExecutor
    from omnigent.community.harness.muse.inner.muse_harness import (
        _build_muse_executor,
    )

    monkeypatch.setenv(ENV_APPROVAL_MODE, "never")
    monkeypatch.setenv(ENV_REASONING_EFFORT, "medium")
    monkeypatch.setenv(ENV_TURN_IDLE_TIMEOUT, "17")
    monkeypatch.setenv(ENV_ENV_PASSTHROUGH, "OPTED_IN")

    executor = cast(MuseExecutor, _build_muse_executor())
    transport = cast(MspTransport, executor._transport_factory())

    assert executor._approval_mode == "never"
    assert executor._reasoning_effort == "medium"
    assert transport._idle_timeout == 17
    assert transport._env_passthrough == ("OPTED_IN",)
