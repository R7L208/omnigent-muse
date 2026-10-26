# Releasing omnigent-muse

A release is a version-bump PR, then a GitHub Release. Publishing the Release
runs [`.github/workflows/release.yml`](.github/workflows/release.yml), which:

1. checks that the tag is exactly `v<project.version>`, that the GitHub
   pre-release flag matches the version, that the tag is annotated, and
   that the tagged commit is on `main`;
2. builds the wheel and sdist once, and checks them with `twine check` and
   [`check-release.py`](.github/scripts/release/check-release.py) (plugin
   modules, entry point, the same bounded Omnigent dependency as
   `pyproject.toml`, no namespace
   `__init__.py` that would shadow core, no `[tool.uv.sources]` override);
3. installs that wheel in a clean environment with the minimum supported
   Omnigent (the `>=` floor in `pyproject.toml`) and the latest release the
   `pyproject.toml` range allows. In each one it checks
   plugin discovery from outside the checkout, then runs the unit suite and
   the credential-free `echo` integration tests against the installed wheel;
4. publishes the same files through Trusted Publishing: final versions to PyPI
   (environment `pypi`), pre-releases to TestPyPI (environment `testpypi`);
5. checks that the index serves files with the same sha256 digests, then
   installs the release from the index and runs the `echo` tests again;
6. attaches the same files to the GitHub Release, once the smoke test
   passes.

No step uses a long-lived token. Only the publish job can mint an OIDC token,
and only after the environment's reviewer approves it.

## First-time setup

Do this once, before the first release.

1. **Check the name.** Confirm `omnigent-muse` is still free on
   [PyPI](https://pypi.org/project/omnigent-muse/) and
   [TestPyPI](https://test.pypi.org/project/omnigent-muse/).
2. **Add pending publishers.** In your
   [PyPI publishing settings](https://pypi.org/manage/account/publishing/) and
   in the [TestPyPI ones](https://test.pypi.org/manage/account/publishing/),
   add a GitHub pending publisher:

   | Field | PyPI | TestPyPI |
   |-------|------|----------|
   | PyPI project name | `omnigent-muse` | `omnigent-muse` |
   | Owner | `R7L208` | `R7L208` |
   | Repository name | `omnigent-muse` | `omnigent-muse` |
   | Workflow name | `release.yml` | `release.yml` |
   | Environment name | `pypi` | `testpypi` |

   The first successful publish turns each pending publisher into the
   project's trusted publisher.
3. **Create the environments.** In the repository's
   **Settings → Environments**, create `pypi` and `testpypi`. For each one:
   - add yourself under **Required reviewers**;
   - under **Deployment branches and tags**, choose **Selected branches and
     tags** and add the tag rule `v*`;
   - add no secrets. Trusted Publishing needs none.

## Rehearse on TestPyPI

Before the first release, and whenever the release workflow changes, publish
a release candidate to TestPyPI:

1. Open a PR that sets `version = "X.Y.ZrcN"` in `pyproject.toml`, and merge
   it.
2. Tag the merge commit with an annotated tag `vX.Y.ZrcN`, as in step 3 of
   the [release checklist](#release-checklist).
3. Create a GitHub Release from that existing tag, and check
   **Set as a pre-release**.
4. Check the run as described in
   [Approving a deployment](#approving-a-deployment), approve the `testpypi`
   deployment, then check that every job passes.

TestPyPI never accepts the same version twice, so each rehearsal needs the
next `rcN`.

## Release checklist

1. **Prepare the release PR.**
   - Set `version` in `pyproject.toml` to the new version (see
     [Versioning](#versioning)).
   - In `CHANGELOG.md`, rename **Unreleased** to `[X.Y.Z] - YYYY-MM-DD`, add
     an empty **Unreleased** above it, and update the comparison links at the
     bottom.
   - If the supported Omnigent range changed, update the `omnigent` bound in
     `pyproject.toml`. If the floor moved, also update the minimum leg in
     `integration.yml` (its name, comment and commit), the version in
     `README.md` and `docs/integration-tests.md`. `release.yml` and the unit
     CI read both bounds from `pyproject.toml`.
   - For a release that changes the Muse transport or provider handling, run
     the manual end-to-end pass with the `meta` provider (#16) and record the
     results in the PR.
2. **Merge it** once CI is green, and wait for the `main` push CI to pass on
   the merge commit.
3. **Tag the merge commit.** The workflow requires an annotated tag, and the
   Release form only creates lightweight ones:

   ```sh
   git fetch origin
   git tag -a vX.Y.Z -m "omnigent-muse X.Y.Z" origin/main
   git push origin vX.Y.Z
   ```

4. **Create the GitHub Release** from the existing tag `vX.Y.Z`. Use the
   version as the title, and paste the changelog section as the notes. Leave
   **Set as a pre-release** cleared for a final version. Publishing the
   Release starts the workflow.
5. **Approve the deployment** to `pypi` when the workflow asks, after the
   checks in [Approving a deployment](#approving-a-deployment).
6. **Check the result.** All jobs pass, the Release has the wheel and sdist
   attached, and <https://pypi.org/project/omnigent-muse/> shows the new
   version.

## Approving a deployment

A Release runs `release.yml` as it is at the tagged commit. Its "on `main`"
check therefore catches mistakes, but someone who can push a tag could tag a
branch where that check was edited out. The environment reviewer is the real
control. Before approving:

- Note the commit on the run's summary page, and confirm it is on `main`:
  `git fetch origin && git merge-base --is-ancestor <sha> origin/main`
  exits 0.
- Confirm the `build` job passed its "Check the tag is annotated and on
  main" step.

## If a release fails

Retry with **Re-run failed jobs**, never **Re-run all jobs**. A re-run of
failed jobs reuses the run's `dist` artifact; rebuilding produces files with
different sha256 digests from the ones already tested or uploaded.

- **Before publishing** (the tag, build, or verify jobs failed): nothing has
  been uploaded to the index or the Release. Delete the GitHub Release and
  the tag, fix the problem through a PR, and release again with the same
  version.
- **Publishing failed or was rejected** (for example a Trusted Publishing
  misconfiguration): nothing is attached to the Release until the smoke test
  passes. Fix the
  configuration, then re-run failed jobs. If one file was uploaded before the
  failure, the re-run skips it and uploads the rest.
- **After publishing**: PyPI never accepts the same version twice. If the
  published files are broken, yank that version on PyPI (with a reason), fix
  forward in a PR, and release the next patch version. If only the attach or
  smoke job failed and the published files are fine, re-run failed jobs.

## Versioning

`omnigent-muse` follows [semantic versioning](https://semver.org/) adapted
for the 0.x series:

- **Patch** (`0.1.0` → `0.1.1`): compatible fixes, documentation, and newly
  supported Omnigent or Muse versions.
- **Minor** (`0.1.x` → `0.2.0`): new features, and any breaking change, such
  as removing or renaming a runtime option, changing a default, or raising
  the minimum supported Omnigent, Muse, or Python version.
- **Pre-releases** (`0.2.0rc1`, `0.2.0b1`): optional, for rehearsing a
  release or for early testing. They publish to TestPyPI only.

Tags are always `v` plus the exact version in `pyproject.toml`.
