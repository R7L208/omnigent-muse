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

> **Status.** The harness is *discoverable* (id, alias `muse-code`, label, install
> spec, capabilities, catalog row, importable `create_app()`), and the MSP transport
> is implemented and hermetically tested — but the executor's `run_turn` is still a
> stub. Wiring it to the transport is the next step.

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

## License

MIT.
