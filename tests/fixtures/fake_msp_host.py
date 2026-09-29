"""Scripted fake MSP host for :mod:`tests.inner.test_msp_client`.

Speaks NDJSON JSON-RPC 2.0 on stdio. Every inbound frame is appended to
``$FAKE_MSP_LOG`` (one JSON object per line: ``{"method": ..., "params":
...}``) so tests can assert what the client sent. Behavior knobs:

- ``FAKE_MSP_SCENARIO``: ``happy`` (default), ``reject_init`` (error the
  handshake), ``die_after_init`` (exit once initialized),
  ``die_on_turn`` (exit when a turn starts), ``server_request`` (send one
  host-initiated request after ``initialized`` and log the answer),
  ``banner`` (print a non-JSON line before the handshake).
- ``FAKE_MSP_BAD_RESPONSE=1``: every response carries both ``result`` and
  ``error`` (malformed).
- ``FAKE_MSP_BACKPRESSURE_ONCE=1``: the first ``turn/start`` fails with a
  ``backpressured`` error, later ones succeed.
- ``FAKE_MSP_DENY``: comma-separated methods answered with an
  ``invalidParams`` error.
- ``FAKE_MSP_HANG``: comma-separated methods never answered (timeout tests).

Logged frames include ``id``/``result``/``error`` when present, so tests can
assert on the client's answers to host-initiated requests.
"""

from __future__ import annotations

import json
import os
import sys


def _send(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def main() -> int:
    scenario = os.environ.get("FAKE_MSP_SCENARIO", "happy")
    bad_response = os.environ.get("FAKE_MSP_BAD_RESPONSE") == "1"
    backpressure_once = os.environ.get("FAKE_MSP_BACKPRESSURE_ONCE") == "1"
    log_path = os.environ.get("FAKE_MSP_LOG")
    log = open(log_path, "a") if log_path else None  # noqa: SIM115
    backpressure_spent = False
    deny = {
        m.strip() for m in os.environ.get("FAKE_MSP_DENY", "").split(",") if m.strip()
    }
    hang = {
        m.strip() for m in os.environ.get("FAKE_MSP_HANG", "").split(",") if m.strip()
    }

    def _log(frame: dict) -> None:
        if log is not None:
            entry = {"method": frame.get("method"), "params": frame.get("params")}
            for key in ("id", "result", "error"):
                if key in frame:
                    entry[key] = frame[key]
            log.write(json.dumps(entry) + "\n")
            log.flush()

    def _respond(
        frame_id: object, result: dict | None = None, error: dict | None = None
    ) -> None:
        response: dict = {"jsonrpc": "2.0", "id": frame_id}
        # Poison everything past the handshake so tests can connect first.
        if bad_response and initialized:
            response["result"] = result or {}
            response["error"] = error or {
                "code": -1,
                "message": "bad",
                "data": {"kind": "bad"},
            }
        elif error is not None:
            response["error"] = error
        else:
            response["result"] = result or {}
        _send(response)

    def _error(code: int, message: str, kind: str, retryable: bool = False) -> dict:
        return {
            "code": code,
            "message": message,
            "data": {"kind": kind, "retryable": retryable},
        }

    if scenario == "banner":
        print("fake-msp: starting up (not JSON)", flush=True)

    initialized = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(frame, dict):
            continue
        method = frame.get("method")
        frame_id = frame.get("id")
        params = frame.get("params") or {}
        _log(frame)

        if method == "initialized":
            initialized = True
            if scenario == "die_after_init":
                return 0
            if scenario == "server_request":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "host/ping",
                        "params": {"sessionId": "sess-1"},
                    }
                )
            continue
        if not isinstance(method, str) or frame_id is None:
            continue

        if method == "initialize":
            if scenario == "reject_init":
                _respond(frame_id, error=_error(-32000, "nope", "rejected"))
                continue
            _respond(
                frame_id,
                result={
                    "schemaInfo": {"fingerprint": "sha256:fake"},
                    "serverInfo": {"version": "0.0.0-fake"},
                },
            )
            continue
        if not initialized:
            _respond(
                frame_id, error=_error(-32000, "not initialized", "not_initialized")
            )
            continue
        if method in hang:
            continue
        if method in deny:
            _respond(
                frame_id, error=_error(-32602, f"denied: {method}", "invalidParams")
            )
            continue

        if method in ("session/start", "session/resume", "session/fork"):
            session_id = params.get("sessionId", "sess-1")
            _respond(
                frame_id,
                result={
                    "session": {
                        "sessionId": session_id,
                        "providerId": params.get("providerId", "echo"),
                        "status": "idle",
                        "turnCount": 0,
                    }
                },
            )
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/started",
                    "params": {"sessionId": session_id},
                }
            )
            continue
        if method == "turn/start":
            nonlocal_backpressure = backpressure_once and not backpressure_spent
            if nonlocal_backpressure:
                backpressure_spent = True
                _respond(frame_id, error=_error(-32001, "busy", "backpressured", True))
                continue
            if scenario == "die_on_turn":
                return 1
            command_id = params.get("commandId", "turn-1")
            session_id = params.get("sessionId", "sess-1")
            _respond(frame_id, result={"status": "accepted", "commandId": command_id})
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "item/delta",
                    "params": {
                        "sessionId": session_id,
                        "itemId": "item-1",
                        "delta": "Hello, ",
                    },
                }
            )
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "item/delta",
                    "params": {
                        "sessionId": session_id,
                        "itemId": "item-1",
                        "delta": "world",
                    },
                }
            )
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/tokenUsage",
                    "params": {
                        "sessionId": session_id,
                        "promptTokens": 10,
                        "outputTokens": 5,
                        "totalTokens": 15,
                        "cumulative": {
                            "promptTokens": 10,
                            "outputTokens": 5,
                            "totalTokens": 15,
                        },
                        "durationMs": 7,
                        "modelId": "fake-model",
                    },
                }
            )
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {
                        "sessionId": session_id,
                        "turnId": command_id,
                        "durationMs": 9,
                        "usage": {
                            "promptTokens": 10,
                            "outputTokens": 5,
                            "totalTokens": 15,
                        },
                    },
                }
            )
            continue
        if method == "approval/listPending":
            _respond(frame_id, result={"approvals": [], "userInputs": []})
            continue
        if method == "usage/read":
            _respond(
                frame_id,
                result={
                    "usage": {
                        "weekly": {"usedPercent": 0},
                        "window": {"usedPercent": 0},
                    }
                },
            )
            continue
        # Generic ack for interrupt/steer/cancel/decide/compact/setModel/....
        _respond(frame_id, result={"status": "accepted"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
