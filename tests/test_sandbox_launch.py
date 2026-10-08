"""OS-environment sandboxing for the ``muse serve`` process tree."""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest
from omnigent.inner.datamodel import CredentialProxySpec, OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import SandboxPolicy

from omnigent.community.harness.muse.inner import sandbox_launch
from omnigent.community.harness.muse.inner.sandbox_launch import (
    MuseSandbox,
    MuseSandboxError,
    resolve_muse_binary,
)


def _policy(
    *,
    backend_type: str = "linux_bwrap",
    active: bool = True,
    allow_network: bool = True,
) -> SandboxPolicy:
    return SandboxPolicy(
        backend_type=backend_type,
        active=active,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=allow_network,
    )


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> list[tuple[OSEnvSpec, Path]]:
    """Stub policy resolution so tests do not depend on the host's bwrap."""
    calls: list[tuple[OSEnvSpec, Path]] = []
    policy = _policy()

    def fake_resolve(spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        calls.append((spec, cwd))
        return policy

    monkeypatch.setattr(sandbox_launch, "resolve_sandbox", fake_resolve)
    return calls


def _bwrap_spec() -> OSEnvSpec:
    return OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap"))


def test_no_os_env_means_no_sandbox(tmp_path: Path) -> None:
    assert MuseSandbox.resolve(None, cwd=tmp_path, provider="meta") is None


def test_explicit_none_sandbox_means_no_sandbox(tmp_path: Path) -> None:
    spec = OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none"))

    assert MuseSandbox.resolve(spec, cwd=tmp_path, provider="meta") is None


def test_policy_is_resolved_once_against_the_workspace(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]]
) -> None:
    spec = _bwrap_spec()

    sandbox = MuseSandbox.resolve(spec, cwd=tmp_path, provider="meta")

    assert sandbox is not None
    assert resolved == [(spec, tmp_path.resolve())]


def test_workspace_falls_back_to_os_env_cwd(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]]
) -> None:
    spec = OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="linux_bwrap"))

    MuseSandbox.resolve(spec, cwd=None, provider="meta")

    assert resolved[0][1] == tmp_path.resolve()


@pytest.mark.parametrize("backend_type", ["windows_jobobject", "custom"])
def test_unsupported_backends_fail_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_type: str
) -> None:
    monkeypatch.setattr(
        sandbox_launch,
        "resolve_sandbox",
        lambda spec, cwd: _policy(backend_type=backend_type),
    )

    with pytest.raises(MuseSandboxError, match=backend_type):
        MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")


@pytest.mark.parametrize(
    "sandbox_spec",
    [
        OSEnvSandboxSpec(type="linux_bwrap", egress_rules=["GET example.com/**"]),
        OSEnvSandboxSpec(
            type="linux_bwrap",
            credential_proxy=CredentialProxySpec(entries=[]),
            egress_rules=[],
        ),
    ],
)
def test_egress_and_credential_proxy_are_rejected(
    tmp_path: Path,
    resolved: list[tuple[OSEnvSpec, Path]],
    sandbox_spec: OSEnvSandboxSpec,
) -> None:
    spec = OSEnvSpec(sandbox=sandbox_spec)

    with pytest.raises(MuseSandboxError, match="egress proxy"):
        MuseSandbox.resolve(spec, cwd=tmp_path, provider="meta")


def test_seatbelt_is_rejected_until_core_supports_muse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sandbox_launch,
        "resolve_sandbox",
        lambda spec, cwd: _policy(backend_type="darwin_seatbelt"),
    )

    with pytest.raises(MuseSandboxError, match="darwin_seatbelt"):
        MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")


@pytest.mark.parametrize(
    "error", [OSError("bwrap not found"), NotImplementedError("no backend")]
)
def test_backend_resolution_failures_fail_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        raise error

    monkeypatch.setattr(sandbox_launch, "resolve_sandbox", fail)

    with pytest.raises(MuseSandboxError, match=str(error)):
        MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")


@pytest.mark.parametrize("provider", [None, "meta", "local"])
def test_network_isolation_rejects_providers_that_need_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str | None
) -> None:
    monkeypatch.setattr(
        sandbox_launch,
        "resolve_sandbox",
        lambda spec, cwd: _policy(allow_network=False),
    )

    with pytest.raises(MuseSandboxError, match="allow_network"):
        MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider=provider)


