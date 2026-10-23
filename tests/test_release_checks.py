from __future__ import annotations

import hashlib
import http.client
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import urllib.error
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github/scripts/release/check-release.py"
PLUGIN = "omnigent/community/harness/muse"
MODULES = ("__init__.py", "plugin.py", "inner/__init__.py", "inner/msp_client.py")
REQUIRES = "omnigent<1,>=0.16.0"
ENTRY_POINTS = (
    "[omnigent.community.harness]\n"
    "muse = omnigent.community.harness.muse.plugin:get_contribution\n"
)


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run check-release.py from ``cwd``, which holds the pyproject it reads."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _project(
    tmp_path: Path, version: str, *, dependency: str = "omnigent>=0.16.0,<1"
) -> Path:
    """A checkout with the pyproject and plugin sources the script reads."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "omnigent-muse"\nversion = "{version}"\n'
        f'dependencies = ["{dependency}"]\n'
    )
    for name in MODULES:
        module = tmp_path / "src" / PLUGIN / name
        module.parent.mkdir(parents=True, exist_ok=True)
        module.touch()
    return tmp_path


def _wheel(
    dist: Path,
    version: str,
    *,
    extra: dict[str, str] | None = None,
    omit: tuple[str, ...] = (),
    entry_points: str = ENTRY_POINTS,
    requires: str = REQUIRES,
) -> None:
    info = f"omnigent_muse-{version}.dist-info"
    files = {f"{PLUGIN}/{name}": "" for name in MODULES}
    files[f"{info}/METADATA"] = (
        "Metadata-Version: 2.4\n"
        "Name: omnigent-muse\n"
        f"Version: {version}\n"
        f"Requires-Dist: {requires}\n"
    )
    files[f"{info}/entry_points.txt"] = entry_points
    files.update(extra or {})
    for name in omit:
        del files[name]
    with zipfile.ZipFile(dist / f"omnigent_muse-{version}-py3-none-any.whl", "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)


def _sdist(dist: Path, version: str, *, pyproject: str = "[project]\n") -> None:
    root = f"omnigent_muse-{version}"
    files = {
        "LICENSE": "",
        "README.md": "",
        "PKG-INFO": "",
        "pyproject.toml": pyproject,
    }
    with tarfile.open(dist / f"{root}.tar.gz", "w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            member = tarfile.TarInfo(f"{root}/{name}")
            member.size = len(data)
            tf.addfile(member, io.BytesIO(data))


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    return dist


@pytest.mark.parametrize(
    ("version", "tag", "prerelease"),
    [
        ("0.1.0", "v0.1.0", "false"),
        ("0.1.1.post1", "v0.1.1.post1", "false"),
        ("0.1.0rc1", "v0.1.0rc1", "true"),
        ("0.2.0b2", "v0.2.0b2", "true"),
        ("0.2.0.dev3", "v0.2.0.dev3", "true"),
    ],
)
def test_version_accepts_a_matching_tag(
    tmp_path: Path, version: str, tag: str, prerelease: str
) -> None:
    result = _run(_project(tmp_path, version), "version", tag, prerelease)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == version


@pytest.mark.parametrize(
    ("version", "tag", "prerelease", "message"),
    [
        ("0.1.0", "v0.1.1", "false", "does not match"),
        ("0.1.0", "0.1.0", "false", "does not match"),
        ("0.1.0", "v0.1.0", "true", "is marked as a pre-release"),
        ("0.1.0rc1", "v0.1.0rc1", "false", "is not a final version"),
        ("0.1.0", "v0.1.0", "yes", "must be 'true' or 'false'"),
    ],
)
def test_version_rejects_a_mismatch(
    tmp_path: Path, version: str, tag: str, prerelease: str, message: str
) -> None:
    result = _run(_project(tmp_path, version), "version", tag, prerelease)

    assert result.returncode == 1
    assert message in result.stderr


def test_dist_accepts_the_expected_files(tmp_path: Path) -> None:
    dist = _dist(_project(tmp_path, "0.1.0"))
    _wheel(dist, "0.1.0")
    _sdist(dist, "0.1.0")
    # uv build writes this next to the distributions.
    (dist / ".gitignore").write_text("*\n")

    result = _run(tmp_path, "dist", str(dist))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("wheel_kwargs", "sdist_pyproject", "message"),
    [
        (
            {"extra": {"omnigent/__init__.py": ""}},
            "[project]\n",
            "ships core namespace packages",
        ),
        (
            {"extra": {"tests/test_x.py": ""}},
            "[project]\n",
            "outside the plugin",
        ),
        ({"entry_points": ""}, "[project]\n", "entry point"),
        (
            {"omit": (f"{PLUGIN}/inner/__init__.py",)},
            "[project]\n",
            "missing plugin modules",
        ),
        (
            {"omit": ("omnigent_muse-0.1.0.dist-info/entry_points.txt",)},
            "[project]\n",
            "wheel is missing omnigent_muse-0.1.0.dist-info/entry_points.txt",
        ),
        ({"requires": "omnigent<1,>=0.1"}, "[project]\n", "pyproject.toml declares"),
        ({"requires": "omnigent>=0.16.0"}, "[project]\n", "pyproject.toml declares"),
        (
            {"requires": "omnigent-other<1,>=0.16.0"},
            "[project]\n",
            "pyproject.toml declares",
        ),
        (
            {},
            '[tool.uv.sources]\nomnigent = { path = "../omnigent" }\n',
            "[tool.uv.sources]",
        ),
    ],
)
def test_dist_rejects_a_broken_distribution(
    tmp_path: Path, wheel_kwargs: dict, sdist_pyproject: str, message: str
) -> None:
    dist = _dist(_project(tmp_path, "0.1.0"))
    _wheel(dist, "0.1.0", **wheel_kwargs)
    _sdist(dist, "0.1.0", pyproject=sdist_pyproject)

    result = _run(tmp_path, "dist", str(dist))

    assert result.returncode == 1
    assert message in result.stderr


def test_dist_rejects_files_for_another_version(tmp_path: Path) -> None:
    dist = _dist(_project(tmp_path, "0.1.1"))
    _wheel(dist, "0.1.0")
    _sdist(dist, "0.1.0")

    result = _run(tmp_path, "dist", str(dist))

    assert result.returncode == 1
    assert "expected one omnigent_muse-0.1.1" in result.stderr


def test_dist_accepts_reordered_specifiers(tmp_path: Path) -> None:
    dist = _dist(_project(tmp_path, "0.1.0", dependency="omnigent<2,>=0.17.0"))
    _wheel(dist, "0.1.0", requires="omnigent >=0.17.0, <2")
    _sdist(dist, "0.1.0")

    result = _run(tmp_path, "dist", str(dist))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("dependency", ["omnigent>=0.16.0", "omnigent<1", "omnigent"])
def test_dist_rejects_an_unbounded_declaration(tmp_path: Path, dependency: str) -> None:
    dist = _dist(_project(tmp_path, "0.1.0", dependency=dependency))
    _wheel(dist, "0.1.0", requires=dependency)
    _sdist(dist, "0.1.0")

    result = _run(tmp_path, "dist", str(dist))

    assert result.returncode == 1
    assert "must bound omnigent" in result.stderr


def test_floor_prints_the_declared_minimum(tmp_path: Path) -> None:
    result = _run(
        _project(tmp_path, "0.1.0", dependency="omnigent<1,>=0.17.2"), "floor"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.17.2"


@pytest.fixture
def release_checks(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The script as a module, with sleeps skipped, for stubbing the index."""
    spec = importlib.util.spec_from_file_location("check_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    return module


def _served(dist: Path) -> bytes:
    return json.dumps(
        {
            "urls": [
                {
                    "filename": p.name,
                    "digests": {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()},
                }
                for p in sorted(dist.iterdir())
            ]
        }
    ).encode()


def _index(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, responses: list
) -> list[str]:
    """Answer each urlopen with the next response, raising it if it is an error."""
    urls: list[str] = []

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        urls.append(url)
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return io.BytesIO(response)

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    return urls


def _published_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dist = _dist(_project(tmp_path, "0.1.0"))
    (dist / "omnigent_muse-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (dist / "omnigent_muse-0.1.0.tar.gz").write_bytes(b"sdist")
    monkeypatch.chdir(tmp_path)
    return dist


def test_published_retries_until_the_index_serves_the_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_checks: ModuleType
) -> None:
    dist = _published_dist(tmp_path, monkeypatch)
    urls = _index(
        monkeypatch,
        release_checks,
        [
            urllib.error.HTTPError("u", 404, "Not Found", {}, None),  # type: ignore[arg-type]
            http.client.RemoteDisconnected("closed"),
            ConnectionResetError("reset"),
            http.client.IncompleteRead(b"{"),
            b'{"urls": null}',
            b"not json",
            _served(dist),
        ],
    )

    release_checks.check_published("testpypi", dist, attempts=7, delay=0)

    assert urls[-1] == "https://test.pypi.org/pypi/omnigent-muse/0.1.0/json"
    assert len(urls) == 7


def test_published_rejects_different_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_checks: ModuleType
) -> None:
    dist = _published_dist(tmp_path, monkeypatch)
    served = _served(dist)
    (dist / "omnigent_muse-0.1.0.tar.gz").write_bytes(b"rebuilt sdist")
    _index(monkeypatch, release_checks, [served, served])

    with pytest.raises(release_checks.CheckError, match="do not match"):
        release_checks.check_published("pypi", dist, attempts=2, delay=0)


def test_published_rejects_an_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_checks: ModuleType
) -> None:
    dist = _dist(_project(tmp_path, "0.1.0"))
    monkeypatch.chdir(tmp_path)
    _index(monkeypatch, release_checks, [])

    with pytest.raises(release_checks.CheckError, match="no distributions"):
        release_checks.check_published("pypi", dist, attempts=1, delay=0)


def test_published_rejects_an_unknown_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_checks: ModuleType
) -> None:
    dist = _published_dist(tmp_path, monkeypatch)

    with pytest.raises(release_checks.CheckError, match="index must be one of"):
        release_checks.check_published("example", dist)
