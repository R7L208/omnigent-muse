"""Run ``muse serve`` inside the Omnigent OS-environment sandbox.

The policy is resolved once from the agent's :class:`OSEnvSpec` and reused
for every spawn, so a respawned transport runs under exactly the sandbox the
first one did. Each spawn writes a fresh exec launcher
(:func:`omnigent.sandbox.create_exec_launcher`) that re-execs itself under the
platform backend before starting Muse, which puts the whole Muse process tree
-- its shell tool and anything that launches -- inside the sandbox.

Anything that would weaken the requested sandbox fails here instead of
silently running Muse unconfined.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.sandbox import _project_root
from omnigent.process_logging import data_dir
from omnigent.sandbox import (
    SandboxPolicy,
    create_exec_launcher,
    resolve_sandbox,
    with_additional_read_roots,
    with_additional_write_roots,
    with_spawn_env_allowlist,
)

# Backends whose launcher confines the target's descendants. Windows Job
# Objects are applied from the parent after spawn, which this launch path
# does not do, and Muse does not ship for Windows.
#
# darwin_seatbelt confines the tree too, but Muse cannot run under Omnigent's
# Seatbelt profile yet: the profile grants only stat on the ancestors of
# granted paths, while Muse opens each ancestor of its data dir (session/start
# fails with UnsafePath), and it denies ~/Library outright, which hides the
# Keychain that holds the Muse login on macOS. Both need core changes; see
# https://github.com/R7L208/omnigent-muse/issues/23.
SUPPORTED_BACKENDS = frozenset({"linux_bwrap"})
# The only provider that works without network access.
OFFLINE_PROVIDERS = frozenset({"echo"})
# Muse's own shell sandbox needs user namespaces, which the outer sandbox's
# seccomp profile denies; the outer sandbox is the enforcement boundary.
DISABLE_NATIVE_SANDBOX = "--disable-sandbox"
# The installer wrapper (~/.local/bin/muse) cannot run inside the sandbox: it
# self-updates over the network and reads its state from dotfiles next to
# itself, which the sandbox masks. So the sandbox runs the versioned binary
# the wrapper would exec and reproduces the two things the wrapper hands it:
# MUSE_NO_AUTO_UPDATE (set below) and MUSE_RELEASE_INFO.
_VERSION_FILE = ".muse-version"
_RELEASE_INFO_FILE = ".muse-release-info.json"
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+-R[0-9]+(\.[0-9]+)?$")
# Sandboxed Muse runs from a private home so nothing it writes (plugins,
# skills, memory, settings) is loaded later by the user's unsandboxed Muse.
# The login is copied in, never linked, so nothing written inside the sandbox
# reaches the user's own login; a refresh inside the sandbox stays private
# (see _bridge_login). The login lock is not shared either: the two sides
# never write the same file, and sandboxed code holding a shared lock could
# stall the user's own refreshes.
_LOGIN = "auth.json"
_LOGIN_LOCK = ".auth.json.lock"
# Opens a directory without following a symlink planted in its place.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Every XDG base directory Muse may read or write points into the private
# home, so nothing it caches or records lands in the user's own directories.
# The values are the XDG defaults relative to $HOME.
_XDG_DEFAULTS = {
    "config": ".config",
    "data": ".local/share",
    "state": ".local/state",
    "cache": ".cache",
}
_XDG_KINDS = tuple(_XDG_DEFAULTS)
# Copied, so a settings change made inside the sandbox stays there.
_COPIED_FILES = ("settings.json",)
_RELEASE_INFO_ENV = "MUSE_RELEASE_INFO"
# The launcher imports omnigent inside the sandbox from the directory core
# puts on its sys.path. For an editable install that is a checkout outside
# the venv, which the sandbox would otherwise hide. Core's own (private)
# helper keeps the grant and the launcher's sys.path in step.
_OMNIGENT_IMPORT_ROOT = _project_root()


class MuseSandboxError(RuntimeError):
    """The requested sandbox cannot be applied to ``muse serve``."""


def resolve_muse_binary(executable: str) -> str:
    """Return the absolute path of the Muse binary behind ``executable``.

    :param executable: A command name or path, e.g. ``"muse"``.
    :raises MuseSandboxError: The executable is missing, or it is the
        installer wrapper and the binary it selects is not installed.
    """
    found = shutil.which(executable)
    if found is None:
        raise MuseSandboxError(f"Muse executable {executable!r} not found")
    path = Path(found).resolve()
    version_file = path.parent / _VERSION_FILE
    if path.name.startswith("muse-bin-") or not version_file.is_file():
        return str(path)
    version = version_file.read_text(encoding="utf-8").strip()
    binary = path.parent / f"muse-bin-{version}"
    if not _VERSION.fullmatch(version) or not os.access(binary, os.X_OK):
        raise MuseSandboxError(
            f"Muse launcher {path} selects {binary.name}, which is not an "
            "installed executable; rerun the Muse installer"
        )
    return str(binary)


def release_info(binary: str) -> str | None:
    """Return the wrapper's release info for ``binary``, as the wrapper would.

    The wrapper exports ``.muse-release-info.json`` as ``MUSE_RELEASE_INFO``
    only when it describes the active version; otherwise it unsets it.

    :param binary: Path of the resolved ``muse-bin-<version>`` binary.
    :returns: The file's JSON text, or ``None`` when it is missing, malformed,
        or describes another version.
    """
    directory = Path(binary).parent
    try:
        version = (directory / _VERSION_FILE).read_text(encoding="utf-8").strip()
        text = (directory / _RELEASE_INFO_FILE).read_text(encoding="utf-8")
        info = json.loads(text)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("version") != version:
        return None
    if Path(binary).name != f"muse-bin-{version}":
        return None
    return text


def _user_muse_dir(env: Mapping[str, str], kind: str) -> Path:
    """Return the user's own Muse directory of XDG ``kind`` as Muse computes it.

    :param env: The environment Muse would run with unsandboxed.
    :param kind: An XDG base directory kind, e.g. ``"data"``.
    :returns: A path such as ``~/.local/share/muse``.
    """
    home = Path(env.get("HOME") or Path.home())
    base = env.get(f"XDG_{kind.upper()}_HOME") or home / _XDG_DEFAULTS[kind]
    return Path(base) / "muse"


def private_home(workspace: Path) -> Path:
    """Return the private Muse home used for sandboxed runs in ``workspace``.

    :param workspace: The resolved sandbox workspace, e.g. ``/repo``.
    :returns: A path such as ``~/.omnigent/muse-sandbox/3f2a9c0d1e4b5a6f``.
    """
    digest = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    return data_dir() / "muse-sandbox" / digest


def _mask_around(path: Path, grants: Sequence[Path]) -> list[Path]:
    """Return the paths that hide ``path`` except for the ``grants`` inside it.

    A granted path (the workspace, the private home, a spec read/write path,
    or the Muse binary's directory) inside ``path`` stays visible; everything
    beside it is masked.

    :param path: A resolved directory to hide, e.g. ``~/.local/share/muse``.
    :param grants: Resolved granted roots.
    :returns: ``[path]``, or its ungranted entries when a grant lies inside.
    """
    # A symlink beside a grant is left to the rest of the policy: the backend
    # cannot mask it, and its target may be anywhere on the host.
    if path.is_symlink():
        return []
    inside = [grant for grant in grants if grant.is_relative_to(path)]
    if not inside:
        return [path]
    if path in inside or not path.is_dir():
        return []
    try:
        children = sorted(path.iterdir())
    except OSError:
        return [path]
    return [masked for child in children for masked in _mask_around(child, inside)]


def _identity(st: os.stat_result) -> str:
    return f"{st.st_dev} {st.st_ino} {st.st_mtime_ns} {st.st_size}"


def _open_private_dir(parent_fd: int, name: str) -> int:
    """Open directory ``name`` under ``parent_fd``, creating it if needed.

    The private home is writable from inside the sandbox, so anything else
    found there (a planted symlink or file) is removed rather than followed;
    every later file operation goes through the returned descriptor.
    """
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        os.unlink(name, dir_fd=parent_fd)
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)


def _remove_at(dir_fd: int, name: str) -> None:
    """Remove ``name`` from ``dir_fd``, even a directory planted in its place."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)


def _write_at(dir_fd: int, name: str, data: BinaryIO | bytes) -> None:
    """Atomically replace ``name`` in ``dir_fd`` with ``data``, readable by the user only.

    Staging under a unique name and renaming means launches racing in one
    workspace, and a Muse already running there, never see a missing or
    half-written file.
    """
    staged = f".{name}.omnigent-{uuid.uuid4().hex}"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(staged, flags, 0o600, dir_fd=dir_fd), "wb") as out:
            if isinstance(data, bytes):
                out.write(data)
            else:
                shutil.copyfileobj(data, out)
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISDIR(
                os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
            ):
                shutil.rmtree(name, dir_fd=dir_fd)
        os.replace(staged, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(staged, dir_fd=dir_fd)


def _copy_to(src: Path, dir_fd: int, name: str) -> os.stat_result | None:
    """Copy ``src`` over ``name`` in ``dir_fd``.

    :returns: The status of the file copied, or ``None`` when ``src`` is
        missing or cannot be read.
    """
    try:
        fd = os.open(src, os.O_RDONLY | os.O_CLOEXEC)
    except (FileNotFoundError, PermissionError):
        return None
    with os.fdopen(fd, "rb") as data:
        copied = os.fstat(fd)
        if not stat.S_ISREG(copied.st_mode):
            return None
        _write_at(dir_fd, name, data)
    return copied


def _read_at(dir_fd: int, name: str) -> str | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as record:
        return record.read()


def _bridge_login(user: Path, dir_fd: int, records_fd: int, record: str) -> None:
    """Copy the user's login ``user`` into the private config ``dir_fd``.

    A copy, not a link, so nothing written inside the sandbox reaches the
    user's own login. Muse refreshes by renaming a new file over
    ``auth.json``; that refresh is kept while the user's login is the one
    last copied, and replaced once the user logs in again. Which login was
    last copied is recorded as ``record`` in ``records_fd``, next to the
    private home rather than in it, so the sandbox cannot edit it.
    """
    try:
        current = _identity(user.stat())
        private = os.stat(_LOGIN, dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, PermissionError):
        pass
    else:
        if stat.S_ISREG(private.st_mode) and _read_at(records_fd, record) == current:
            return
    copied = _copy_to(user, dir_fd, _LOGIN)
    if copied is None:
        # Logged out (or the login is unreadable): the sandbox loses it too.
        _remove_at(dir_fd, _LOGIN)
        _remove_at(records_fd, record)
    else:
        _write_at(records_fd, record, _identity(copied).encode())


def _unshare_login_lock(user_config: Path, dir_fd: int) -> None:
    """Remove a login lock hard-linked in by an earlier version of this harness."""
    with contextlib.suppress(FileNotFoundError, PermissionError):
        private = os.stat(_LOGIN_LOCK, dir_fd=dir_fd, follow_symlinks=False)
        if os.path.samestat(private, (user_config / _LOGIN_LOCK).stat()):
            os.unlink(_LOGIN_LOCK, dir_fd=dir_fd)


def _prepare_private_home(home: Path, user_config: Path) -> None:
    """Lay out the private home and bridge the user's login and settings.

    :param home: The private home, e.g. ``~/.omnigent/muse-sandbox/<digest>``.
    :param user_config: The user's Muse config directory.
    """
    home.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:

        def track(fd: int) -> int:
            stack.callback(os.close, fd)
            return fd

        homes_fd = track(
            os.open(home.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        )
        home_fd = track(_open_private_dir(homes_fd, home.name))
        kinds = {kind: track(_open_private_dir(home_fd, kind)) for kind in _XDG_KINDS}
        track(_open_private_dir(kinds["data"], "muse"))
        config_fd = track(_open_private_dir(kinds["config"], "muse"))
        _unshare_login_lock(user_config, config_fd)
        _bridge_login(user_config / _LOGIN, config_fd, homes_fd, f"{home.name}.login")
        for name in _COPIED_FILES:
            if _copy_to(user_config / name, config_fd, name) is None:
                _remove_at(config_fd, name)


@dataclass(frozen=True)
class MuseLaunch:
    """One sandboxed ``muse serve`` invocation; ``cleanup`` after spawning."""

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: str
    policy: SandboxPolicy

    def cleanup(self) -> None:
        """Remove the launcher script. Safe once the process has started."""
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.argv[0])


class MuseSandbox:
    """A resolved sandbox that every ``muse serve`` spawn is launched through."""

    def __init__(self, policy: SandboxPolicy, workspace: Path) -> None:
        self._policy = policy
        self._workspace = workspace

    @property
    def workspace(self) -> Path:
        """The resolved directory every sandboxed ``muse serve`` runs in."""
        return self._workspace

    @classmethod
    def resolve(
        cls,
        os_env: OSEnvSpec | None,
        *,
        cwd: str | Path | None,
        provider: str | None,
    ) -> MuseSandbox | None:
        """Resolve ``os_env`` into a sandbox, or ``None`` when none is requested.

        :param os_env: The agent's OS environment; ``None`` or
            ``sandbox.type: none`` leaves Muse unsandboxed.
        :param cwd: Workspace Muse serves; falls back to ``os_env.cwd`` and
            then the process working directory.
        :param provider: Muse provider, checked against network isolation.
        :raises MuseSandboxError: The sandbox cannot be applied as requested.
        """
        if os_env is None:
            return None
        workspace = Path(cwd or os_env.cwd or os.getcwd()).resolve()
        try:
            policy = resolve_sandbox(os_env, workspace)
        except (OSError, ValueError, NotImplementedError) as exc:
            raise MuseSandboxError(f"cannot sandbox Muse: {exc}") from exc
        if not policy.active:
            return None
        spec = os_env.sandbox
        if spec is not None and (spec.egress_rules or spec.credential_proxy):
            # Nothing on the exec-launcher path starts Omnigent's egress
            # proxy, so these would leave the network unrestricted.
            raise MuseSandboxError(
                "os_env.sandbox.egress_rules and credential_proxy are not "
                "supported for Muse: nothing starts the egress proxy, so the "
                "network would be unrestricted; remove them, or set "
                "allow_network: false with the 'echo' provider"
            )
        if policy.backend_type == "darwin_seatbelt":
            raise MuseSandboxError(
                "Muse cannot run under the darwin_seatbelt sandbox yet; use "
                "sandbox type 'none' on macOS"
            )
        if policy.backend_type not in SUPPORTED_BACKENDS:
            supported = ", ".join(sorted(SUPPORTED_BACKENDS))
            raise MuseSandboxError(
                f"sandbox type {policy.backend_type!r} cannot confine the Muse "
                f"process tree; use one of {supported}, or 'none'"
            )
        if not policy.allow_network and provider not in OFFLINE_PROVIDERS:
            raise MuseSandboxError(
                "os_env.sandbox.allow_network is false, but Muse provider "
                f"{provider or 'default'!r} needs network access; allow network "
                "or use the 'echo' provider"
            )
        return cls(policy, workspace)

    def launch(
        self, executable: str, args: Sequence[str], env: Mapping[str, str]
    ) -> MuseLaunch:
        """Prepare one sandboxed spawn of ``executable args``.

        :param executable: Muse command name or path, e.g. ``"muse"``.
        :param args: Arguments after the executable, e.g. ``["serve"]``.
        :param env: The already-filtered environment for the Muse process.
        :raises MuseSandboxError: The Muse binary cannot be resolved.
        :raises OSError: The private home or launcher cannot be written.
        """
        binary = resolve_muse_binary(executable)
        home = private_home(self._workspace)
        _prepare_private_home(home, _user_muse_dir(env, "config"))
        xdg = {f"XDG_{kind.upper()}_HOME": home / kind for kind in _XDG_KINDS}
        spawn_env = {
            **env,
            "MUSE_NO_AUTO_UPDATE": "1",
            **{name: str(path) for name, path in xdg.items()},
        }
        info = release_info(binary)
        if info is None:
            spawn_env.pop(_RELEASE_INFO_ENV, None)
        else:
            spawn_env[_RELEASE_INFO_ENV] = info
        policy = with_additional_read_roots(self._policy, [_OMNIGENT_IMPORT_ROOT])
        # Grant the home itself: the backends mask dotfiles only at the top
        # level of a granted root, so Muse's lock files below stay visible.
        policy = with_additional_write_roots(policy, [home])
        # The user's own Muse directories stay hidden even where a broader
        # grant covers them: core makes the binary's prefix (~/.local for
        # ~/.local/bin/muse-bin-*) readable, which holds ~/.local/share/muse.
        grants = [
            path.resolve()
            for path in (
                *(policy.read_roots or []),
                *policy.write_roots,
                *policy.write_files,
                *(policy.copy_on_write_roots or []),
                self._workspace,
                Path(binary).parent,
            )
        ]
        masks = [
            path
            for kind in _XDG_KINDS
            for path in _mask_around(_user_muse_dir(env, kind).resolve(), grants)
        ]
        policy = dataclasses.replace(
            policy, mask_paths=[*(policy.mask_paths or []), *masks]
        )
        policy = with_spawn_env_allowlist(policy, list(spawn_env))
        launcher = create_exec_launcher(binary, policy, cwd=str(self._workspace))
        return MuseLaunch(
            argv=(launcher, *args, DISABLE_NATIVE_SANDBOX),
            env=spawn_env,
            cwd=str(self._workspace),
            policy=policy,
        )
