# MSP transport review implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Address all seven reviewer comments on PR #1 with focused transport fixes and regression coverage.

**Implementation status:** All seven fixes are implemented and committed locally, and all local validation items are complete. Checked steps indicate their intended outcomes are covered; some regression names and instrumentation changed during implementation. See [review results](../reviews/2026-09-30-msp-transport-review-results.md) for final commits, passing evidence, implementation adjustments, and draft comment responses. Successful live text/usage validation passed with the existing account login on Muse 1.4.1; the isolated echo route returned `authRequired`.

**Architecture:** Keep the existing subprocess, ordered writer, request correlation, and notification subscription design. Give every future and background task an explicit cleanup path, honor the host's retry flag, and correlate item deltas to their owning turn before translation.

**Tech Stack:** Python 3.12+, asyncio, pytest-asyncio, Ruff, Pyrefly, uv, and the hermetic fake MSP host.

---

## Review inventory and verified context

Reviewed [PR #1](https://github.com/R7L208/omnigent-muse/pull/1), including its review summary, all five inline comments, and both top-level comments, on September 30, 2026. The reviewer is `R7L208`; the review requests changes.

| Comment | Assessment | Task |
| --- | --- | --- |
| [Environment-copy security scan](https://github.com/R7L208/omnigent-muse/pull/1#issuecomment-5894529243) | Equivalent spelling change; the scanner flags the current expression. | 1 |
| [Pending request leak](https://github.com/R7L208/omnigent-muse/pull/1#discussion_r4136516534) | Confirmed: timeout/cancellation leave entries; `_finish()` also fails to clear the dictionary in the current code. | 2 |
| [Writer cancellation reaches request callers](https://github.com/R7L208/omnigent-muse/pull/1#issuecomment-5898461255) | Confirmed: the writer sets its own `CancelledError` on the response future. | 3 |
| [Untracked host request handlers](https://github.com/R7L208/omnigent-muse/pull/1#discussion_r4136567914) | Confirmed: handler tasks are created without ownership and omitted from teardown. | 4 |
| [Unawaited stream helper tasks](https://github.com/R7L208/omnigent-muse/pull/1#discussion_r4136592452) | Confirmed: race losers are cancelled without joining; cancellation during `asyncio.wait()` has no cleanup. | 5 |
| [Explicit retry refusal overridden](https://github.com/R7L208/omnigent-muse/pull/1#discussion_r4136583404) | Confirmed: `False or retryable_kind` retries against the explicit host flag. | 6 |
| [Events crossing turn boundaries](https://github.com/R7L208/omnigent-muse/pull/1#discussion_r4136537748) | Valid concern; a simple top-level ID requirement would drop legitimate deltas. Correlate their item IDs first. | 7 |

The local branch is `feat/msp-transport` at `3370869`. The reviewed remote PR head is `6afec8ed16687aed5d3b262b783ef876e892818f`, two commits ahead. Those commits add CI/security/type-checking configuration and unrelated typing corrections; the reviewed `msp_client.py`, fake host, and transport tests have no changes between these heads. Start implementation from the remote PR head to retain that work. The planning task has not changed runtime code or posted anything to GitHub.

The reviewer already added `test_request_timeout_clears_pending` and `test_pending_request_reports_closed_when_writer_cancelled` as non-strict xfails in [PR #3's test file](https://github.com/R7L208/omnigent-muse/blob/1bd6b6985e533ba7489c1e188d4f7f4e4e77fcb1/tests/test_msp_client.py). Reuse their assertions here as ordinary passing requirements; use event synchronization instead of their fixed sleep. After integration into that follow-up branch, its xfail markers should be removed.

Protocol evidence: the [official pinned MSP schema](https://github.com/meta-models/muse-code-sdk/blob/bb44be3d36de46d2411bd9eaa4aee99006092546/schema/msp/stable/msp.schema.json) requires `turnId` for usage, approvals, and terminal events. Deltas identify an item; `item/started` supplies that item's turn. The [official text transcript](https://github.com/meta-models/muse-code-sdk/blob/bb44be3d36de46d2411bd9eaa4aee99006092546/schema/msp/transcripts/text-run-single-turn/transcript.ndjson) demonstrates this ordering. The scoping design below follows those fields rather than inventing a required wire field on deltas.

## File responsibilities

- `src/omnigent/community/harness/muse/inner/msp_client.py`: all production changes; private request ownership, task ownership, retry decision, and turn correlation.
- `tests/test_msp_client.py`: regression tests, reusing `_spawn()` and the existing fixture helpers.
- `tests/fixtures/fake_msp_host.py`: make the existing happy-path stream include item lifecycle and usage turn IDs.
- `README.md`: explain stream correlation and the subscribe-before-submit requirement.
- Existing CI configuration, executor wiring, SDK migration, and unrelated protocol shape changes stay outside these fixes.

No project virtual environment exists locally at planning time. Tests have not been run for this plan. At implementation time, use the remote head's development dependencies and sibling Omnigent checkout. Run `just ensure` with the existing package-index configuration, or `OMNIGENT_SKIP_WEB_UI=true uv sync --group dev` when the index is already configured.

Before implementation, bring a clean or isolated checkout of this branch up to its published head:

```bash
git fetch origin feat/msp-transport
git merge --ff-only origin/feat/msp-transport
git rev-parse HEAD
```

At the reviewed snapshot, expect `6afec8ed16687aed5d3b262b783ef876e892818f`. If new commits appeared, inspect their transport changes before applying this plan. Preserve this plan document when preparing the checkout.

## Task 1: Unblock the environment-copy scan

**Files:** Modify `tests/test_msp_client.py`, `_spawn_env()` (currently line 36).

- [x] Replace only the environment-copy expression:

```python
env = os.environ.copy()
```

Keep the log-path insertion and override application as they are. This is a reversible spelling change and needs no new unit test.

- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py` and `uv run --no-sync ruff check tests/test_msp_client.py`. Expect the existing transport tests to pass and Ruff to exit zero.
- [x] Commit as `test: use environment copy compatible with security scan`.

The remote scanner examines added lines in the PR diff. After the eventual push, require the Security Scan check to pass; local unit tests alone do not establish that result.

## Task 2: Release pending requests on every exit path

**Files:** Modify `msp_client.py`, `_initialize()`, `request()`, `_request_raw()`, and `_finish()` (currently lines 264, 340, 366, and 646); test in `tests/test_msp_client.py`.

- [x] Add these regressions before changing production code:

```python
async def test_request_timeout_clears_pending(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_HANG="usage/read")
    try:
        for _ in range(3):
            with pytest.raises(TimeoutError, match="usage/read"):
                await client.read_usage("s", timeout=0.02)
            assert client._pending == {}
    finally:
        await client.close()


async def test_request_cancellation_clears_pending(tmp_path: Path) -> None:
    client = await _spawn(tmp_path, FAKE_MSP_HANG="usage/read")
    request = asyncio.create_task(client.read_usage("s"))
    try:
        async with asyncio.timeout(2):
            while not _method_frames(tmp_path, "usage/read"):
                await asyncio.sleep(0.01)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert client._pending == {}
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await client.close()


async def test_serialization_failure_clears_pending(tmp_path: Path) -> None:
    client = await _spawn(tmp_path)
    try:
        with pytest.raises(MspProtocolError, match="JSON-encodable"):
            await client.request("usage/read", {"invalid": object()})
        assert client._pending == {}
    finally:
        await client.close()
```

- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'clears_pending'`. Expect the new pending-map assertions to fail on the current implementation.
- [x] Replace `_request_raw()` with this private request-ID handoff:

```python
def _request_raw(
    self, method: str, params: JsonObject
) -> tuple[int, asyncio.Future[JsonObject]]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[JsonObject] = loop.create_future()
    self._next_request_id += 1
    request_id = self._next_request_id
    self._pending[request_id] = future
    frame: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        frame["params"] = params
    self._enqueue_write(frame, future)
    return request_id, future
```

In `request()`, replace the current future assignment and timeout block with:

```python
request_id, future = self._request_raw(method, params or {})
try:
    async with asyncio.timeout(timeout):
        return await future
except TimeoutError as exc:
    raise TimeoutError(
        f"{self._label}: no answer to {method} within {timeout:g}s"
    ) from exc
finally:
    self._pending.pop(request_id, None)
    if not future.done():
        future.cancel()
```

In `_initialize()`, replace its nested timeout and `_request_raw()` await with:

```python
result = await self.request("initialize", params, timeout=timeout)
```

Retain the surrounding handshake exception translation. This routes initialization through the same cleanup without adding a second ownership mechanism. Check all `_request_raw` call sites with `rg -n '_request_raw' src tests`; only `request()` should call it after this change.

In `_finish()`, replace the future iteration with:

```python
pending = tuple(self._pending.values())
self._pending.clear()
for future in pending:
    if not future.done():
        future.set_exception(error)
```

- [x] Add `test_eof_clears_pending` to assert `client._pending == {}` after a turn request fails with connection closure. Add a late-reply check after a timeout by calling `_route_frame()` with that timed-out request's ID and an empty result; assert the client remains open and a later `start_session()` succeeds. This verifies that an unknown late ID cannot recreate or settle an unrelated request.
- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'pending or timeout or handshake or host_death'`. Expect all selected tests to pass, including genuine caller cancellation preserving `CancelledError`.
- [x] Commit as `fix: release MSP pending requests on all exit paths`.

## Task 3: Report writer teardown as connection closure

**Files:** Modify `msp_client.py`, `_writer_loop()` and `close()` (currently lines 446 and 668); test in `tests/test_msp_client.py`.

- [x] Add the reviewer regression using deterministic synchronization:

```python
async def test_pending_request_reports_closed_when_writer_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await _spawn(tmp_path)
    await client.flush()
    entered = asyncio.Event()

    async def blocked_write(encoded: bytes) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_write_once", blocked_write)
    request = asyncio.create_task(client.read_usage("s"))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        client._writer_task.cancel()
        await asyncio.gather(client._writer_task, return_exceptions=True)
        with pytest.raises(MspConnectionClosed):
            await request
        assert client.closed
        assert client._pending == {}
        await client.flush(timeout=0.2)
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await client.close()
```

Also exercise the public teardown path with the same setup and `await client.close()` in place of direct writer cancellation. Keep both cases parameterized or as two explicit tests.

- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'writer_cancelled'`. Expect the direct cancellation case to fail before the fix because the request receives `CancelledError`.
- [x] Replace `_writer_loop()` with the following queue-balanced structure:

```python
async def _writer_loop(self) -> None:
    try:
        while True:
            encoded, future = await self._write_queue.get()
            try:
                await self._write_once(encoded)
            except BaseException as exc:
                error = (
                    MspConnectionClosed(f"{self._label}: connection is closed")
                    if isinstance(exc, asyncio.CancelledError)
                    else exc
                )
                if future is not None and not future.done():
                    future.set_exception(error)
                self._finish(error)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
            finally:
                self._write_queue.task_done()
    except asyncio.CancelledError:
        self._finish(MspConnectionClosed(f"{self._label}: connection is closed"))
```

Retain a concise comment describing why writer cancellation becomes a transport exception for request callers. Do not catch or convert genuine caller cancellation in `request()`.

Immediately after setting `_did_close = True` in `close()`, add:

```python
self._finish(MspConnectionClosed(f"{self._label}: client closed"))
```

This stops new traffic and settles all response futures before process reaping can spend time waiting. Retain the final `_finish()` call as an idempotent safeguard and retain the existing EOF/terminate/kill process shutdown sequence.

- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'writer or cancellation or close or host_death'`. Expect no leaked writer cancellation, no queue-join hang, and the child reaped by `close()`.
- [x] Commit as `fix: surface connection closure during MSP writer teardown`.

## Task 4: Own and join host request handler tasks

**Files:** Modify `msp_client.py`, `_create()`, `_route_frame()`, `_log_task_error()`, `_finish()`, and `close()`; test in `tests/test_msp_client.py`.

- [x] Add a blocked-handler regression that covers both explicit closure and EOF-equivalent `_finish()`:

```python
@pytest.mark.parametrize("finish_first", [False, True])
async def test_close_joins_server_request_handlers(
    tmp_path: Path, finish_first: bool
) -> None:
    client = await _spawn(tmp_path)
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def handler(method: str, params: dict) -> dict:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
        return {}

    client.set_server_request_handler(handler)
    frame = {"jsonrpc": "2.0", "id": 900, "method": "host/ping", "params": {}}
    client._route_frame(frame, json.dumps(frame))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        if finish_first:
            client._finish(MspConnectionClosed("test EOF"))
        await client.close()
        assert exited.is_set()
        assert client._server_request_tasks == set()
    finally:
        await client.close()
```

- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'joins_server_request'`. Expect failure because teardown does not cancel the handler.
- [x] Initialize task ownership in `_create()` before starting pumps:

```python
self._server_request_tasks: set[asyncio.Task[None]] = set()
```

In the existing server-request routing branch, register each newly created task before returning:

```python
self._server_request_tasks.add(task)
task.add_done_callback(self._log_task_error)
```

Add this as the first statement of `_log_task_error()`:

```python
self._server_request_tasks.discard(done)
```

After setting `_closed` in `_finish()`, cancel in-flight handlers:

```python
caller = asyncio.current_task()
for task in tuple(self._server_request_tasks):
    if task is not caller and not task.done() and not task.cancelling():
        task.cancel()
```

In `close()`'s final cleanup, replace the existing three-task cancellation/await loop with:

```python
caller = asyncio.current_task()
tasks = (
    self._reader_task,
    self._stderr_task,
    self._writer_task,
    *(task for task in self._server_request_tasks if task is not caller),
)
for task in tasks:
    if not task.done() and not task.cancelling():
        task.cancel()
await asyncio.gather(*tasks, return_exceptions=True)
self._server_request_tasks.clear()
```

The connection is already marked closed before taking this snapshot, so the reader cannot create new request handlers across an await during cleanup. Make `_route_frame()` return immediately when `self.closed` to enforce that boundary explicitly.

- [x] Add a successful custom-handler case: route a request after installing a handler returning `{"ok": True}`, snapshot and await its tracked task, then assert the set self-evicts. Flush and inspect the fake log for the matching request ID and result. Retain the default method-not-found test and cover handler exceptions returning an error response.
- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'server_request'`. Expect success, failure, default response, and closure ownership cases to pass.
- [x] Commit as `fix: track and join MSP server request handlers`.

## Task 5: Join every stream race task

**Files:** Modify `msp_client.py`, `TurnStream.follow()` (currently line 1008); test in `tests/test_msp_client.py`.

- [x] Add a deterministic cancellation test. Spy on the two helper tasks using their proposed names:

```python
async def test_follow_cancellation_joins_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await _spawn(tmp_path)
    created: list[asyncio.Task] = []
    ready = asyncio.Event()
    create_task = asyncio.create_task

    def record(coro, *, name=None, context=None):
        task = create_task(coro, name=name, context=context)
        if name in {"msp-stream-get", "msp-stream-closed"}:
            created.append(task)
            if len(created) == 2:
                ready.set()
        return task

    monkeypatch.setattr(asyncio, "create_task", record)
    with client.open_stream("s") as stream:
        iterator = stream.follow("t")
        consumer = asyncio.create_task(anext(iterator))
        try:
            async with asyncio.timeout(2):
                await ready.wait()
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
            assert len(created) == 2
            assert all(task.done() for task in created)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await iterator.aclose()
            await client.close()
```

To demonstrate the failure before adding names, instrument the current un-named calls during `anext()` only, or first introduce the two task names as a behavior-neutral preparation. The assertions must fail against the old cleanup behavior, not simply time out because names are absent.

- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'follow_cancellation'`. Expect the unjoined helper assertion to fail on the old cleanup.
- [x] Inside each iteration of `follow()`, replace task creation, waiting, and loser cancellation with:

```python
get = asyncio.create_task(self._queue.get(), name="msp-stream-get")
closed = asyncio.create_task(self._client.wait_closed(), name="msp-stream-closed")
tasks = (get, closed)
try:
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
finally:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
```

Keep the existing simultaneous-ready behavior: consume an available queue item even if closure is also ready; raise `MspConnectionClosed` when closure alone wins. Translate/yield only after the helper cleanup finishes. With that ordering, suspension at `yield` leaves no helper tasks running.

- [x] Test normal completion, host death, simultaneous queued completion and closure, and explicit `iterator.aclose()` after the first yielded event. Use the task spy to assert all created helpers finish. Keep `with client.open_stream(...)` in consumers so subscription cleanup is explicit; a bare loop break does not automatically close an async generator or its subscription.
- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'follow or stream or host_death'`. Expect all stream cases to pass without pending helper tasks.
- [x] Commit as `fix: await MSP stream helper cancellation`.

## Task 6: Respect the explicit retry flag

**Files:** Modify `msp_client.py`, `command()` (currently line 401); test in `tests/test_msp_client.py`.

- [x] Add a focused decision matrix by replacing only the request method in the test:

```python
@pytest.mark.parametrize(
    ("kind", "retryable", "attempts"),
    [
        ("backpressured", False, 1),
        ("overloaded", False, 1),
        ("backpressured", None, 2),
        ("overloaded", None, 2),
        ("temporary", True, 2),
        ("invalidParams", None, 1),
    ],
)
async def test_command_retry_flag_is_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    retryable: bool | None,
    attempts: int,
) -> None:
    client = await _spawn(tmp_path)
    calls: list[dict] = []

    async def request(method, params=None, *, timeout=30.0):
        calls.append(dict(params or {}))
        if len(calls) == 1:
            data = {} if retryable is None else {"retryable": retryable}
            raise MspError(-32001, "busy", kind=kind, data=data)
        return {"status": "accepted"}

    monkeypatch.setattr(client, "request", request)
    try:
        if attempts == 1:
            with pytest.raises(MspError):
                await client.command("turn/start", {"sessionId": "s"})
        else:
            await client.command("turn/start", {"sessionId": "s"})
        assert len(calls) == attempts
        assert len({call["commandId"] for call in calls}) == 1
    finally:
        await client.close()
```

- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'retry_flag'`. Expect explicit-false cases to fail before the fix.
- [x] Replace the current retry expression with:

```python
retryable = (
    exc.retryable if exc.retryable is not None else exc.kind in _COMMAND_RETRYABLE_KINDS
)
```

Document that `True`/`False` is authoritative; kind-based fallback applies only when the flag is absent or not boolean. Keep the same command ID across retries, bounded attempts, and existing backoff. Do not begin retrying timeouts or transport failures.

- [x] Add an always-retryable mocked response to assert exhaustion raises after exactly `max_attempts` calls. Retain the existing fake-host backpressure test to verify the decision through real pipes.
- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'retry or backpressure or non_retryable'`. Expect all selected tests to pass.
- [x] Commit as `fix: honor explicit MSP retryability flags`.

## Task 7: Scope every translated event to its turn

**Files:** Modify `msp_client.py`, `TurnStream.__init__()`, `_tap()`, `close()`, and `_translate_notification()`; modify `tests/fixtures/fake_msp_host.py` turn-start stream; modify translation tests and `README.md`.

- [x] Add parameterized translation tests for `item/delta`, `session/tokenUsage`, `approval/requested`, `turn/completed`, and `turn/retracted`. For each event use a valid payload, then assert a foreign session, foreign turn, and missing turn are dropped; matching session/turn passes. A delta passed directly to the translator must carry the internally correlated turn ID.
- [x] Add this integration regression for protocol-shaped deltas without a wire `turnId`:

```python
async def test_follow_correlates_items_and_rejects_other_turns(tmp_path: Path) -> None:
    client = await _spawn(tmp_path)
    try:
        with client.open_stream("s") as stream:
            for turn, item in [("foreign", "i-other"), ("t", "i-own")]:
                client._fan_out(
                    "item/started",
                    {
                        "sessionId": "s",
                        "item": {"itemId": item, "turnId": turn},
                    },
                )
                client._fan_out(
                    "item/delta",
                    {
                        "sessionId": "s",
                        "itemId": item,
                        "delta": turn,
                    },
                )
                client._fan_out(
                    "session/tokenUsage",
                    {
                        "sessionId": "s",
                        "turnId": turn,
                        "totalTokens": 3,
                    },
                )
                client._fan_out(
                    "approval/requested",
                    {
                        "sessionId": "s",
                        "turnId": turn,
                        "approvalId": turn,
                    },
                )
                client._fan_out(
                    "turn/completed",
                    {
                        "sessionId": "s",
                        "turnId": turn,
                    },
                )
            async with asyncio.timeout(2):
                events = [event async for event in stream.follow("t")]
            assert [e.delta for e in events if isinstance(e, MspTextDelta)] == ["t"]
            assert len([e for e in events if isinstance(e, MspTokenUsage)]) == 1
            assert [
                e.approval_id for e in events if isinstance(e, MspApprovalRequested)
            ] == ["t"]
            assert [e.turn_id for e in events if isinstance(e, MspTurnCompleted)] == [
                "t"
            ]
    finally:
        await client.close()
```

- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py -k 'correlates or scope'`. Expect foreign-turn deltas, usage, and approvals to fail isolation before the fix.
- [x] Initialize correlation state in `TurnStream.__init__()`:

```python
self._item_turns: dict[str, str] = {}
```

Replace `_tap()` with:

```python
def _tap(self, method: str, params: JsonObject) -> None:
    if params.get("sessionId") != self._session_id:
        return
    if method in {"item/started", "item/completed"}:
        item = params.get("item")
        if isinstance(item, dict):
            item_id = item.get("itemId")
            turn_id = item.get("turnId")
            if isinstance(item_id, str):
                if method == "item/completed":
                    self._item_turns.pop(item_id, None)
                elif isinstance(turn_id, str):
                    self._item_turns[item_id] = turn_id
        return
    if method == "item/delta":
        item_id = params.get("itemId")
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str):
            turn_id = (
                self._item_turns.get(item_id) if isinstance(item_id, str) else None
            )
        if not isinstance(turn_id, str):
            return
        params = {**params, "turnId": turn_id}
    self._queue.put_nowait((method, params))
```

At the start of `_translate_notification()`, before any method branch, add:

```python
if params.get("sessionId") != session_id or params.get("turnId") != turn_id:
    return None
```

Remove the now-redundant `turnId` checks in the two terminal branches. Keep raw subscriber payloads untouched by copying only the internally annotated delta. Clear `_item_turns` in `TurnStream.close()`.

- [x] Before the existing happy-path deltas in the fake host, emit:

```python
_send(
    {
        "jsonrpc": "2.0",
        "method": "item/started",
        "params": {
            "sessionId": session_id,
            "item": {
                "itemId": "item-1",
                "turnId": command_id,
                "kind": "agentMessage",
                "status": "inProgress",
                "revision": 1,
                "text": "",
            },
        },
    }
)
```

Add `"turnId": command_id` to its existing usage notification. After its deltas, emit `item/completed` with the same item/turn IDs to test map eviction. Keep the delta payloads without `turnId`, matching the official protocol. Update `test_approval_requested_event_shape()` to include `"turnId": "t"` in both its malformed and valid inputs so it continues testing approval shape rather than failing the scope gate.

- [x] Add checks for unknown-item deltas being dropped, completed items being evicted, foreign-session lifecycle events not populating the map, and other subscribers receiving the original unmodified delta parameters. Also run two open streams for the same session following distinct turns against interleaved notifications.
- [x] Add this description to `follow()` and the README transport notes:

```text
Open the stream before submitting a turn. Text deltas are scoped through
the item-to-turn association supplied by item/started; unknown-item deltas
are dropped. Usage, approvals, and terminal events require a matching
turnId. Attaching after a turn has begun requires replay or snapshot
seeding, which this live-only stream does not provide.
```

This is the main behavioral compatibility change. A host omitting item lifecycle or required usage/approval/terminal IDs will no longer have those events attributed to the current turn by assumption. Before shipping, verify the same lifecycle ordering against the supported CLI version using an echo-provider turn; the schema alone does not establish behavior of every installed host version.

- [x] Run `uv run --no-sync pytest -q tests/test_msp_client.py`. Expect all existing tests plus scoping regressions to pass, including happy-path output `Hello, world`.
- [x] Commit as `fix: correlate MSP stream events with their owning turn`.

## Final verification and review handoff

- [x] Run `uv run --no-sync ruff check .`, `uv run --no-sync ruff format --check .`, and `uv run --no-sync pyrefly check`. Expect zero exit codes; adapt test-only mocks' annotations to the remote head's checker if necessary.
- [x] Run `PYTHONASYNCIODEBUG=1 uv run --no-sync pytest -q`. Expect the complete suite to pass without unhandled task exceptions or pending-task warnings. Preserve the remote head's registration and security-scan coverage.
- [x] Run `uv build --out-dir /tmp/omnigent-muse-review-dist` and `uvx --from twine twine check /tmp/omnigent-muse-review-dist/*`. Expect valid wheel and source distributions.
- [x] Verify the package still registers `muse` with zero plugin load errors in the established regular-install development workflow. Match the remote CI checks on Python 3.12 and 3.13.
- [x] Run one successful live turn against the supported Muse CLI, confirming item lifecycle correlation preserves deltas, usage, and completion. Record the CLI version and served fingerprint in validation notes. Completed using the authenticated Meta provider because the isolated echo route returned `authRequired`.
- [x] Run `git diff --check` and inspect the final diff for unrelated changes.
- [x] Prepare a review response for each of the seven links above, naming the fix and its relevant passing regression. For the scoping thread, explain item correlation and link the schema; for the pending-map thread, mention `_finish()` now clears the map as well.
- [x] Prepare the follow-up PR #3 integration note identifying the two xfail markers to remove. Do not include executor implementation changes in this transport PR.

The user authorized implementation and subsequently authorized pushing the fixes and replying to every reviewer comment. The changes and responses are ready for that publication; requesting another review or merging the PR remains outside this instruction.

## Plan self-review

All seven comments have a task and a validation path. The implementation uses one pending-map cleanup owner, one handler-task set, one stream helper cleanup boundary, and one translation scope gate. Test synchronization uses events or bounded polling instead of fixed timing assumptions. The ID types and method signatures remain consistent with the current transport; only the private `_request_raw()` return signature changes.
