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
      DIR holds exactly one wheel and one sdist for this version. The wheel
      has every plugin module under src/, the harness entry point, and the
      same bounded ``omnigent`` requirement as pyproject.toml, and nothing
      that would break a user install (a namespace ``__init__.py`` that
      shadows core, or the local uv source override in the sdist).
  spec minimum|latest
      Prints the Omnigent requirement to test against: ``omnigent==<floor>``
      for the minimum pyproject.toml declares, or the declared range itself
      for the latest release it allows.
  discovery [--installed] [--version VERSION]
      Run with the Python of the environment to check. Omnigent loads the
      muse harness with no plugin load errors. --installed also requires the
      plugin to import from site-packages (a real install, not the checkout),
      and --version that the installed omnigent-muse is VERSION.
  published INDEX DIR
      INDEX ("pypi" or "testpypi") serves exactly the files in DIR, with the
      same sha256 digests: the validated artifacts are the ones published.
      Retries while the index catches up, including over dropped
      connections, and fails at once if it serves a file with another
      digest.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.metadata
import json
import re
import sys
import tarfile
import time
import tomllib
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path

PROJECT = "omnigent-muse"
DIST_NAME = "omnigent_muse"
PLUGIN_DIR = "omnigent/community/harness/muse"
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


def _plugin_modules() -> list[str]:
    """Every module under src/ that the wheel must ship."""
    src = Path("src")
    modules = sorted(
        p.relative_to(src).as_posix() for p in (src / PLUGIN_DIR).rglob("*.py")
    )
    if not modules:
        raise CheckError(f"no plugin modules found under {src / PLUGIN_DIR}")
    return modules


