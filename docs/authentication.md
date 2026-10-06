# Muse provider authentication

The Muse harness does not store or manage credentials. Authentication is owned
by Muse: the harness starts `muse serve`, and Muse authenticates with whichever
provider the session uses. This page covers how provider selection affects
authentication, how to prepare a Muse installation before running the harness,
and how to read the harness's authentication errors.

## Providers

| Provider | Credentials | Use it for |
|----------|-------------|------------|
| `meta`   | Required (Muse-owned) | Real model calls. Muse's default when no provider is configured. |
| `echo`   | None | Credential-free transport checks: MSP handshake, streaming, and event translation without a model. |
| `local`  | Depends on your local Muse setup | Models configured locally in Muse. See the [Muse Code documentation](https://meta-models.github.io/muse-code-sdk/). |

### Selecting a provider

Set the provider on the agent spec, or override it for the harness process with
`HARNESS_MUSE_PROVIDER` (environment variables override the spec):

```yaml
executor:
  type: omnigent
  config:
    harness: muse
    provider: meta                # meta, echo, local
```

When a provider is configured, the harness passes it to `muse serve` as
`--provider`. When it is omitted, Muse uses its configured provider or its
`meta` default.

## Setting up `meta`

Install Muse (`curl -fsSL https://dev.meta.ai/install.sh | bash`), then
authenticate Muse with one of:

- **`muse login`**: interactive device-code login; Muse stores the credential.
  This is the simplest option on a workstation. Run it as the same user the
  harness runs as. A running `muse serve` host picks up a login completed later
  on its next session start, resume, or fork.
- **`muse auth set`**: stores a credential in Muse's auth store without an
  interactive login. See `muse --help` for its usage on your Muse version,
  and prefer a form that does not put the key on the command line.
- **`META_API_KEY`**: for unattended environments such as CI. Two things are
  required:
  1. Inject the value from a secret manager or CI secret, never as a literal
     in a script, spec, or shell command:

     ```yaml
     # GitHub Actions
     env:
       META_API_KEY: ${{ secrets.META_API_KEY }}
     ```

  2. Allow it through to Muse. The harness launches `muse serve` with a
     deny-by-default environment, so `META_API_KEY` is dropped unless it is
     listed for passthrough:

     ```yaml
     executor:
       config:
         harness: muse
         provider: meta
         env_passthrough: [META_API_KEY]
     ```

     Names in `os_env.sandbox.env_passthrough` are always added as well.
     `HARNESS_MUSE_ENV_PASSTHROUGH` (comma-separated) also works, but when it
     is set in the harness environment it *replaces* `executor.config.env_passthrough`
     rather than adding to it, so include `META_API_KEY` there too.

## Verifying readiness

None of these start an Omnigent workload.

```sh
# Muse is installed and on PATH (or set OMNIGENT_MUSE_PATH)
muse --version

# Muse's own commands, including authentication, for your version
muse --help

# The harness is discoverable by Omnigent
python -c "from omnigent.harness_plugins import valid_harnesses; assert 'muse' in valid_harnesses(); print('ok')"
python -c "from omnigent.harness_plugins import plugin_state; print(plugin_state().load_errors)"  # {}
```

To separate transport problems from authentication problems, run your agent
once with `provider: echo`. If `echo` works and `meta` does not, the transport
is healthy and the issue is Muse authentication.

## Troubleshooting

### `authRequired` after a successful connection

A missing or expired credential does not fail the MSP handshake. `muse serve`
starts, the harness connects, and a session starts. The failure appears when
the first turn reaches the model, as a terminal turn failure. This is an
authentication problem, not a transport problem. The harness keeps the
session rather than tearing down the connection. After fixing the credential,
resend the turn; if the error persists, start a new session. A changed `META_API_KEY`
only takes effect once the harness process restarts with it.

The harness reports it as:

```
Muse provider authentication failed (provider=<provider>, authRequired). <hint>
```

`<provider>` is the provider Muse reported for the session. If Muse did not
report one, or reported an id other than `meta`, `echo`, or `local`, it is the
configured provider, and `unknown` if neither is available. Messages are built
only from fixed text and these provider ids, so they never include credential
values or Muse's raw error text.

#### `meta`

```
Muse provider authentication failed (provider=meta, authRequired). Run `muse login` or `muse auth set`, or set META_API_KEY and add it to executor.config.env_passthrough.
```

Authenticate Muse as described in [Setting up `meta`](#setting-up-meta). If you
rely on `META_API_KEY`, check that it is set in the harness process *and*
listed in `env_passthrough`.

#### Provider mismatch

```
Muse provider authentication failed (provider=<active>, authRequired). Muse used provider <active> but the harness is configured for <configured>; check executor.config.provider / HARNESS_MUSE_PROVIDER and Muse's default provider.
```

Muse ran the session with a different provider than the harness was configured
for. Check `executor.config.provider` and `HARNESS_MUSE_PROVIDER` (the
environment variable wins), and Muse's own default provider.

#### `echo`

```
Muse provider authentication failed (provider=echo, authRequired). Echo provider requires no credentials. Verify configuration and try again.
```

`echo` never needs credentials, so this points at configuration rather than
login. Do not run `muse login` for it. Check the provider settings above.

#### `local` or unknown provider

```
Muse provider authentication failed (provider=local, authRequired). Check your Muse credentials and provider configuration.
Muse provider authentication failed (provider=unknown, authRequired). Check your Muse credentials and provider configuration.
```

Check the provider's setup in Muse. For `unknown`, set `provider` explicitly so
the harness and Muse agree on which provider to use.

### Debugging provider calls

`MUSE_TRANSPORT_TRACE=1` makes Muse print raw provider request and response
lines to stderr; Muse scrubs credentials from this output. Like any variable,
it must be listed in `env_passthrough` to reach `muse serve`. Use it only while
debugging, and treat the output as sensitive.

## Keeping credentials safe

- Never put credentials in agent specs, command-line arguments, or committed
  files.
- Prefer `muse login` on workstations and CI secrets for `META_API_KEY`.
- Pass through only the variables Muse needs. Passthrough is exact-name and
  deny-by-default for that reason.
