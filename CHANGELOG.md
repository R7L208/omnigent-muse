# Changelog

All notable changes to `omnigent-muse` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
the 0.x policy in [RELEASING.md](RELEASING.md#versioning).

Every pull request that changes user-visible behavior (runtime options,
supported Omnigent or Muse versions, errors users see, packaging) adds a line
under **Unreleased**, in the section that fits: Added, Changed, Deprecated,
Removed, Fixed, or Security. Internal refactors, tests, and CI changes are not
listed. A release PR renames **Unreleased** to the new version and date.

## [Unreleased]

### Added

- A headless `muse` harness for Omnigent, registered through the
  `omnigent.community.harness` entry point, that drives `muse serve` over the
  Muse Session Protocol (MSP) with a vendored async client.
- Translation of Muse streaming text, reasoning, usage, approvals,
  cancellation, and turn failures into Omnigent events.
- A transport that respawns `muse serve` after it exits, with a configurable
  turn idle timeout.
- Runtime options on the agent spec or as `HARNESS_MUSE_*` variables: provider,
  approval mode, reasoning effort, turn idle timeout, OS environment, and
  exact-name environment passthrough.
- Provider-aware authentication errors that say how to sign in to each Muse
  provider, and an authentication setup guide.
- Linux sandboxing of the Muse process tree through `os_env.sandbox`
  (`linux_bwrap`), with a private Muse home per workspace.

### Security

- Credentials are redacted from Muse-sourced error text and logs.
- Muse inherits only a safe base environment plus names passed through
  explicitly.

[Unreleased]: https://github.com/R7L208/omnigent-muse/commits/main
