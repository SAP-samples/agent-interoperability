"""Opaque resume handle for A2A input-required continuation across the MCP leg.

Quick does not keep the MCP session id stable across turns, so the paused SAP
task's (context_id, task_id) cannot be looked up server-side by session. Instead
we hand the model an explicit state handle: on `input-required` the tool result
carries a `resume_token`; the model passes it back on the next call and we decode
it to resume the exact A2A task. The token is self-contained (no backing store)
and unsigned — context_id/task_id are opaque SAP-issued ids, not secrets. `decode`
fails closed so a corrupted or unrelated token simply starts a fresh task.
"""

from __future__ import annotations

import base64
import json
from typing import Any

_FIELDS = ("agent_id", "context_id", "task_id")


def encode(agent_id: str, context_id: str, task_id: str) -> str:
    payload = json.dumps(
        {"agent_id": agent_id, "context_id": context_id, "task_id": task_id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode(token: str) -> dict[str, Any] | None:
    if not isinstance(token, str) or not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or set(data) != set(_FIELDS):
        return None
    if not all(isinstance(data[f], str) and data[f] for f in _FIELDS):
        return None
    return data
