from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github/scripts/release/check-release.py"
PLUGIN = "omnigent/community/harness/muse"
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


def _project(tmp_path: Path, version: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "omnigent-muse"\nversion = "{version}"\n'
    )
    return tmp_path


def _wheel(
    dist: Path,
    version: str,
    *,
    extra: dict[str, str] | None = None,
    entry_points: str = ENTRY_POINTS,
) -> None:
    info = f"omnigent_muse-{version}.dist-info"
    files = {
        f"{PLUGIN}/{name}": ""
        for name in (
            "__init__.py",
            "plugin.py",
            "inner/msp_client.py",
            "inner/msp_transport.py",
            "inner/muse_executor.py",
            "inner/muse_harness.py",
        )
    }
    files[f"{info}/METADATA"] = (
        "Metadata-Version: 2.4\n"
        "Name: omnigent-muse\n"
        f"Version: {version}\n"
        "Requires-Dist: omnigent<1,>=0.16.0\n"
    )
    files[f"{info}/entry_points.txt"] = entry_points
    files.update(extra or {})
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