def test_network_isolation_allows_offline_echo_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sandbox_launch,
        "resolve_sandbox",
        lambda spec, cwd: _policy(allow_network=False),
    )

    assert MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="echo")


@pytest.fixture
def omnigent_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "omnigent"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data))
    return data


def _user_config(home: Path) -> Path:
    config = home / ".config" / "muse"
    config.mkdir(parents=True)
    (config / "auth.json").write_text('{"providers": {}}')
    (config / ".auth.json.lock").write_text("")
    (config / "settings.json").write_text('{"model": "user"}')
    (config / "trust.json").write_text("{}")
    return config


def _assert_copied(private: Path, user: Path) -> None:
    """``private`` holds ``user``'s content in a file of its own."""
    assert private.read_bytes() == user.read_bytes()
    assert not os.path.samefile(private, user)
    assert stat.S_IMODE(private.stat().st_mode) == 0o600


def test_launch_wraps_real_binary_and_delegates_shell_sandbox(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    binary = _executable(tmp_path / "muse")
    home = tmp_path / "home"
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")
    assert sandbox is not None

    launch = sandbox.launch(
        str(binary),
        ["serve", "--provider", "meta"],
        {"HOME": str(home), "PATH": "/bin", "META_API_KEY": "sk-test"},
    )
    try:
        launcher = Path(launch.argv[0])
        assert launcher.is_file() and os.access(launcher, os.X_OK)
        assert launch.argv[1:] == ("serve", "--provider", "meta", "--disable-sandbox")
        assert launch.cwd == str(tmp_path.resolve())
        private = sandbox_launch.private_home(tmp_path.resolve())
        assert private.is_relative_to(omnigent_data)
        # A passed-through META_API_KEY reaches Muse, as it does unsandboxed.
        assert launch.env == {
            "HOME": str(home),
            "PATH": "/bin",
            "META_API_KEY": "sk-test",
            "MUSE_NO_AUTO_UPDATE": "1",
            "XDG_CONFIG_HOME": str(private / "config"),
            "XDG_DATA_HOME": str(private / "data"),
            "XDG_STATE_HOME": str(private / "state"),
            "XDG_CACHE_HOME": str(private / "cache"),
        }

        policy = launch.policy
        assert policy.read_roots == [sandbox_launch._OMNIGENT_IMPORT_ROOT]
        assert private.resolve() in policy.write_roots
        assert (private / "data" / "muse").is_dir()
        assert policy.write_files == []
        assert policy.spawn_env_allowlist == sorted(launch.env)
        assert str(binary.resolve()) in launcher.read_text()
    finally:
        launch.cleanup()
    assert not Path(launch.argv[0]).exists()
    launch.cleanup()  # idempotent


def test_launch_links_login_and_copies_settings(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    binary = _executable(tmp_path / "muse")
    home = tmp_path / "home"
    user = _user_config(home)
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")
    assert sandbox is not None

    sandbox.launch(str(binary), ["serve"], {"HOME": str(home)}).cleanup()

    private = sandbox_launch.private_home(tmp_path.resolve()) / "config" / "muse"
    _assert_copied(private / "auth.json", user / "auth.json")
    # Settings are a private copy; trust decisions and the login lock are not
    # shared.
    assert (private / "settings.json").read_text() == '{"model": "user"}'
    _assert_copied(private / "settings.json", user / "settings.json")
    assert sorted(path.name for path in private.iterdir()) == [
        "auth.json",
        "settings.json",
    ]
    # The record of the copied login is out of the sandbox's reach.
    record = sandbox_launch.private_home(tmp_path.resolve()).with_suffix(".login")
    assert record.is_file()


def test_launch_resyncs_user_config_on_every_spawn(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    binary = _executable(tmp_path / "muse")
    home = tmp_path / "home"
    user = _user_config(home)
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")
    assert sandbox is not None
    env = {"HOME": str(home)}
    sandbox.launch(str(binary), ["serve"], env).cleanup()
    private = sandbox_launch.private_home(tmp_path.resolve()) / "config" / "muse"

    # Logging in again replaces the file rather than rewriting it.
    (user / "auth.json").unlink()
    (user / "auth.json").write_text('{"providers": {"meta": {}}}')
    (user / "settings.json").write_text('{"model": "changed"}')
    sandbox.launch(str(binary), ["serve"], env).cleanup()
    _assert_copied(private / "auth.json", user / "auth.json")
    assert (private / "settings.json").read_text() == '{"model": "changed"}'

    # The user logs out: the sandbox loses the login too.
    (user / "auth.json").unlink()
    sandbox.launch(str(binary), ["serve"], env).cleanup()
    assert not (private / "auth.json").exists()


def _refresh_inside_sandbox(private: Path, text: str) -> None:
    """Rewrite the private login the way Muse refreshes it: temp + rename."""
    staged = private / ".auth.json.tmp-5-0"
    staged.write_text(text)
    os.replace(staged, private / "auth.json")


def _linked_sandbox(tmp_path: Path) -> tuple[MuseSandbox, Path, Path, dict[str, str]]:
    binary = _executable(tmp_path / "muse")
    user = _user_config(tmp_path / "home")
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")
    assert sandbox is not None
    env = {"HOME": str(tmp_path / "home")}
    sandbox.launch(str(binary), ["serve"], env).cleanup()
    private = sandbox_launch.private_home(tmp_path.resolve()) / "config" / "muse"
    return sandbox, private, user, env


def test_launch_keeps_a_refresh_made_inside_the_sandbox_private(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    sandbox, private, user, env = _linked_sandbox(tmp_path)
    _refresh_inside_sandbox(private, '{"refreshed": true}')
    # Even dated in the past, the refresh is kept while the user's login is
    # the one last linked.
    os.utime(private / "auth.json", (0, 0))

    sandbox.launch(str(tmp_path / "muse"), ["serve"], env).cleanup()

    # The refresh stays in the sandbox and never reaches the user's login.
    assert (private / "auth.json").read_text() == '{"refreshed": true}'
    assert (user / "auth.json").read_text() == '{"providers": {}}'


@pytest.mark.parametrize("in_place", [False, True])
def test_a_new_user_login_replaces_a_sandbox_refresh(
    tmp_path: Path,
    resolved: list[tuple[OSEnvSpec, Path]],
    omnigent_data: Path,
    in_place: bool,
) -> None:
    sandbox, private, user, env = _linked_sandbox(tmp_path)
    _refresh_inside_sandbox(private, '{"refreshed": true}')
    # A future date planted in the sandbox does not hold the new login off.
    os.utime(private / "auth.json", (4_000_000_000,) * 2)
    if not in_place:
        (user / "auth.json").unlink()
    (user / "auth.json").write_text('{"providers": {"meta": {"new": 1}}}')

    sandbox.launch(str(tmp_path / "muse"), ["serve"], env).cleanup()

    _assert_copied(private / "auth.json", user / "auth.json")


def test_a_sandbox_refresh_does_not_survive_a_logout(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    sandbox, private, user, env = _linked_sandbox(tmp_path)
    _refresh_inside_sandbox(private, '{"refreshed": true}')
    (user / "auth.json").unlink()

    sandbox.launch(str(tmp_path / "muse"), ["serve"], env).cleanup()

    assert not (private / "auth.json").exists()


def test_a_planted_login_symlink_is_relinked(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    sandbox, private, user, env = _linked_sandbox(tmp_path)
    secret = tmp_path / "secret.json"
    secret.write_text("secret")
    (private / "auth.json").unlink()
    (private / "auth.json").symlink_to(secret)

    sandbox.launch(str(tmp_path / "muse"), ["serve"], env).cleanup()

    assert not (private / "auth.json").is_symlink()
    _assert_copied(private / "auth.json", user / "auth.json")
    assert secret.read_text() == "secret"


def test_a_planted_private_file_is_replaced(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    sandbox, private, user, env = _linked_sandbox(tmp_path)
    shutil.rmtree(private)
    private.write_text("not a directory")

    sandbox.launch(str(tmp_path / "muse"), ["serve"], env).cleanup()

    assert private.is_dir()
    _assert_copied(private / "auth.json", user / "auth.json")


def test_a_planted_private_directory_symlink_is_replaced(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    sandbox, private, user, env = _linked_sandbox(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "settings.json").write_text("keep")
    shutil.rmtree(private)
    private.symlink_to(elsewhere)

    sandbox.launch(str(tmp_path / "muse"), ["serve"], env).cleanup()

    # Omnigent writes into a real private directory, never through the link.
    assert private.is_dir() and not private.is_symlink()
    _assert_copied(private / "auth.json", user / "auth.json")
    assert sorted(p.name for p in elsewhere.iterdir()) == ["settings.json"]
    assert (elsewhere / "settings.json").read_text() == "keep"


def test_an_in_place_write_inside_the_sandbox_does_not_reach_the_user(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    _, private, user, _ = _linked_sandbox(tmp_path)

    (private / "auth.json").write_text('{"planted": true}')

    assert (user / "auth.json").read_text() == '{"providers": {}}'


@pytest.mark.parametrize("name", ["auth.json", "settings.json"])
@pytest.mark.parametrize("logged_in", [True, False])
def test_a_directory_planted_at_a_bridged_file_is_replaced(
    tmp_path: Path,
    resolved: list[tuple[OSEnvSpec, Path]],
    omnigent_data: Path,
    name: str,
    logged_in: bool,
) -> None:
    sandbox, private, user, env = _linked_sandbox(tmp_path)
    (private / name).unlink()
    (private / name / "nested").mkdir(parents=True)
    if not logged_in:
        (user / name).unlink()

    sandbox.launch(str(tmp_path / "muse"), ["serve"], env).cleanup()

    if logged_in:
        _assert_copied(private / name, user / name)
    else:
        assert not (private / name).exists()


def test_launch_masks_the_users_own_muse_directories(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    binary = _executable(tmp_path / "muse")
    home = tmp_path / "home"
    data = tmp_path / "xdg-data"
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")
    assert sandbox is not None

    launch = sandbox.launch(
        str(binary), ["serve"], {"HOME": str(home), "XDG_DATA_HOME": str(data)}
    )
    launch.cleanup()

    # The binary's prefix (~/.local) is readable inside the sandbox, which
    # would otherwise expose the user's Muse sessions and memory.
    assert launch.policy.mask_paths == [
        (home / ".config" / "muse").resolve(),
        (data / "muse").resolve(),
        (home / ".local" / "state" / "muse").resolve(),
        (home / ".cache" / "muse").resolve(),
    ]


def test_launch_masks_only_around_a_grant_inside_a_muse_directory(
    tmp_path: Path, omnigent_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _executable(tmp_path / "muse")
    home = tmp_path / "home"
    config = home / ".config" / "muse"
    data = home / ".local" / "share" / "muse"
    workspace = config / "skills"
    plugins = data / "plugins" / "shared"
    for directory in (
        workspace,
        plugins,
        data / "plugins" / "other",
        data / "sessions",
    ):
        directory.mkdir(parents=True)
    (config / "auth.json").write_text("{}")
    (data / "session-index.db").write_text("")
    policy = _policy()
    policy.write_roots = [workspace.resolve()]
    policy.read_roots = [plugins.resolve()]
    monkeypatch.setattr(sandbox_launch, "resolve_sandbox", lambda spec, cwd: policy)
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=workspace, provider="meta")
    assert sandbox is not None

    launch = sandbox.launch(str(binary), ["serve"], {"HOME": str(home)})
    launch.cleanup()

    # The grants stay visible; everything beside them is still masked.
    assert launch.policy.mask_paths == [
        (config / "auth.json").resolve(),
        (data / "plugins" / "other").resolve(),
        (data / "session-index.db").resolve(),
        (data / "sessions").resolve(),
        (home / ".local" / "state" / "muse").resolve(),
        (home / ".cache" / "muse").resolve(),
    ]


def _masks_around_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binary: Path
) -> tuple[list[Path], Path]:
    home = tmp_path / "home"
    data = home / ".local" / "share" / "muse"
    plugins = data / "plugins"
    plugins.mkdir(parents=True)
    policy = _policy()
    policy.read_roots = [plugins.resolve()]
    monkeypatch.setattr(sandbox_launch, "resolve_sandbox", lambda spec, cwd: policy)
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")
    assert sandbox is not None
    launch = sandbox.launch(str(binary), ["serve"], {"HOME": str(home)})
    launch.cleanup()
    assert launch.policy.mask_paths is not None
    return launch.policy.mask_paths, data


def test_a_symlink_beside_a_grant_is_left_to_the_rest_of_the_policy(
    tmp_path: Path, omnigent_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "usr-lib"
    target.mkdir()
    data = tmp_path / "home" / ".local" / "share" / "muse"
    data.mkdir(parents=True)
    (data / "lib").symlink_to(target)

    masks, _ = _masks_around_plugins(
        tmp_path, monkeypatch, _executable(tmp_path / "muse")
    )

    # Its target may be anything on the host, so it is neither masked here
    # nor followed.
    assert target.resolve() not in masks
    assert (data / "lib") not in masks


def test_the_muse_binary_directory_is_never_masked(
    tmp_path: Path, omnigent_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "home" / ".local" / "share" / "muse" / "bin"
    bin_dir.mkdir(parents=True)
    binary = _executable(bin_dir / "muse-bin-1.0.0-R1")

    masks, data = _masks_around_plugins(tmp_path, monkeypatch, binary)

    assert not any(bin_dir.resolve().is_relative_to(mask) for mask in masks)
    assert data.resolve() not in masks


def test_an_unreadable_directory_beside_a_grant_is_masked_whole(
    tmp_path: Path, omnigent_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "home" / ".local" / "share" / "muse"
    locked = data / "plugins" / "locked"
    (locked / "inner").mkdir(parents=True)
    grant = (locked / "inner").resolve()
    locked.chmod(0)
    try:
        policy = _policy()
        policy.read_roots = [grant]
        monkeypatch.setattr(sandbox_launch, "resolve_sandbox", lambda spec, cwd: policy)
        sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")
        assert sandbox is not None
        launch = sandbox.launch(
            str(_executable(tmp_path / "muse")),
            ["serve"],
            {"HOME": str(tmp_path / "home")},
        )
        launch.cleanup()
    finally:
        locked.chmod(0o755)

    assert launch.policy.mask_paths is not None
    assert locked.resolve() in launch.policy.mask_paths


def test_a_workspace_inside_a_muse_directory_is_not_masked(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    # Under bwrap the read-only cwd is in neither read_roots nor write_roots.
    home = tmp_path / "home"
    workspace = home / ".config" / "muse"
    workspace.mkdir(parents=True)
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=workspace, provider="meta")
    assert sandbox is not None

    launch = sandbox.launch(
        str(_executable(tmp_path / "muse")), ["serve"], {"HOME": str(home)}
    )
    launch.cleanup()

    assert launch.policy.mask_paths is not None
    assert workspace.resolve() not in launch.policy.mask_paths


def test_a_login_lock_linked_by_an_earlier_version_is_removed(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    sandbox, private, user, env = _linked_sandbox(tmp_path)
    os.link(user / ".auth.json.lock", private / ".auth.json.lock")

    sandbox.launch(str(tmp_path / "muse"), ["serve"], env).cleanup()

    assert not (private / ".auth.json.lock").exists()
    assert (user / ".auth.json.lock").exists()


def test_a_file_write_grant_inside_a_muse_directory_stays_visible(
    tmp_path: Path, omnigent_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    settings = home / ".config" / "muse" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{}")
    (settings.parent / "auth.json").write_text("{}")
    policy = _policy()
    policy.write_files = [settings.resolve()]
    monkeypatch.setattr(sandbox_launch, "resolve_sandbox", lambda spec, cwd: policy)
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="meta")
    assert sandbox is not None

    launch = sandbox.launch(
        str(_executable(tmp_path / "muse")), ["serve"], {"HOME": str(home)}
    )
    launch.cleanup()

    assert launch.policy.mask_paths is not None
    assert settings.resolve() not in launch.policy.mask_paths
    assert (settings.parent / "auth.json").resolve() in launch.policy.mask_paths


def test_a_failed_login_copy_is_not_taken_for_a_logout(
    tmp_path: Path,
    resolved: list[tuple[OSEnvSpec, Path]],
    omnigent_data: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox, private, user, env = _linked_sandbox(tmp_path)
    (user / "auth.json").write_text('{"providers": {"meta": {"new": 1}}}')

    def vanished(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("private config removed")

    monkeypatch.setattr(sandbox_launch, "_write_at", vanished)

    with pytest.raises(FileNotFoundError):
        sandbox.launch(str(tmp_path / "muse"), ["serve"], env)

    assert (private / "auth.json").read_text() == '{"providers": {}}'


def test_launch_honours_user_xdg_config_home(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    binary = _executable(tmp_path / "muse")
    user = tmp_path / "cfg" / "muse"
    user.mkdir(parents=True)
    (user / "auth.json").write_text("{}")
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="echo")
    assert sandbox is not None
    env = {"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}

    launch = sandbox.launch(str(binary), ["serve"], env)
    launch.cleanup()

    private = sandbox_launch.private_home(tmp_path.resolve())
    assert launch.env["XDG_CONFIG_HOME"] == str(private / "config")
    _assert_copied(private / "config" / "muse" / "auth.json", user / "auth.json")


def test_workspaces_get_separate_private_homes(
    tmp_path: Path, omnigent_data: Path
) -> None:
    first = sandbox_launch.private_home(tmp_path / "a")
    second = sandbox_launch.private_home(tmp_path / "b")

    assert first != second
    assert first == sandbox_launch.private_home(tmp_path / "a")


def test_launch_redirects_user_xdg_state_and_cache(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    binary = _executable(tmp_path / "muse")
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="echo")
    assert sandbox is not None
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "user-state"),
        "XDG_CACHE_HOME": str(tmp_path / "user-cache"),
    }

    launch = sandbox.launch(str(binary), ["serve"], env)
    launch.cleanup()

    private = sandbox_launch.private_home(tmp_path.resolve())
    assert launch.env["XDG_STATE_HOME"] == str(private / "state")
    assert launch.env["XDG_CACHE_HOME"] == str(private / "cache")


def test_each_launch_reuses_the_resolved_policy(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    binary = _executable(tmp_path / "muse")
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="echo")
    assert sandbox is not None
    env = {"HOME": str(tmp_path / "home")}

    first = sandbox.launch(str(binary), ["serve"], env)
    second = sandbox.launch(str(binary), ["serve"], env)
    first.cleanup()
    second.cleanup()

    assert len(resolved) == 1
    assert first.argv[0] != second.argv[0]
    assert first.policy == second.policy


def test_resolve_binary_follows_installer_wrapper(tmp_path: Path) -> None:
    wrapper = _executable(tmp_path / "muse", "#!/usr/bin/env bash\nexec true\n")
    (tmp_path / ".muse-version").write_text("1.4.2-R4684.1\n")
    real = _executable(tmp_path / "muse-bin-1.4.2-R4684.1")

    assert resolve_muse_binary(str(wrapper)) == str(real.resolve())


def test_resolve_binary_through_path_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _executable(tmp_path / "muse")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert resolve_muse_binary("muse") == str(real.resolve())


def test_resolve_binary_rejects_wrapper_without_installed_binary(
    tmp_path: Path,
) -> None:
    wrapper = _executable(tmp_path / "muse")
    (tmp_path / ".muse-version").write_text("1.4.2-R4684.1\n")

    with pytest.raises(MuseSandboxError, match="muse-bin-1.4.2-R4684.1"):
        resolve_muse_binary(str(wrapper))


def _installed(directory: Path, version: str, info: object) -> Path:
    directory.mkdir(exist_ok=True)
    _executable(directory / "muse")
    (directory / ".muse-version").write_text(f"{version}\n")
    (directory / ".muse-release-info.json").write_text(json.dumps(info))
    return _executable(directory / f"muse-bin-{version}")


def test_launch_exports_release_info_like_the_wrapper(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    info = {"channel": "muse-stable", "version": "1.4.3-R5018.1"}
    _installed(tmp_path / "bin", "1.4.3-R5018.1", info)
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="echo")
    assert sandbox is not None

    launch = sandbox.launch(
        str(tmp_path / "bin" / "muse"),
        ["serve"],
        {"HOME": str(tmp_path / "home"), "MUSE_RELEASE_INFO": "stale"},
    )
    launch.cleanup()

    assert json.loads(launch.env["MUSE_RELEASE_INFO"]) == info


@pytest.mark.parametrize("info", [{"version": "1.4.2-R4684.1"}, ["not", "a", "dict"]])
def test_release_info_for_another_version_is_dropped(
    tmp_path: Path, info: object
) -> None:
    binary = _installed(tmp_path, "1.4.3-R5018.1", info)

    assert sandbox_launch.release_info(str(binary)) is None


def test_launch_unsets_release_info_when_missing(
    tmp_path: Path, resolved: list[tuple[OSEnvSpec, Path]], omnigent_data: Path
) -> None:
    binary = _executable(tmp_path / "muse")
    sandbox = MuseSandbox.resolve(_bwrap_spec(), cwd=tmp_path, provider="echo")
    assert sandbox is not None

    launch = sandbox.launch(
        str(binary), ["serve"], {"HOME": str(tmp_path), "MUSE_RELEASE_INFO": "stale"}
    )
    launch.cleanup()

    assert "MUSE_RELEASE_INFO" not in launch.env


def test_resolve_binary_rejects_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(MuseSandboxError, match="not found"):
        resolve_muse_binary(str(tmp_path / "missing-muse"))
