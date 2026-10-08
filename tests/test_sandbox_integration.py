"""End-to-end sandboxing of the Muse process tree under a real backend.

Skipped unless unprivileged bubblewrap works on this host (Ubuntu 24.04+
needs an AppArmor ``userns`` profile for ``/usr/bin/bwrap``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import psutil
import pytest
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

from omnigent.community.harness.muse.inner.msp_transport import MspTransport
from omnigent.community.harness.muse.inner.sandbox_launch import (
    MuseSandbox,
    MuseSandboxError,
    resolve_muse_binary,
)

FAKE_HOST = Path(__file__).parent / "fixtures" / "fake_msp_host.py"


def _bwrap_usable() -> bool:
    if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
        return False
    probe = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-user", "true"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _bwrap_usable(), reason="unprivileged bubblewrap is unavailable"
)


@pytest.fixture(autouse=True)
def _private_omnigent_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keeps the private Muse homes out of the developer's ~/.omnigent.
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "omnigent"))


def _spec(workspace: Path, *read_paths: Path) -> OSEnvSpec:
    return OSEnvSpec(
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=[str(path) for path in read_paths],
            write_paths=[str(workspace)],
        )
    )


def _descendants(transport: MspTransport) -> list[psutil.Process]:
    assert transport._client is not None
    pid = transport._client._proc.pid
    return psutil.Process(pid).children(recursive=True)


async def test_restrictions_apply_to_descendants_and_die_with_teardown(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    readonly = tmp_path / "readonly"
    outside = tmp_path / "outside"
    home = tmp_path / "home"
    muse_data = home / ".local" / "share" / "muse"
    for directory in (workspace, readonly, outside, muse_data):
        directory.mkdir(parents=True)
    (home / ".local" / "visible").write_text("visible\n")
    (muse_data / "session-index.db").write_text("sessions\n")
    probe = tmp_path / "bin" / "muse"
    probe.parent.mkdir()
    probe.write_text(
        "#!/bin/sh\n"
        "sh -c '\n"
        '  echo inside > "$PROBE_WORKSPACE/inside.txt"; echo "inside=$?"\n'
        '  echo denied > "$PROBE_READONLY/denied.txt"; echo "readonly=$?"\n'
        '  echo escaped > "$PROBE_OUTSIDE/escaped.txt"; echo "outside=$?"\n'
        '  cat "$HOME/.local/visible"; echo "local=$?"\n'
        '  cat "$HOME/.local/share/muse/session-index.db"; echo "muse_data=$?"\n'
        '\' > "$PROBE_WORKSPACE/probe.log" 2>&1\n'
        'echo "args=$*" >> "$PROBE_WORKSPACE/probe.log"\n'
        "sleep 300 &\n"
        f"exec {sys.executable} {FAKE_HOST}\n"
    )
    probe.chmod(0o755)
    # ~/.local stands in for the prefix core grants around ~/.local/bin/muse.
    sandbox = MuseSandbox.resolve(
        _spec(workspace, readonly, FAKE_HOST.parent, home / ".local"),
        cwd=workspace,
        provider="echo",
    )
    assert sandbox is not None
    transport = await MspTransport.spawn(
        executable=str(probe),
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "PROBE_WORKSPACE": str(workspace),
            "PROBE_READONLY": str(readonly),
            "PROBE_OUTSIDE": str(outside),
        },
        env_passthrough=("PROBE_WORKSPACE", "PROBE_READONLY", "PROBE_OUTSIDE"),
        provider="echo",
        sandbox=sandbox,
    )
    try:
        session_id = await transport.start_session(
            workspace_root=str(workspace), model=None, approval_mode="onRequest"
        )
        assert session_id
        tree = _descendants(transport)
        assert any("sleep" in process.name() for process in tree)
    finally:
        await transport.close()

    log = (workspace / "probe.log").read_text().splitlines()
    assert "inside=0" in log
    assert "readonly=0" not in log
    assert "outside=0" not in log
    assert "local=0" in log
    assert "muse_data=0" not in log
    assert "sessions" not in log
    assert "args=serve --provider echo --disable-sandbox" in log
    assert (workspace / "inside.txt").read_text() == "inside\n"
    assert not (readonly / "denied.txt").exists()
    assert not (outside / "escaped.txt").exists()
    _, alive = psutil.wait_procs(tree, timeout=10)
    assert alive == []


async def test_real_muse_serve_handshakes_inside_sandbox(tmp_path: Path) -> None:
    try:
        resolve_muse_binary("muse")
    except MuseSandboxError:
        pytest.skip("muse is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = MuseSandbox.resolve(_spec(workspace), cwd=workspace, provider="echo")
    assert sandbox is not None
    # An isolated HOME keeps the test off the developer's Muse sessions.
    transport = await MspTransport.spawn(
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},
        provider="echo",
        sandbox=sandbox,
    )
    try:
        session_id = await transport.start_session(
            workspace_root=str(workspace), model=None, approval_mode="onRequest"
        )
        assert session_id
        tree = _descendants(transport)
    finally:
        await transport.close()

    _, alive = psutil.wait_procs(tree, timeout=10)
    assert alive == []
