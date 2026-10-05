# Muse Provider Authentication Setup

This guide explains how to set up authentication for the Omnigent Muse harness. The harness delegates authentication to Muse, which supports multiple providers with different authentication requirements.

## Supported Providers

The Muse harness supports three providers, each with distinct authentication characteristics:

### Meta Provider

The **`meta`** provider routes model calls to Meta's Model API. It requires authentication credentials.

**Setup steps:**

1. **Install Muse**: Ensure you have the Muse CLI installed.
   ```sh
   curl -fsSL https://dev.meta.ai/install.sh | bash
   ```

2. **Authenticate** using one of these approaches (in order of precedence):
   
   a. **Using `muse login` (interactive authentication):**
   ```sh
   muse login
   ```
   Follow the prompts to authenticate. Credentials are stored securely in Muse's auth store.
   
   **Recommended**: This method is the most secure for local development because credentials are managed by Muse and not exposed in shell history or command-line arguments.

   b. **Using Muse's credential management for programmatic access:**
   
   Muse provides secure credential storage that can be configured via its CLI. Refer to the Muse documentation for the exact command syntax to store API keys securely without exposing them in command-line arguments or shell history.
   
   c. **Using the `META_API_KEY` environment variable (CI/CD environments):**
   
   For CI/CD systems, set the `META_API_KEY` environment variable from a secure secret manager (e.g., GitHub Secrets, GitLab CI/CD variables). **Never hardcode credentials or use shell export commands with literal keys**, as this exposes them in shell history and logs.
   
   Example (GitHub Actions):
   ```yaml
   env:
     META_API_KEY: ${{ secrets.META_API_KEY }}
   ```

3. **Precedence order**: Muse checks credentials in this order:
   - `META_API_KEY` environment variable (if set in the harness process)
   - Stored credential from Muse's credential store (configured via `muse login` or programmatic credential management)
   - Fall back to default provider if neither is available

**Security best practices:**
- **Never** pass credentials as command-line arguments (they appear in process listings and shell history).
- **Never** hardcode credentials in agent specifications, environment exports, or config files.
- Use `muse login` for interactive authentication in local development.
- Use secure secret managers (CI/CD variables, HashiCorp Vault, etc.) for automated deployments.

### Echo Provider

The **`echo`** provider is credential-free and designed for testing the MSP transport layer without connecting to any model API. It echoes back mock responses.

**Use cases:**
- Verifying the MSP connection between the harness and Muse
- Transport smoke tests to confirm the integration is wired correctly
- Development and testing workflows

**Setup:** No authentication required. Simply declare `provider: echo` in your agent spec or set the environment variable:
```sh
export HARNESS_MUSE_PROVIDER=echo
```

**Example verification:**
```yaml
executor:
  type: omnigent
  harness: muse
  model: echo
  config:
    provider: echo
```

### Local Provider

The **`local`** provider routes model calls to a local model instance available on your machine. Authentication requirements depend on your local setup.

**Setup:** Configure your local model provider according to Muse's `local` provider documentation, then declare it in your agent spec:
```yaml
executor:
  type: omnigent
  config:
    provider: local
```

**Note:** Specific authentication steps for the `local` provider depend on your local model configuration. Refer to the Muse documentation for details on configuring local model endpoints.

## Provider Configuration

### Via Agent Specification

Declare the provider in your Omnigent agent YAML:

```yaml
executor:
  type: omnigent
  harness: muse
  model: muse-large           # or appropriate model for your provider
  config:
    provider: meta            # or echo, local
```

### Via Environment Variables

Override spec-declared values with environment variables (takes precedence):

```sh
export HARNESS_MUSE_PROVIDER=meta
```

The `HARNESS_MUSE_PROVIDER` environment variable accepts: `meta`, `echo`, or `local`.

### Default Behavior

If `provider` is omitted in both the spec and environment:
- The harness uses Muse's configured default provider
- Muse's default is typically `meta`

## Verification Workflow

Before running a full Omnigent workload, verify that your provider and authentication are correctly configured.

### 1. Verify Muse is installed

```sh
muse --version
```

Check that the Muse CLI is on your PATH and working. Expected output: version information (e.g., `muse 1.4.0`).

### 2. Verify the harness can load the Muse plugin

```sh
python -c "from omnigent.harness_plugins import valid_harnesses; assert 'muse' in valid_harnesses(); print('Muse harness discovered')"
```

This confirms that the Omnigent Muse harness plugin is discoverable. If this fails, verify that you have installed the omnigent-muse package correctly.

### 3. Check Muse configuration and credentials

Run Muse's own verification commands to confirm your provider and authentication are set up:

```sh
muse --help
```

Review Muse's help output for commands that verify your authentication status and provider configuration. The specific commands depend on your Muse version.

**For meta provider**: If you've run `muse login`, Muse stores credentials in its configured auth location (typically `~/.config/muse/auth.json`). Muse provides commands to verify this configuration — check the help output or Muse documentation for the exact command syntax.

**For echo provider**: The echo provider is credential-free. If Muse is installed and the echo provider is available, it should work without additional setup.

### 4. Test in a non-workload context (optional)

Once Muse verification succeeds, you can optionally test your chosen provider in isolation (outside of an Omnigent workload). Refer to the Muse documentation for examples of running Muse commands with your chosen provider.

**Avoid**: Do not start full Omnigent workloads yet if auth verification failed, as `authRequired` errors will surface as terminal turn failures after the MSP connection initializes (see troubleshooting below).

## Troubleshooting Authentication Failures