def _omnigent_specifiers(requirement: str) -> frozenset[str] | None:
    """The specifiers of an unconditional ``omnigent`` requirement, else None."""
    match = re.fullmatch(
        r"\s*omnigent\s*(?:\[[^\]]*\])?\s*\(?\s*((?:[<>=!~][^;()]*)?)\)?\s*",
        requirement,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return frozenset(s.replace(" ", "") for s in match.group(1).split(",") if s.strip())


def _declared_omnigent(project: dict) -> frozenset[str]:
    found = [
        specs
        for r in project["project"].get("dependencies", [])
        if (specs := _omnigent_specifiers(r)) is not None
    ]
    if len(found) != 1:
        raise CheckError("pyproject.toml must declare exactly one omnigent dependency")
    specs = found[0]
    floors = [s for s in specs if s.startswith(">=")]
    caps = [s for s in specs if s.startswith("<") and not s.startswith("<=")]
    if len(floors) != 1 or len(caps) != 1:
        raise CheckError(
            "pyproject.toml must bound omnigent with one >= and one <, "
            f"not {sorted(specs)}"
        )
    return specs


def omnigent_spec(leg: str) -> str:
    specs = _declared_omnigent(_project())
    if leg == "minimum":
        (floor,) = [s[2:] for s in specs if s.startswith(">=")]
        return f"omnigent=={floor}"
    if leg == "latest":
        return f"omnigent{','.join(sorted(specs))}"
    raise CheckError(f"leg must be 'minimum' or 'latest', not {leg!r}")


def _read(archive: zipfile.ZipFile, name: str) -> str:
    try:
        return archive.read(name).decode()
    except KeyError:
        raise CheckError(f"wheel is missing {name}") from None


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
        metadata = Parser().parsestr(_read(zf, f"{info}/METADATA"))
        entry_points = _read(zf, f"{info}/entry_points.txt")
    modules = set(_plugin_modules())
    shipped = {n for n in names if n.startswith(f"{PLUGIN_DIR}/") and n.endswith(".py")}
    if missing := sorted(modules - shipped):
        raise CheckError(f"wheel is missing plugin modules: {missing}")
    if extra := sorted(shipped - modules):
        raise CheckError(f"wheel ships modules that are not in src/: {extra}")
    shadowing = [m for m in CORE_NAMESPACES if m in names]
    if shadowing:
        raise CheckError(f"wheel ships core namespace packages: {shadowing}")
    stray = sorted(n for n in names if not n.startswith((f"{PLUGIN_DIR}/", f"{info}/")))
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
    declared = _declared_omnigent(project)
    required = [s for r in requires if (s := _omnigent_specifiers(r)) is not None]
    if required != [declared]:
        raise CheckError(
            f"wheel requires omnigent as {requires}, but pyproject.toml declares "
            f"{','.join(sorted(declared))}"
        )

    with tarfile.open(sdist) as tf:
        root = f"{DIST_NAME}-{version}"
        members = set(tf.getnames())
        for needed in ("LICENSE", "README.md", "pyproject.toml", "PKG-INFO"):
            if f"{root}/{needed}" not in members:
                raise CheckError(f"sdist is missing {needed}")
        extracted = tf.extractfile(f"{root}/pyproject.toml")
        if extracted is None:
            raise CheckError("sdist pyproject.toml is not a regular file")
        sdist_project = tomllib.loads(extracted.read().decode())
    if "sources" in sdist_project.get("tool", {}).get("uv", {}):
        raise CheckError("sdist pyproject.toml still has a [tool.uv.sources] override")
    print(f"{wheel.name} and {sdist.name} look right")


def check_discovery(options: list[str]) -> None:
    installed = "--installed" in options
    rest = [o for o in options if o != "--installed"]
    match rest:
        case []:
            version = None
        case ["--version", version]:
            pass
        case _:
            raise CheckError(f"unknown discovery options: {options}")

    # Imported here: the other subcommands run without Omnigent installed.
    from omnigent.harness_plugins import plugin_state, valid_harnesses

    from omnigent.community.harness.muse import plugin

    if installed and "site-packages" not in (plugin.__file__ or ""):
        raise CheckError(f"plugin imports from {plugin.__file__}, not site-packages")
    found = importlib.metadata.version(PROJECT)
    if version is not None and found != version:
        raise CheckError(f"installed {PROJECT} is {found}, not {version}")
    state = plugin_state()
    if state.load_errors:
        raise CheckError(f"plugin load errors: {state.load_errors}")
    if "muse" not in valid_harnesses():
        raise CheckError(f"muse is not a valid harness: {sorted(valid_harnesses())}")
    print(
        f"{PROJECT} {found} with omnigent {importlib.metadata.version('omnigent')}: "
        "muse harness discovered"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_published(
    index: str, dist: Path, attempts: int = 20, delay: float = 30.0
) -> None:
    if index not in INDEXES:
        raise CheckError(f"index must be one of {sorted(INDEXES)}, not {index!r}")
    version = _project()["project"]["version"]
    expected = {p.name: _sha256(p) for p in _artifacts(dist)}
    if not expected:
        raise CheckError(f"no distributions in {dist} to compare with {index}")
    url = f"{INDEXES[index]}/pypi/{PROJECT}/{version}/json"
    served: dict[str, str] = {}
    for attempt in range(1, attempts + 1):
        served = {}
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                release = json.load(response)
            served = {f["filename"]: f["digests"]["sha256"] for f in release["urls"]}
        # OSError covers URLError, timeouts and dropped connections (which
        # urllib does not wrap); HTTPException covers truncated responses.
        except (
            OSError,
            http.client.HTTPException,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            print(f"attempt {attempt}: {url} not ready ({exc})")
        if served == expected:
            print(f"{index} serves the validated artifacts for {PROJECT} {version}")
            return
        # Waiting only helps while files are still missing. A file that is
        # not ours, or one with another digest, will not change.
        wrong = sorted(n for n in served if expected.get(n) != served[n])
        if wrong:
            raise CheckError(
                f"{index} serves {wrong} with digests that do not match the "
                f"validated artifacts: served {served}, expected {expected}"
            )
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
            case ["discovery", *options]:
                check_discovery(options)
            case ["spec", leg]:
                print(omnigent_spec(leg))
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
