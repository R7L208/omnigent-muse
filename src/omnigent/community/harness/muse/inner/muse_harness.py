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

_ENV_MODEL = "HARNESS_MUSE_MODEL"
_ENV_CWD = "HARNESS_MUSE_CWD"


def _build_muse_executor() -> Executor:
    return MuseExecutor(
        MspTransport,
        model=os.environ.get(_ENV_MODEL) or None,
        cwd=os.environ.get(_ENV_CWD)
        or os.environ.get("OMNIGENT_RUNNER_WORKSPACE")
        or None,
    )


def create_app() -> FastAPI:
    return ExecutorAdapter(
        executor_factory=_build_muse_executor, harness_label="Muse"
    ).build()
