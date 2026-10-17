#!/usr/bin/env python3
"""Release checks for .github/workflows/release.yml.

Stdlib only, so the checks need nothing installed and read only files and
public index metadata. Run from the repository root.

  version TAG PRERELEASE
      TAG is exactly ``v<project.version>``, and GitHub's pre-release flag
      ("true"/"false") agrees with the version: a/b/rc/dev versions are
      pre-releases (published to TestPyPI), everything else is final (PyPI).
      Prints the version.
  dist DIR
      DIR holds exactly one wheel and one sdist for this version, with the
      plugin modules, the harness entry point and the dependency floor, and
      without anything that would break a user install (a namespace
      ``__init__.py`` that shadows core, or the local uv source override).
  published INDEX DIR
      INDEX ("pypi" or "testpypi") serves exactly the files in DIR, with the
      same sha256 digests: the validated artifacts are the ones published.
      Retries while the index catches up.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path

PROJECT = "omnigent-muse"
DIST_NAME = "omnigent_muse"
PLUGIN_MODULES = (
    "omnigent/community/harness/muse/__init__.py",
    "omnigent/community/harness/muse/plugin.py",
    "omnigent/community/harness/muse/inner/msp_client.py",
    "omnigent/community/harness/muse/inner/msp_transport.py",
    "omnigent/community/harness/muse/inner/muse_executor.py",
    "omnigent/community/harness/muse/inner/muse_harness.py",
)
# Core owns these packages; shipping an __init__.py for one would shadow it.
CORE_NAMESPACES = (
    "omnigent/__init__.py",
    "omnigent/community/__init__.py",
    "omnigent/community/harness/__init__.py",
)
ENTRY_POINT = "muse = omnigent.community.harness.muse.plugin:get_contribution"
INDEXES = {"pypi": "https://pypi.org", "testpypi": "https://test.pypi.org"}
FINAL_RE = re.compile(r"^\d+\.\d+\.\d+(\.post\d+)?$")
PRERELEASE_RE = re.compile(r"^\d+\.\d+\.\d+((a|b|rc)\d+)?(\.dev\d+)?$")


class CheckError(Exception):
    pass


def _project() -> dict:
    with open("pyproject.toml", "rb") as f:
        return tomllib.load(f)


def check_version(tag: str, prerelease: str) -> str:
    version = _project()["project"]["version"]
    if tag != f"v{version}":
        raise CheckError(f"tag {tag!r} does not match project.version {version!r}")
    if prerelease not in ("true", "false"):
        raise CheckError(
            f"pre-release flag must be 'true' or 'false', not {prerelease!r}"
        )
    if prerelease == "true":
        if FINAL_RE.match(version) or not PRERELEASE_RE.match(version):
            raise CheckError(
                f"{version} is marked as a pre-release on GitHub but is not an "
                "a/b/rc/dev version; use e.g. 0.1.0rc1, or clear the flag"
            )
    elif not FINAL_RE.match(version):
        raise CheckError(
            f"{version} is not a final version; mark the GitHub Release as a "
            "pre-release to publish it to TestPyPI"
        )
    return version


def _artifacts(dist: Path) -> list[Path]:
    # uv build also writes a .gitignore into the output directory.
    return sorted(p for p in dist.iterdir() if not p.name.startswith("."))


def _one(dist: Path, pattern: str) -> Path:
    found = sorted(dist.glob(pattern))
    if len(found) != 1:
        raise CheckError(
            f"expected one {pattern} in {dist}, found {[p.name for p in found]}"
        )
    return found[0]


def check_dist(dist: Path) -> None:
    project = _project()
    version = project["project"]["version"]
    files = [p.name for p in _artifacts(dist)]
    wheel = _one(dist, f"{DIST_NAME}-{version}-py3-none-any.whl")
    sdist = _one(dist, f"{DIST_NAME}-{version}.tar.gz")
    if files != sorted((wheel.name, sdist.name)):
        raise CheckError(f"unexpected files in {dist}: {files}")

    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        info = f"{DIST_NAME}-{version}.dist-info"
        metadata = Parser().parsestr(zf.read(f"{info}/METADATA").decode())
        entry_points = zf.read(f"{info}/entry_points.txt").decode()
    missing = [m for m in PLUGIN_MODULES if m not in names]
    if missing:
        raise CheckError(f"wheel is missing plugin modules: {missing}")
    shadowing = [m for m in CORE_NAMESPACES if m in names]
    if shadowing:
        raise CheckError(f"wheel ships core namespace packages: {shadowing}")
    stray = sorted(
        n
        for n in names
        if not n.startswith(("omnigent/community/harness/muse/", f"{info}/"))
    )
    if stray:
        raise CheckError(f"wheel contains files outside the plugin: {stray}")
    if (
        "[omnigent.community.harness]" not in entry_points
        or ENTRY_POINT not in entry_points
    ):
        raise CheckError(
            f"wheel does not register the muse harness entry point:\n{entry_points}"
        )
    if metadata["Name"] != PROJECT or metadata["Version"] != version:
        raise CheckError(f"wheel metadata is {metadata['Name']} {metadata['Version']}")
    requires = metadata.get_all("Requires-Dist") or []
    if not any(r.replace(" ", "").startswith("omnigent<1,>=") for r in requires):
        raise CheckError(f"wheel does not require a bounded omnigent: {requires}")

    with tarfile.open(sdist) as tf:
        root = f"{DIST_NAME}-{version}"
        members = set(tf.getnames())
        for needed in ("LICENSE", "README.md", "pyproject.toml", "PKG-INFO"):
            if f"{root}/{needed}" not in members:
                raise CheckError(f"sdist is missing {needed}")
        extracted = tf.extractfile(f"{root}/pyproject.toml")
        assert extracted is not None
        sdist_project = tomllib.loads(extracted.read().decode())
    if "sources" in sdist_project.get("tool", {}).get("uv", {}):
        raise CheckError("sdist pyproject.toml still has a [tool.uv.sources] override")
    print(f"{wheel.name} and {sdist.name} look right")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_published(
    index: str, dist: Path, attempts: int = 20, delay: float = 30.0
) -> None:
    if index not in INDEXES:
        raise CheckError(f"index must be one of {sorted(INDEXES)}, not {index!r}")
    version = _project()["project"]["version"]
    expected = {p.name: _sha256(p) for p in _artifacts(dist)}
    url = f"{INDEXES[index]}/pypi/{PROJECT}/{version}/json"
    served: dict[str, str] = {}
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                release = json.load(response)
            served = {f["filename"]: f["digests"]["sha256"] for f in release["urls"]}
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            print(f"attempt {attempt}: {url} not ready ({exc})")
        if served == expected:
            print(f"{index} serves the validated artifacts for {PROJECT} {version}")
            return
        if served:
            print(f"attempt {attempt}: {index} serves {sorted(served)}")
        if attempt < attempts:
            time.sleep(delay)
    raise CheckError(
        f"{index} files {served} do not match the validated artifacts {expected}"
    )


def main(argv: list[str]) -> int:
    try:
        match argv:
            case ["version", tag, prerelease]:
                print(check_version(tag, prerelease))
            case ["dist", dist]:
                check_dist(Path(dist))
            case ["published", index, dist]:
                check_published(index, Path(dist))
            case _:
                print(__doc__, file=sys.stderr)
                return 2
    except CheckError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
