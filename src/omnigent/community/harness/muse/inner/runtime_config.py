"""Validated runtime configuration for the Muse harness process."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from omnigent.inner.agent_env import declared_passthrough
from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.os_env_serialization import decode_sandbox_spec

ENV_APPROVAL_MODE = "HARNESS_MUSE_APPROVAL_MODE"
ENV_PROVIDER = "HARNESS_MUSE_PROVIDER"
ENV_REASONING_EFFORT = "HARNESS_MUSE_REASONING_EFFORT"
ENV_TURN_IDLE_TIMEOUT = "HARNESS_MUSE_TURN_IDLE_TIMEOUT"
ENV_OS_ENV = "HARNESS_MUSE_OS_ENV"
ENV_ENV_PASSTHROUGH = "HARNESS_MUSE_ENV_PASSTHROUGH"

DEFAULT_APPROVAL_MODE = "onRequest"
DEFAULT_TURN_IDLE_TIMEOUT = 300.0
APPROVAL_MODES = frozenset(
    {"allowAll", "promptUnmatched", "onRequest", "denyUnmatched"}
)
PROVIDERS = frozenset({"echo", "local", "meta"})
REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MuseRuntimeConfig:
    """Configuration resolved once, before ``muse serve`` can be spawned."""

    approval_mode: str = DEFAULT_APPROVAL_MODE
    provider: str | None = None
    reasoning_effort: str | None = None
    turn_idle_timeout: float = DEFAULT_TURN_IDLE_TIMEOUT
    os_env: OSEnvSpec | None = None
    env_passthrough: tuple[str, ...] = ()


def _optional_choice(name: str, allowed: frozenset[str]) -> str | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of {choices}; got {value!r}")
    return value


def _idle_timeout() -> float:
    raw = os.environ.get(ENV_TURN_IDLE_TIMEOUT)
    if raw is None or not raw.strip():
        return DEFAULT_TURN_IDLE_TIMEOUT
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{ENV_TURN_IDLE_TIMEOUT} must be a number; got {raw!r}"
        ) from exc
    if not 0 < value < float("inf"):
        raise ValueError(
            f"{ENV_TURN_IDLE_TIMEOUT} must be finite and greater than zero"
        )
    return value


def _os_env() -> OSEnvSpec | None:
    raw = os.environ.get(ENV_OS_ENV)
    if raw is None or not raw.strip():
        return None
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{ENV_OS_ENV} must contain valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"{ENV_OS_ENV} must encode an object")
    env_type = payload.get("type", "caller_process")
    if env_type != "caller_process":
        raise ValueError(
            f"{ENV_OS_ENV}.type must be 'caller_process'; got {env_type!r}"
        )
    cwd = payload.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError(f"{ENV_OS_ENV}.cwd must be a string or null")
    for field_name in ("fork", "start_in_scratch"):
        if field_name in payload and not isinstance(payload[field_name], bool):
            raise ValueError(f"{ENV_OS_ENV}.{field_name} must be a boolean")
    sandbox_payload = payload.get("sandbox")
    if sandbox_payload is not None and not isinstance(sandbox_payload, dict):
        raise ValueError(f"{ENV_OS_ENV}.sandbox must be an object or null")
    try:
        sandbox = (
            decode_sandbox_spec(sandbox_payload)
            if sandbox_payload is not None
            else None
        )
        return OSEnvSpec(
            type=env_type,
            cwd=cwd,
            sandbox=sandbox,
            fork=bool(payload.get("fork", False)),
            start_in_scratch=bool(payload.get("start_in_scratch", False)),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{ENV_OS_ENV} is invalid: {exc}") from exc


def _passthrough_names(os_env: OSEnvSpec | None) -> tuple[str, ...]:
    raw_names = os.environ.get(ENV_ENV_PASSTHROUGH, "").split(",")
    sandbox_names = declared_passthrough(os_env)
    names: list[str] = []
    for candidate in (*raw_names, *sandbox_names):
        name = candidate.strip()
        if not name:
            continue
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(
                f"{ENV_ENV_PASSTHROUGH} contains invalid environment-variable name {name!r}"
            )
        if name not in names:
            names.append(name)
    return tuple(names)


def load_runtime_config() -> MuseRuntimeConfig:
    """Resolve environment configuration with strict startup validation."""

    os_env = _os_env()
    return MuseRuntimeConfig(
        approval_mode=_optional_choice(ENV_APPROVAL_MODE, APPROVAL_MODES)
        or DEFAULT_APPROVAL_MODE,
        provider=_optional_choice(ENV_PROVIDER, PROVIDERS),
        reasoning_effort=_optional_choice(ENV_REASONING_EFFORT, REASONING_EFFORTS),
        turn_idle_timeout=_idle_timeout(),
        os_env=os_env,
        env_passthrough=_passthrough_names(os_env),
    )
