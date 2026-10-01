# MSP transport review results

All seven comments on [PR #1](https://github.com/R7L208/omnigent-muse/pull/1) are addressed on `feat/msp-transport`, starting from the reviewed head `6afec8ed16687aed5d3b262b783ef876e892818f`. The fixes are committed individually. The user has authorized pushing the branch and posting the comment responses below; the PR records their publication and current CI status.

## Comment responses ready for review

| Reviewer comment | Implemented response | Passing regression / evidence | Commit |
| --- | --- | --- | --- |
| [Environment-copy scan](https://github.com/R7L208/omnigent-muse/pull/1#issuecomment-5894529243) | Changed the test environment copy to `os.environ.copy()`. | The exfiltration scanner passes on the full PR diff. | `ecb8a6e` |
| [Pending request leak](https://github.com/R7L208/omnigent-muse/pull/1#discussion_r4136516534) | `request()` releases its pending entry in `finally`; initialization uses the same path. `_finish()` now clears the map before failing remaining futures. Late responses are ignored. | `test_request_timeout_clears_pending`, `test_request_cancellation_clears_pending`, `test_serialization_failure_clears_pending`, `test_late_response_after_timeout_is_ignored`, `test_eof_clears_pending`. | `7314e3f` |
| [Writer cancellation](https://github.com/R7L208/omnigent-muse/pull/1#issuecomment-5898461255) | Writer teardown delivers `MspConnectionClosed` to request callers. Queue accounting is balanced and public closure marks the transport closed before waiting for the child. Caller cancellation still propagates normally. | `test_pending_request_reports_closed_when_writer_cancelled` covers direct writer cancellation and public closure, including another queued request and `flush()`. | `4913632` |
| [Host request handler ownership](https://github.com/R7L208/omnigent-muse/pull/1#discussion_r4136567914) | Track handlers, remove completed tasks, cancel them on closure, and join them during teardown. Prevent new handlers after closure. Exclude the handler calling `close()` from cancellation and joining; avoid cancelling an already cancelling handler again. | `test_close_joins_server_request_handlers`, `test_server_request_handler_response_and_self_eviction`, `test_server_request_handler_can_close_client`, `test_close_awaits_async_handler_cleanup`; existing default-response coverage retained. | `8bbbc04`, `7b4c9d5` |
| [Stream helper task cleanup](https://github.com/R7L208/omnigent-muse/pull/1#discussion_r4136592452) | A `finally` boundary cancels and joins both race helpers before translating or yielding an event. | `test_follow_joins_helpers_on_every_exit` covers consumer cancellation, iterator closure after yielding, normal completion, EOF, and simultaneous readiness. | `67689b6` |
| [Retry flag precedence](https://github.com/R7L208/omnigent-muse/pull/1#discussion_r4136583404) | Explicit `retryable=False` returns immediately. Retryable error kinds are used only when a boolean flag is absent. Retries retain the command ID and attempt limit. | `test_command_retry_flag_is_authoritative`, `test_command_retry_exhaustion_preserves_command_id`; existing real-pipe backpressure test retained. | `51f84cb` |
| [Turn event scoping](https://github.com/R7L208/omnigent-muse/pull/1#discussion_r4136537748) | Require matching session and turn IDs for every translated event. Correlate deltas without wire `turnId` through `item/started`; evict completed items and drop unknown-item deltas. Copy internally annotated payloads so other raw subscribers see unchanged frames. Document attaching before submission. | `test_translation_checks_event_scope`, `test_follow_correlates_items_and_rejects_other_turns`, `test_follow_drops_unassociated_item_deltas`, plus the protocol-shaped happy-path fake host. | `71d23c6` |

For the scoping thread: the [pinned official schema](https://github.com/meta-models/muse-code-sdk/blob/bb44be3d36de46d2411bd9eaa4aee99006092546/schema/msp/stable/msp.schema.json) and [official text transcript](https://github.com/meta-models/muse-code-sdk/blob/bb44be3d36de46d2411bd9eaa4aee99006092546/schema/msp/transcripts/text-run-single-turn/transcript.ndjson) put the owning turn on the started item, while deltas identify that item. Requiring a new top-level wire field would discard legitimate deltas; correlation supplies the internal turn ID before applying the common scope gate.

## Verification

Validated on September 30, 2026, against production/test head `7b4c9d5`:

| Check | Result |
| --- | --- |
| `PYTHONASYNCIODEBUG=1 uv run --no-sync pytest -q` (Python 3.13.9) | 85 passed; no unhandled-task or pending-task warnings. |
| `PYTHONASYNCIODEBUG=1 .scratch/venv-py312/bin/python -m pytest -q` (Python 3.12.12) | 85 passed; no unhandled-task or pending-task warnings. |
| Ruff check / format check | All checks passed; 14 files already formatted. |
| Pyrefly | Zero errors. |
| Exfiltration / secret scanners | Both passed against the full diff from PR base `58dcae5d3c55a8e43ae0aa1a36de6ad0ea02a710` to local HEAD. Exfiltration scan emits only the existing informational note for `pyproject.toml`. |
| Workflow action pinning / shell syntax | Passed. |
| Wheel / source distribution build | Both built successfully. |
| Twine distribution checks | Both passed. |
| Regular wheel installation on Python 3.12 and 3.13 | `muse` registers with zero plugin load errors; transport imports from the installed wheel. All eight registration tests pass on each version. Development editable installations restored afterward. |
| Live Muse 1.4.1 with existing account login | Passed against `meta` / `muse-spark-1.3-contributor`: expected text marker streamed, one text delta and one token-usage event received, `usage/read` answered, matching turn completed without error, and child reaped. |
| Independent code review | Two additional handler shutdown regressions found, reproduced, fixed, and tested. Re-review reports no remaining important findings; 21 focused tests pass with asyncio debug enabled. |
| `git diff --check` | Passed. |

The reviewed head's baseline had 34 passing tests, eight Ruff findings, and 77 Pyrefly errors. Commit `75c5385` declares factory-created instance attributes at class scope for Pyrefly and makes the focused formatting/logging corrections needed by the existing CI checks. It does not change protocol behavior.

## Live host validation

An isolated probe used Muse Code **1.4.1 (1.4.1-R4503.1)** with write and shell tools disabled, private temporary XDG directories, and an `echo` session with `allowAll` approval mode. It verified initialization, session creation, turn acceptance, delivery of the matching terminal event, and child reaping during `close()`.

Served schema fingerprint: `sha256:e0e163db6ccf00dbe68402ce55d6319b3edc33c421f31e9583b587b2de8a118f`. This was read from the live initialize response by the probe; the existing client's `schemaInfo` field lookup was not changed.

The initial probe returned `turn/completed` with `error.kind=authRequired` and `retryable=False`, despite accepting the session as provider `echo`. Its isolated configuration hid the user's existing account login. That attempt emitted no agent text deltas or token-usage notification.

A subsequent read-only `account/read` check using the normal login configuration reported `state=accountLogin`. The authenticated live probe retained that configuration while keeping its workspace, session data, and run state in private temporary directories. Write and shell tools remained disabled. It started a `meta` session using `muse-spark-1.3-contributor`; after an initial 60-second timeout, a second attempt with a 120-second limit completed in 10.6 seconds.

The successful attempt received item lifecycle notifications, **one text delta** containing the requested `MUSE_VERIFICATION_OK` marker, **one token-usage event**, and the matching `turn/completed` event without error. `usage/read` also responded successfully. Closure reaped the child and left no pending requests or server handlers. The probe ended with `PASS: live MSP text, usage, completion, and teardown verified`.

No new login was necessary, and no user credentials or settings were changed. Successful live text, usage, completion, and shutdown are now verified on Muse 1.4.1 with the existing account login. The echo-only MSP route on that version remains unverified; the standalone `muse exec --provider echo` command does complete successfully.

## Follow-up PR integration

When these changes are integrated into [PR #3](https://github.com/R7L208/omnigent-muse/pull/3), remove its non-strict xfail markers on `test_request_timeout_clears_pending` and `test_pending_request_reports_closed_when_writer_cancelled`. These are now ordinary passing regressions in this branch. Keep the follow-up executor work separate from this transport review.

All local validation items from the original plan are complete. Before publication, the branch also incorporated upstream's contribution-template commit `09ddc996d56a20b5258874d3c2a69d8fdc97ffbd`. It changes no runtime code. Fresh verification again passed all 85 tests on Python 3.12 and 3.13, Ruff, formatting, and Pyrefly. GitHub CI is checked on the pushed head; review approval remains with the reviewer.
