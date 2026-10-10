"""Muse harness application wiring.

``create_app()`` is the entry point core's runner invokes after resolving the
``muse`` harness id to this module. It returns the FastAPI app built by
``ExecutorAdapter``, which installs the elicitation/policy bridges.

The executor's Omnigent behavior lives in :mod:`.muse_executor`; the MSP
transport is supplied separately so protocol framing stays replaceable.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

from .msp_transport import MspTransport
from .muse_executor import MuseExecutor
from .omnigent_tool_relay import SessionToolRelay
from .runtime_config import load_runtime_config
from .sandbox_launch import MuseSandbox

_ENV_MODEL = "HARNESS_MUSE_MODEL"
_ENV_CWD = "HARNESS_MUSE_CWD"


def _build_muse_executor() -> Executor:
    config = load_runtime_config()
    workspace = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE")
    # Absolute, because a sandboxed Muse runs in the workspace itself and would
    # resolve a relative session root against it a second time.
    cwd = os.path.abspath(workspace) if workspace else None
    # Resolved once, before any spawn: invalid sandboxes fail here, and every
    # respawned transport reuses the same policy.
    sandbox = MuseSandbox.resolve(config.os_env, cwd=cwd, provider=config.provider)
    return MuseExecutor(
        lambda: MspTransport(
            idle_timeout=config.turn_idle_timeout,
            env_passthrough=config.env_passthrough,
            provider=config.provider,
            sandbox=sandbox,
        ),
        model=os.environ.get(_ENV_MODEL) or None,
        cwd=cwd,
        approval_mode=config.approval_mode,
        reasoning_effort=config.reasoning_effort,
        provider=config.provider,
        # Sandboxed Muse cannot reach the relay's bridge dir yet.
        relay_factory=lambda: SessionToolRelay(sandboxed=sandbox is not None),
    )


def create_app() -> FastAPI:
    return ExecutorAdapter(
        executor_factory=_build_muse_executor, harness_label="Muse"
    ).build()