### `authRequired` Error After Successful MSP Connection

The most common authentication issue is `authRequired` arriving as a **terminal turn failure** after the MSP connection initializes successfully. This can be misleading because:

1. The MSP connection (stdio-based) initializes correctly
2. The first turn starts without apparent transport errors
3. The error surfaces only after Muse attempts to call the model API

This is **not a transport problem** — it indicates the selected provider cannot authenticate. Verify your setup using the **verification workflow** above.

### Common Auth Failures and Solutions

#### Meta Provider Authentication Failed

**Exact Message:**
```
Muse provider authentication failed (provider=meta, authRequired). Run `muse login` or `muse auth set`, or set META_API_KEY in the harness environment.
```

*Source: STRINGS-13.md, src/omnigent/community/harness/muse/inner/muse_executor.py:413-414*

- **Solution**: 
  1. Run `muse login` to authenticate interactively in your browser. This is the recommended method for local development.
  2. Alternatively, consult Muse documentation for the correct syntax to use `muse auth set` to store credentials securely without exposing them in command-line arguments.
  3. For CI/CD environments, set `META_API_KEY` as an environment variable from a secure secret manager (not as a literal value in code or shell commands).
  
  **Example for CI/CD (GitHub Actions):**
  ```yaml
  env:
    META_API_KEY: ${{ secrets.META_API_KEY }}
  ```

#### Echo Provider Authentication Failed

**Exact Message:**
```
Muse provider authentication failed (provider=echo, authRequired). Echo provider requires no credentials. Verify configuration and try again.
```

*Source: STRINGS-13.md, src/omnigent/community/harness/muse/inner/muse_executor.py:410-412*

- **Cause**: Echo provider configuration is incorrect or the provider is not properly initialized.
- **Solution**: Verify that `HARNESS_MUSE_PROVIDER=echo` is set and that `muse` is running with the `--provider echo` flag. The echo provider should not require credentials.

#### Unknown Provider Authentication Failed

**Exact Message:**
```
Muse provider authentication failed (provider=unknown, authRequired). Check your Muse credentials and provider configuration.
```

*Source: STRINGS-13.md, src/omnigent/community/harness/muse/inner/muse_executor.py:407, 415-417*

- **Cause**: The provider could not be determined (e.g., `HARNESS_MUSE_PROVIDER` is not set, Muse's config is corrupted, or no provider is configured).
- **Solution**: Verify that `~/.config/muse/settings.json` exists and is valid JSON. Explicitly set the provider:
  ```sh
  export HARNESS_MUSE_PROVIDER=meta  # or echo, local
  ```
  Then check Muse's configuration documentation for your chosen provider.

#### Local Provider Authentication Failed

**Exact Message:**
```
Muse provider authentication failed (provider=local, authRequired). Check your Muse credentials and provider configuration.
```

*Source: STRINGS-13.md, src/omnigent/community/harness/muse/inner/muse_executor.py:415-417*

- **Cause**: Local provider authentication is failing, typically due to misconfigured credentials or local model endpoint not being available.
- **Solution**: Verify your local model provider setup according to Muse documentation, then retry.

#### Authentication Works Locally but Fails in CI/CD

- **Cause**: The `META_API_KEY` environment variable is not set in the CI/CD environment, and Muse's credential store (local to your machine) is not available in the CI/CD container/runner.
- **Solution**: Set `META_API_KEY` in your CI/CD environment variables from a secure secret manager. 
  
  **Example (GitHub Actions):**
  ```yaml
  env:
    META_API_KEY: ${{ secrets.META_API_KEY }}
  ```
  
  **Example (GitLab CI/CD):**
  ```yaml
  variables:
    META_API_KEY: $CI_JOB_TOKEN  # or configure via CI/CD settings
  ```
  
  **Important**: Never hardcode credentials or use plain `export` commands in CI/CD scripts. Use the CI/CD platform's secure secret management system.

#### Provider Mismatch

- **Cause**: The provider configured in the agent spec differs from the active provider in Muse or from what Muse expects based on credentials available.
- **Example**: Spec declares `provider: echo` but Muse is configured for `provider: meta` and `META_API_KEY` is set.
- **Solution**: Ensure the spec's `provider` value matches your Muse configuration and available credentials, or let the spec omit `provider` to use Muse's default.

### Debug Trace

For troubleshooting provider issues, enable tracing to see detailed provider interactions:

```sh
export MUSE_TRANSPORT_TRACE=1
```

Consult Muse documentation for details on what this trace output includes and how to use it safely in a debug context.

## Security Best Practices

1. **Never embed credentials in YAML specs**: Always use environment variables or Muse's credential storage.
2. **Never pass credentials as command-line arguments**: They appear in process listings (`ps`), shell history, and logs.
3. **Use `META_API_KEY` only in trusted environments**: The environment variable is visible to the harness process and should only be used in secure, automated deployments (CI/CD systems with secret management).
4. **Use `muse login` for interactive authentication**: For local development, `muse login` is the most secure approach.
5. **Verify your logging pipeline**: Ensure that `META_API_KEY` environment variable values are not captured in logs or configuration artifacts.

## Next Steps

Once you have verified your provider authentication:

1. Declare the provider in your Omnigent agent spec
2. Start your Omnigent workload
3. Monitor the first turn for authentication errors (which surface as terminal events, not transport errors)
4. If `authRequired` appears, revisit the **troubleshooting** section above

For more information on Muse and its providers, visit the [Muse Code documentation](https://meta-models.github.io/muse-code-sdk/).
