# Integration tests

`tests/integration` drives a real `muse serve` end to end through Omnigent's
harness runner. Each test starts the `muse` harness with Omnigent's
`HarnessProcessManager`, which launches the shared harness runner subprocess.
That subprocess discovers the installed plugin through its entry point, builds
the harness app, and spawns `muse serve`. Turns are sent over the harness HTTP
contract (`POST /v1/sessions/{id}/events`) and read back as SSE, so the tests
exercise the same path a user's runner does. The unit suite covers the same
code against the scripted host in `tests/fixtures/fake_msp_host.py`.

No credentials are needed. Turns use Muse's `echo` provider. The failure test
uses `meta` with no login, in a private `HOME`.

## Coverage

| Behavior | Integration (real Muse) | Unit (fake MSP host) |
|----------|-------------------------|----------------------|
| Process spawn and MSP handshake | yes | yes |
| Multi-turn streaming on one session | yes | yes |
| Usage on the completed response | yes (echo reports zero tokens) | yes |
| Terminal turn failure (`authRequired`) | yes | yes |
| Respawn after `muse serve` is killed | yes | yes |
| Approval round trip | no | yes |
| Interrupt / cancel | no | yes |

After `muse serve` dies, the next turn fails fast with a `Muse transport
error`. The turn after that spawns a fresh `muse serve`. The respawn test pins
this behavior.

## Why approvals and interrupts are unit-only

There is no credential-free way to make a real Muse request a tool approval or
run a turn long enough to interrupt. This was checked against Muse
1.4.8-R6256.1:

- `echo` replies `echo: <prompt>` immediately with zero usage. It never calls
  tools, requests approvals, or fails.
- `local` is not an endpoint provider. It runs models on-device through an
  embedded llama.cpp, behind the `local_models` experiment
  (`MUSE_EXPERIMENTAL_LOCAL_MODELS`). On macOS it requires macOS 14+ and at
  least 64 GiB of RAM. It cannot be pointed at a mock model server, so
  upstream Omnigent's mock-LLM pattern does not carry over.
- `meta` accepts a base-URL override (`--base-url`, or the
  `endpoint_transport.base_url` setting). Muse first fetches a private model
  catalog (`GET /muse-code/models`) and strictly validates its rows, then
  streams from an undocumented responses API. A mock of that would be
  reverse-engineered and could break on any Muse release, so it was not built.

If Muse ships a scriptable test provider, or documents the `meta` endpoint
protocol, these tests can be added.

## CI

`.github/workflows/integration.yml` runs on pull requests, pushes to `main`,
the weekly schedule, and manual dispatch. It runs behind
`security-gate.yml`, declares no secrets, and blanks provider credentials. The
matrix covers the minimum supported Omnigent release (0.16.0, pinned by
commit) and Omnigent `main`. Omnigent and the built plugin wheel are installed
non-editable into a fresh virtual environment, with no local source override.

`.github/scripts/install-muse.sh` installs Muse. It downloads one release
binary directly and checks it against a pinned sha256. The check runs on every
run, including when the download comes from the cache, and the script never
runs the self-updating `curl … | bash` installer. To bump Muse, take
`version` and `artifacts.<platform>.checksum` from the release manifest at
`https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version=<VERSION>&file=manifest.json`
and update `MUSE_VERSION` and both Linux checksums together.

## Running locally

```sh
just integration    # needs muse on PATH, or OMNIGENT_MUSE_PATH=/path/to/muse
```

Without `OMNIGENT_MUSE_INTEGRATION=1` (which the recipe sets) the tests are
skipped, so `just test` stays offline. With it set, a missing `muse` binary
fails the run instead of skipping. Either the installer's `muse` launcher or a
release binary works; the tests pass `MUSE_NO_AUTO_UPDATE=1` through so the
launcher doesn't update itself mid-run.

The tests give Muse a private `HOME`, which hides a login stored in files
(Linux, and the macOS fallback). On macOS, though, `muse login` normally
stores Meta credentials in the Keychain, which a private `HOME` does not
hide. If you are logged in there, Muse may authenticate and
`test_unauthenticated_provider_fails_the_turn` can fail locally. CI runs on
Linux with no stored credentials and isn't affected.
