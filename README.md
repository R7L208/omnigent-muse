# omnigent-muse

A community-plugin harness that adds **Muse Code** to [Omnigent](https://github.com/omnigent-ai/omnigent).

Muse is an interactive terminal coding agent that serves the **Muse Session Protocol
(MSP)** over stdio via `muse serve`. This package registers a headless `muse` harness
through Omnigent's `omnigent.community.harness` entry point and drives it with a
vendored async MSP client (`inner/msp_client.py`). Vendoring is deliberate: the
published [`muse-code-sdk`](https://github.com/meta-models/muse-code-sdk) 1.3.1 is
fingerprint-pinned to host 1.3.0 and rejects current CLI releases, with no newer SDK
on PyPI — so the SDK can't be a runtime dependency until Meta publishes a matching
release. The vendored client is a thin seam that a future SDK can replace.

Open a `TurnStream` before submitting its turn. Text deltas are correlated to
their turn through `item/started`; deltas for unknown items are dropped. Usage,
approvals, and terminal events require a matching `turnId`. This live stream does
not replay or seed a snapshot for consumers attaching after a turn has begun.

> **Status.** The harness is discoverable and runnable. Its executor translates Muse
> streaming output, reasoning, approvals, usage, cancellation, and failures into
> Omnigent events through the vendored MSP transport.

## Install (local dev)

```sh
# from this directory, with a sibling ../omnigent checkout
uv pip install .
```

Use a regular (non-editable) install: editable installs resolve through PEP 660
finder hooks rather than plain `sys.path` entries, which core's namespace
extending can't see — the plugin's modules won't import.

## Verify discovery

```sh
python -c "from omnigent.harness_plugins import valid_harnesses; assert 'muse' in valid_harnesses(); print('ok')"
python -c "from omnigent.harness_plugins import plugin_state; print(plugin_state().load_errors)"  # {}
pytest -q
```

## Requirements

- `muse` CLI on PATH (or set `OMNIGENT_MUSE_PATH`). Install: `curl -fsSL https://dev.meta.ai/install.sh | bash`.
- Transport verified live against `muse` 1.4.0 (echo provider). The client records
  the served schema fingerprint without gating on it.

## Runtime configuration

Muse options can be declared on an Omnigent agent spec. Environment variables
override the spec; per-turn model and reasoning-effort values override both.

```yaml
executor:
  type: omnigent
  model: muse-large
  reasoning_effort: high          # none, minimal, low, medium, high, xhigh, max, ultra
  config:
    harness: muse
    provider: meta                  # meta, echo, local
    approval_mode: onRequest       # allowAll, promptUnmatched, onRequest, denyUnmatched
    turn_idle_timeout: 300         # seconds; finite and greater than zero
    env_passthrough: [GITHUB_TOKEN]
os_env:
  type: caller_process
  sandbox:
    type: none
    env_passthrough: [AWS_PROFILE]
```

The corresponding harness-process variables are
`HARNESS_MUSE_PROVIDER`, `HARNESS_MUSE_APPROVAL_MODE`, `HARNESS_MUSE_REASONING_EFFORT`,
`HARNESS_MUSE_TURN_IDLE_TIMEOUT`, `HARNESS_MUSE_OS_ENV` (JSON), and
`HARNESS_MUSE_ENV_PASSTHROUGH` (comma-separated names). Invalid values fail
before `muse serve` starts. Passthrough is exact-name and deny-by-default:
variables not in the shared safe base or either explicit passthrough list are
not inherited by Muse. The resolved timeout and allowlist are retained by the
transport factory and therefore also apply after a transport respawn.

When `provider` is omitted, Muse uses its configured provider or its `meta`
default. The `echo` provider is credential-free and useful for transport smoke
tests; `meta` requires Muse-owned authentication through `muse login`,
`muse auth set`, or `META_API_KEY`.

See [docs/authentication.md](docs/authentication.md) for per-provider setup,
readiness checks, and troubleshooting authentication failures.

## Omnigent tools

The Omnigent tools an agent is given (session, agent, policy, skill and web
tools) are exposed to Muse as an MCP server named `omnigent_<random>`, so
Muse sees them as `mcp__omnigent_<random>__<tool>` next to its built-in tools.
Each Muse session gets its own relay, with a fresh name, started with the
session and stopped when the session ends or the transport is respawned. The
random suffix keeps a workspace `.mcp.json` server from posing as the relay.

Calls go through Omnigent's policy (allow, ask, deny) when they are
dispatched, and approval cards appear in Omnigent as for other harnesses.
Muse's own approval prompt for these tools is answered automatically with a
one-time grant, so a call is never approved twice and no "always allow" rule
is written to your Muse settings. While an Omnigent tool call is still
running, including one waiting on an approval card, the turn idle timeout
(`turn_idle_timeout`) does not fire.

The relay is best effort: if it cannot start, the session runs with Muse's
built-in tools only and a warning is logged. Sandboxed sessions do not get
Omnigent tools yet (see below).

## Sandboxing

When `os_env.sandbox` selects the `linux_bwrap` backend, `muse serve` is
started through Omnigent's sandbox launcher, so Muse and every process it launches (including its shell tool)
run under the spec's filesystem, network, and environment restrictions. The
policy is resolved once when the harness starts, and every respawned
transport reuses it.

```yaml
os_env:
  type: caller_process
  sandbox:
    type: linux_bwrap
    write_paths: ["."]             # the workspace is read-only unless granted
```

Inside the sandbox the harness:

- runs the installed `muse-bin-<version>` binary directly (the `muse`
  installer wrapper self-updates and reads files the sandbox hides) and sets
  `MUSE_NO_AUTO_UPDATE=1`;
- passes `--disable-sandbox`, because Muse's own shell sandbox needs user
  namespaces that the outer sandbox denies — Omnigent's sandbox is the
  boundary;
- runs Muse from a private home, `$OMNIGENT_DATA_DIR/muse-sandbox/<hash>`
  (default `~/.omnigent/...`), one per workspace, and grants write access to
  that home only. Sessions, plugins, skills, memory and settings written by a
  sandboxed run stay there, so the unsandboxed `muse` never loads them;
- signs Muse in with your login: `auth.json` and `settings.json` are copied
  in from your Muse config directory (`$XDG_CONFIG_HOME/muse`, default
  `~/.config/muse`). Nothing written inside the sandbox reaches your own
  login: a token refresh made there stays in the private home, and is replaced
  when you log in again (or dropped when you log out). `trust.json` is not
  copied, so a sandboxed run starts with no trusted workspaces.
  `META_API_KEY` reaches Muse only when it is passed through, as unsandboxed;
- does not expose Omnigent tools: the relay's bridge directory is not
  reachable from inside the sandbox yet, so the session runs with Muse's
  built-in tools only;
- hides your own Muse directories (`~/.config/muse`, `~/.local/share/muse`,
  `~/.local/state/muse`, `~/.cache/muse`, or their `$XDG_*_HOME`
  equivalents), which a broader read grant would otherwise expose. A path the
  spec grants inside one of them stays visible.

The login is readable inside the sandbox, so its shell tool can read your
Muse token, as with Omnigent's Claude and Codex harnesses.

`sandbox.type: none` (or no `os_env`) leaves Muse unsandboxed. These
configurations fail before `muse serve` starts, never falling back to an
unsandboxed launch:

- a backend that cannot confine the Muse process tree (e.g.
  `windows_jobobject`) or that is unavailable on this host (e.g. `bwrap`
  missing);
- `darwin_seatbelt` (the macOS default for `type: auto`): Muse cannot run
  under Omnigent's Seatbelt profile yet, so use `type: none` on macOS;
- `allow_network: false` with any provider except `echo` — the `meta` and
  `local` providers need the network;
- a `muse` installer wrapper whose selected binary is not installed.

On Ubuntu 24.04+ unprivileged bubblewrap also needs an AppArmor profile that
grants `userns` to `/usr/bin/bwrap`.

## License

Apache-2.0.
