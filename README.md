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

`OSEnvSpec` is accepted and validated here, including its sandbox environment
allowlist. Process-tree sandbox enforcement is tracked separately in issue #6.

## License

Apache-2.0.
