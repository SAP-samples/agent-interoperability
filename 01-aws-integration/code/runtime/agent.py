"""Bedrock router with one dynamic pair of A2A tools per configured SAP agent.

The "brain" behind the MCP front door. For each SAP agent in the live config it
exposes two Bedrock tools — `ask_<id>` (send a task) and `capabilities_<id>` (fetch
the agent card) — then runs a Converse tool loop: the model picks a tool, this
module performs the CIS OBO exchange (runtime/cis.py) and the A2A call
(runtime/sap_gateway.py), feeds the result back, and repeats until the model
produces a final answer. Each ask/capabilities call mints its own user-scoped CIS
token, so identity is enforced per SAP call, not once at the edge.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
from typing import Any

import boto3

from runtime import resume_token
from runtime.config import RuntimeConfig
from runtime.cis import CisTokenProvider
from runtime.rate_limit import RateLimitExceeded, SlidingWindowRateLimiter
from runtime.sap_gateway import SapGatewayError, fetch_agent_card, send_message

logger = logging.getLogger(__name__)
_MAX_TURNS = 24  # conversation-history cap kept per session (trims oldest turns)
_MAX_TOOL_ROUNDS = 6  # max model<->tool round-trips per request, guards against a runaway loop


def _extract_a2a_task(response: Any) -> dict[str, Any]:
    """Parse an A2A message/send response into relevant task fields.

    Returns a dict with keys: state, task_id, context_id, text.
    Best-effort — missing/unparseable fields are simply omitted.
    """
    parsed: dict[str, Any] = {}
    try:
        data = response if isinstance(response, dict) else {}
        result = data.get("result", data) if data else {}
        task = result
        if isinstance(result, dict):
            for key in ("task", "result"):
                if isinstance(result.get(key), dict):
                    task = result[key]
                    break
        if isinstance(task, dict):
            status = task.get("status") if isinstance(task.get("status"), dict) else {}
            state = status.get("state") if isinstance(status, dict) else task.get("state")
            if state is not None and not isinstance(state, str):
                state = json.dumps(state)
            parsed["state"] = state
            parsed["task_id"] = task.get("id") or task.get("taskId")
            parsed["context_id"] = task.get("contextId")
            msg = status.get("message") if isinstance(status, dict) else None
            if isinstance(msg, dict):
                texts = [
                    p.get("text")
                    for p in msg.get("parts", [])
                    if isinstance(p, dict) and p.get("kind") == "text" and p.get("text")
                ]
                if texts:
                    parsed["text"] = " ".join(texts)
        parsed = {k: v for k, v in parsed.items() if v is not None}
    except Exception:
        pass
    return parsed


class ConversationStore:
    def __init__(self, max_sessions: int = 256) -> None:
        self._max = max_sessions
        self._items: collections.OrderedDict[str, list[dict[str, Any]]] = collections.OrderedDict()
        self._lock = threading.Lock()

    def get(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            value = list(self._items.get(session_id, []))
            if session_id in self._items:
                self._items.move_to_end(session_id)
            return value

    def put(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        with self._lock:
            self._items[session_id] = messages[-_MAX_TURNS:]
            self._items.move_to_end(session_id)
            while len(self._items) > self._max:
                self._items.popitem(last=False)


class AgentService:
    def __init__(self) -> None:
        self._bedrock: Any | None = None
        self._tokens = CisTokenProvider()
        self._sessions = ConversationStore()
        self._limiter = SlidingWindowRateLimiter()

    def _client(self) -> Any:
        if self._bedrock is None:
            self._bedrock = boto3.client(
                "bedrock-runtime", region_name=os.environ.get("AWS_REGION", "eu-central-1")
            )
        return self._bedrock

    @staticmethod
    def _tools(config: RuntimeConfig) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for agent in config.agents:
            tools.append(
                {
                    "toolSpec": {
                        "name": f"ask_{agent.id}",
                        "description": f"Send a task to {agent.name}. Use for: {agent.description}",
                        "inputSchema": {
                            "json": {
                                "type": "object",
                                "properties": {
                                    "message": {"type": "string"},
                                    "resume_token": {
                                        "type": "string",
                                        "description": (
                                            "Opaque continuation handle. Only set this when a "
                                            "previous result from this tool returned a "
                                            "`_resume_token`; pass that exact value to continue "
                                            "the same task with the user's answer."
                                        ),
                                    },
                                },
                                "required": ["message"],
                            }
                        },
                    }
                }
            )
            tools.append(
                {
                    "toolSpec": {
                        "name": f"capabilities_{agent.id}",
                        "description": (
                            f"Fetch the agent card for {agent.name} to answer questions about "
                            f"its capabilities, supported operations, or what it can do. "
                            f"Use this when the user asks what {agent.name} can do."
                        ),
                        "inputSchema": {
                            "json": {
                                "type": "object",
                                "properties": {},
                                "required": [],
                            }
                        },
                    }
                }
            )
        return tools

    @staticmethod
    def _system(config: RuntimeConfig) -> str:
        routes = "\n".join(
            f"- {agent.name}: {agent.description}; tool ask_{agent.id}; capabilities tool capabilities_{agent.id}"
            for agent in config.agents
        )
        return (
            "You are an AWS-hosted assistant that can delegate to SAP business agents. "
            "Use a matching ask tool for requests in a configured agent's domain. "
            "Use the capabilities tool when the user asks what an agent can do, what it supports, or its skills. "
            "Answer general questions directly. "
            "Never invent SAP results and never mention credentials or internal tokens. "
            "If a tool result contains a `_resume_token`, the SAP agent needs more information "
            "to finish the task: ask the user the question from that result, and on your next "
            "call to that same ask tool pass `resume_token` set to that exact value along with "
            "the user's answer as `message`.\n"
            f"Configured SAP agents:\n{routes or '- None; answer directly.'}"
        )

    def _tool_result(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        email: str,
        workload_access_token: str,
        config: RuntimeConfig,
    ) -> Any:
        if name.startswith("capabilities_"):
            agent_id = name.removeprefix("capabilities_")
            agent = config.agent(agent_id)
            token = self._tokens.get_token(
                user_email=email,
                workload_access_token=workload_access_token,
                resource_urn=config.resource_for(agent),
                config=config,
                region=os.environ.get("AWS_REGION", "eu-central-1"),
            )
            return fetch_agent_card(agent, token)
        if name.startswith("ask_"):
            agent_id = name.removeprefix("ask_")
            agent = config.agent(agent_id)
            self._limiter.check(email, config.rate_limit)
            token = self._tokens.get_token(
                user_email=email,
                workload_access_token=workload_access_token,
                resource_urn=config.resource_for(agent),
                config=config,
                region=os.environ.get("AWS_REGION", "eu-central-1"),
            )
            message = arguments.get("message")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("SAP agent message is missing")
            # Explicit state handle: the model echoes back a resume_token from a
            # prior input-required result. A valid token for this agent seeds the
            # A2A continuation; anything else starts a fresh task (fail closed).
            handle = resume_token.decode(str(arguments.get("resume_token") or ""))
            context_id = None
            task_id = None
            if handle and handle["agent_id"] == agent_id:
                context_id = handle["context_id"]
                task_id = handle["task_id"]
                logger.info(
                    "resuming A2A task agent=%s context_id=%s task_id=%s",
                    agent_id, context_id, task_id,
                )
            response = send_message(
                agent,
                message[:20_000],
                token,
                context_id=context_id,
                task_id=task_id,
            )
            task = _extract_a2a_task(response)
            if task.get("state") == "input-required" and task.get("context_id") and task.get("task_id"):
                new_token = resume_token.encode(agent_id, task["context_id"], task["task_id"])
                logger.info(
                    "A2A input-required agent=%s context_id=%s task_id=%s",
                    agent_id, task["context_id"], task["task_id"],
                )
                return {
                    "a2a": response,
                    "_resume_token": new_token,
                    "_resume_hint": (
                        f"The SAP agent needs more input. Ask the user, then call ask_{agent_id} "
                        "again with resume_token set to this value and message set to the answer."
                    ),
                }
            return response
        raise ValueError("model selected an unknown tool")

    def chat(
        self,
        *,
        message: str,
        session_id: str,
        email: str,
        workload_access_token: str,
        config: RuntimeConfig,
        resume_token: str | None = None,
    ) -> dict[str, Any]:
        messages = self._sessions.get(session_id)
        user_text = message[:20_000]
        if resume_token:
            # Quick does not keep MCP sessions sticky, so a continued turn arrives
            # fresh. Carry the handle explicitly so the inner model forwards it to
            # the ask_ tool instead of starting a new SAP task.
            user_text = (
                f"{user_text}\n\n[continuation] This answers a previous input-required "
                f"request. When you call the matching ask tool, set its resume_token to: "
                f"{resume_token}"
            )
        messages.append({"role": "user", "content": [{"text": user_text}]})
        tools = self._tools(config)
        used: list[str] = []
        pending_resume: str | None = None
        for _ in range(_MAX_TOOL_ROUNDS):
            kwargs: dict[str, Any] = {
                "modelId": config.model_id,
                "system": [{"text": self._system(config)}],
                "messages": messages,
                "inferenceConfig": {"maxTokens": 1500, "temperature": 0.2},
            }
            if tools:
                kwargs["toolConfig"] = {"tools": tools}
            response = self._client().converse(**kwargs)
            assistant = response["output"]["message"]
            messages.append(assistant)
            tool_uses = [part["toolUse"] for part in assistant["content"] if "toolUse" in part]
            if not tool_uses:
                # No tool call this round -> the model has produced its final answer.
                text = "".join(part.get("text", "") for part in assistant["content"]).strip()
                self._sessions.put(session_id, messages)
                out: dict[str, Any] = {"status": "ok", "message": text, "tools": used}
                if pending_resume:
                    out["resume_token"] = pending_resume
                return out
            results = []
            for tool in tool_uses:
                name = tool["name"]
                used.append(name)
                status = "success"
                try:
                    result = self._tool_result(
                        name=name,
                        arguments=tool.get("input") or {},
                        email=email,
                        workload_access_token=workload_access_token,
                        config=config,
                    )
                except (
                    SapGatewayError,
                    RateLimitExceeded,
                    RuntimeError,
                    ValueError,
                    KeyError,
                ) as exc:
                    logger.warning("tool %s failed: %s: %s", name, type(exc).__name__, exc)
                    result = {"error": str(exc)}
                    status = "error"
                if isinstance(result, dict) and result.get("_resume_token"):
                    pending_resume = result["_resume_token"]
                results.append(
                    {
                        "toolResult": {
                            "toolUseId": tool["toolUseId"],
                            "content": [{"json": _json_value(result)}],
                            "status": status,
                        }
                    }
                )
            messages.append({"role": "user", "content": results})
        self._sessions.put(session_id, messages)
        return {"status": "error", "message": "The tool-call limit was reached", "tools": used}


def _json_value(value: Any) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


SERVICE = AgentService()
