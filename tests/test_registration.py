"""Registration tests for the omnigent-muse community harness.

``test_contribution_shape`` needs only the omnigent registry types importable.
``test_registered_in_omnigent`` additionally needs the plugin's entry point
discoverable in the running environment (an editable/installed omnigent + this
package). It is skipped automatically if omnigent isn't importable.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _plugin_entry_point_is_installed() -> bool:
    candidates = importlib.metadata.entry_points().select(
        group="omnigent.community.harness"
    )
    return any(
        entry_point.name == "muse"
        and entry_point.value
        == "omnigent.community.harness.muse.plugin:get_contribution"
        for entry_point in candidates
    )


def test_contribution_shape_and_capabilities():
    from omnigent.harness_capabilities import (
        AuthModel,
        EffortFamily,
        Elicitation,
        IntegrationMode,
        ModelFamily,
        Resume,
    )

    from omnigent.community.harness.muse.plugin import get_contribution

    c = get_contribution()
    assert c.name == "omnigent-muse"
    assert c.valid_harnesses == frozenset({"muse"})
    assert "muse" in c.valid_harnesses
    assert (
        c.harness_modules["muse"]
        == "omnigent.community.harness.muse.inner.muse_harness"
    )
    assert c.aliases["muse-code"] == "muse"
    assert c.harness_labels["muse"] == "Muse"
    assert c.model_env_keys == {"muse": "HARNESS_MUSE_MODEL"}
    assert c.spawn_env_builders == {
        "muse": "omnigent.community.harness.muse.plugin:build_spawn_env"
    }

    capabilities = c.capabilities["muse"]
    assert capabilities.integration_mode is IntegrationMode.CLI_SUBPROCESS
    assert capabilities.elicitation is Elicitation.JSONRPC
    assert capabilities.resume is Resume.NONE
    assert capabilities.effort is EffortFamily.OPENAI
    assert capabilities.model_family is ModelFamily.MULTI
    assert capabilities.auth is AuthModel.OWN_AUTH
    assert capabilities.subagents is False
    assert capabilities.interrupt is True
    assert capabilities.streaming is True


def test_install_and_auth_metadata():
    from omnigent.community.harness.muse.plugin import get_contribution

    contribution = get_contribution()
    install = contribution.install_specs["muse"]

    assert install.display == "Muse"
    assert install.binary == "muse"
    assert install.package is None
    assert install.login_args == ("login",)
    assert install.logout_args == ("logout",)
    assert install.min_version == "1.3.0"
    assert install.install_hint == "curl -fsSL https://dev.meta.ai/install.sh | bash"
    assert contribution.harness_install_keys == {"muse": "muse", "muse-code": "muse"}


def test_contribution_discovery_does_not_import_muse_sdk():
    from omnigent.community.harness.muse.plugin import get_contribution

    before = {
        name
        for name in sys.modules
        if name == "muse_code" or name.startswith("muse_code.")
    }
    get_contribution()
    after = {
        name
        for name in sys.modules
        if name == "muse_code" or name.startswith("muse_code.")
    }

    assert after == before


@pytest.mark.parametrize(
    ("spec", "cwd", "expected"),
    [
        (
            SimpleNamespace(executor=SimpleNamespace(model="muse-large")),
            None,
            {"HARNESS_MUSE_MODEL": "muse-large"},
        ),
        (
            SimpleNamespace(executor=None, model="fallback-model"),
            Path("/tmp/workspace"),
            {
                "HARNESS_MUSE_MODEL": "fallback-model",
                "HARNESS_MUSE_CWD": "/tmp/workspace",
            },
        ),
        (SimpleNamespace(executor=SimpleNamespace(model=None), model=None), None, {}),
    ],
)
def test_build_spawn_env(spec, cwd, expected):
    from omnigent.community.harness.muse.plugin import build_spawn_env

    assert build_spawn_env(spec, cwd=cwd) == expected


def test_build_spawn_env_returns_a_fresh_mapping():
    from omnigent.community.harness.muse.plugin import build_spawn_env

    spec = SimpleNamespace(executor=SimpleNamespace(model="muse-large"))
    first = build_spawn_env(spec)
    first["MUTATED"] = "yes"

    assert build_spawn_env(spec) == {"HARNESS_MUSE_MODEL": "muse-large"}


def test_build_spawn_env_registers_all_runtime_options(monkeypatch):
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    from omnigent.community.harness.muse.plugin import build_spawn_env

    monkeypatch.setenv("MUSE_TEST_TOKEN", "token-value")
    spec = SimpleNamespace(
        executor=SimpleNamespace(
            model="muse-large",
            reasoning_effort="high",
            config={
                "approval_mode": "allowAll",
                "turn_idle_timeout": 45,
                "env_passthrough": ["MUSE_TEST_TOKEN"],
            },
        ),
        model=None,
        os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none")),
    )

    env = build_spawn_env(spec, cwd=Path("/tmp/workspace"))

    assert env["HARNESS_MUSE_MODEL"] == "muse-large"
    assert env["HARNESS_MUSE_CWD"] == "/tmp/workspace"
    assert env["HARNESS_MUSE_APPROVAL_MODE"] == "allowAll"
    assert env["HARNESS_MUSE_REASONING_EFFORT"] == "high"
    assert env["HARNESS_MUSE_TURN_IDLE_TIMEOUT"] == "45"
    assert json.loads(env["HARNESS_MUSE_OS_ENV"])["sandbox"]["type"] == "none"
    assert env["HARNESS_MUSE_ENV_PASSTHROUGH"] == "MUSE_TEST_TOKEN"
    assert "MUSE_TEST_TOKEN" not in env


@pytest.mark.skipif(
    not _plugin_entry_point_is_installed(),
    reason="omnigent-muse entry point is not installed in this environment",
)
def test_registered_in_omnigent():
    import omnigent.harness_plugins as hp
    from omnigent.util.reasoning_effort import OPENAI_EFFORTS, efforts_for_harness

    hp.reset_plugin_state_for_tests()
    assert "muse" in hp.valid_harnesses()
    assert hp.harness_aliases()["muse-code"] == "muse"
    assert (
        hp.harness_modules()["muse"]
        == "omnigent.community.harness.muse.inner.muse_harness"
    )
    assert any(r["id"] == "muse" and r["label"] == "Muse" for r in hp.harness_catalog())
    assert efforts_for_harness("muse") == OPENAI_EFFORTS
    assert hp.plugin_state().load_errors == {}
