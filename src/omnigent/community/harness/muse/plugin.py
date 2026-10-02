"""Community harness contribution for Muse Code.

``get_contribution()`` is the entry point core loads during plugin discovery. It
must stay import-light: only registry / install-spec / capability types, no SDK and
no runtime modules (those import lazily inside ``create_app`` / ``build_spawn_env``).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re

_HARNESS = "muse"
_MODULE = "omnigent.community.harness.muse.inner.muse_harness"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _set_unless_ambient(env: dict[str, str], name: str, value: object) -> None:
    """Apply spec configuration without overriding process environment config."""
    if name not in os.environ and value is not None:
        env[name] = str(value)


def _passthrough_names(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TypeError("executor.config.env_passthrough must be a list of names")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _ENV_NAME.fullmatch(item):
            raise ValueError(f"invalid environment-variable passthrough name: {item!r}")
        if item not in names:
            names.append(item)
    return tuple(names)


def get_contribution():
    # Import-light: registry/spec/capability types only (no SDK, no runtime).
    from omnigent.harness_capabilities import (
        AuthModel,
        EffortFamily,
        Elicitation,
        HarnessCapabilities,
        IntegrationMode,
        ModelFamily,
        Resume,
    )
    from omnigent.harness_install_spec import HarnessInstallSpec
    from omnigent.harness_plugins import HarnessContribution

    return HarnessContribution(
        name="omnigent-muse",
        valid_harnesses=frozenset({_HARNESS}),
        harness_modules={_HARNESS: _MODULE},
        aliases={"muse-code": _HARNESS},
        harness_labels={_HARNESS: "Muse"},
        model_env_keys={_HARNESS: "HARNESS_MUSE_MODEL"},
        install_specs={
            _HARNESS: HarnessInstallSpec(
                display="Muse",
                binary="muse",
                package=None,
                login_args=("login",),
                logout_args=("logout",),
                install_hint="curl -fsSL https://dev.meta.ai/install.sh | bash",
                min_version="1.3.0",
            )
        },
        harness_install_keys={_HARNESS: _HARNESS, "muse-code": _HARNESS},
        spawn_env_builders={
            _HARNESS: "omnigent.community.harness.muse.plugin:build_spawn_env"
        },
        capabilities={
            _HARNESS: HarnessCapabilities(
                IntegrationMode.CLI_SUBPROCESS,  # spawns `muse serve`
                Elicitation.JSONRPC,  # MSP structured approval requests
                Resume.NONE,  # cross-process session-id persistence is not wired yet
                EffortFamily.CODEX_NATIVE,
                ModelFamily.MULTI,  # --provider meta / --model
                AuthModel.OWN_AUTH,  # `muse auth` / `muse login`
                subagents=False,
                interrupt=True,
                streaming=True,
            )
        },
    )


def build_spawn_env(spec, *, cwd=None) -> dict[str, str]:
    """Build the env-var dict the muse harness wrap reads at startup.

    Environment variables override declarative spec values. The harness process
    validates the resulting values before it can spawn ``muse serve``.
    """
    env: dict[str, str] = {}
    model = getattr(getattr(spec, "executor", None), "model", None) or getattr(
        spec, "model", None
    )
    _set_unless_ambient(env, "HARNESS_MUSE_MODEL", model)
    if cwd is not None:
        env["HARNESS_MUSE_CWD"] = str(cwd)
    executor = getattr(spec, "executor", None)
    config = getattr(executor, "config", None)
    config = config if isinstance(config, dict) else {}
    _set_unless_ambient(env, "HARNESS_MUSE_APPROVAL_MODE", config.get("approval_mode"))
    reasoning_effort = getattr(executor, "reasoning_effort", None)
    _set_unless_ambient(env, "HARNESS_MUSE_REASONING_EFFORT", reasoning_effort)
    _set_unless_ambient(
        env, "HARNESS_MUSE_TURN_IDLE_TIMEOUT", config.get("turn_idle_timeout")
    )

    os_env = getattr(spec, "os_env", None)
    if os_env is not None and "HARNESS_MUSE_OS_ENV" not in os.environ:
        env["HARNESS_MUSE_OS_ENV"] = json.dumps(dataclasses.asdict(os_env))

    names = _passthrough_names(config.get("env_passthrough"))
    if names and "HARNESS_MUSE_ENV_PASSTHROUGH" not in os.environ:
        env["HARNESS_MUSE_ENV_PASSTHROUGH"] = ",".join(names)
    return env
