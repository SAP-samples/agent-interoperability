"""Authenticated A2A calls to SAP Agent Gateway.

The outbound leg of the chain: once the CIS OBO token is in hand, the runtime
speaks the A2A protocol (JSON-RPC `message/send`) to the SAP agent's endpoint,
passing the CIS token as the Bearer. Responses are A2A task objects, parsed by the
caller (runtime/agent.py).
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from runtime.config import SapAgent


class SapGatewayError(RuntimeError):
    pass


def _call(url: str, token: str, *, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, method="POST" if body is not None else "GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        # 180s: SAP agent execution latency is highly variable and slow runs would
        # otherwise trip a shorter client timeout (see ROADMAP Troubleshooting).
        with urlopen(request, timeout=180) as response:  # noqa: S310 - URL is validated config
            raw = response.read(1_000_001)  # read one byte past the cap to detect overflow
    except HTTPError as exc:
        detail = exc.read(4_000).decode("utf-8", errors="replace")
        raise SapGatewayError(f"SAP Agent Gateway returned HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, URLError) as exc:
        raise SapGatewayError("SAP Agent Gateway could not be reached") from exc
    if len(raw) > 1_000_000:
        raise SapGatewayError("SAP Agent Gateway response exceeded 1 MB")
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def send_message(
    agent: SapAgent,
    message: str,
    token: str,
    *,
    context_id: str | None = None,
    task_id: str | None = None,
) -> Any:
    msg: dict[str, Any] = {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "text", "text": message}],
    }
    # contextId/taskId resume an existing A2A task (multi-turn input-required flow);
    # omitting both starts a fresh task.
    if context_id:
        msg["contextId"] = context_id
    if task_id:
        msg["taskId"] = task_id
    payload = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "id": str(uuid.uuid4()),
        "params": {"message": msg},
    }
    return _call(agent.gateway_url, token, body=payload)


def fetch_agent_card(agent: SapAgent, token: str) -> Any:
    return _call(agent.card_url, token)

