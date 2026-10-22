"""MCP entrypoint for AgentCore Runtime (protocol = MCP), fronted by Amazon Quick.

LLM-backed variant: instead of forwarding the request to one hardcoded SAP agent,
this delegates to the shared AgentService brain (runtime/agent.py). AgentService
runs a Bedrock tool loop that decides which SAP agent(s) to call, mints the IAS
on-behalf-of token per agent, and calls SAP Agent Gateway over A2A. This module is
the MCP front door to that brain.

Per-request identity arrives as inbound HTTP headers, captured by an ASGI
middleware before the tool runs:
  * Authorization: Bearer <cognito access token> -> the end user
  * WorkloadAccessToken (platform-injected)       -> the agent workload, for the OBO
  * Mcp-Session-Id (platform-managed)             -> conversation + microVM stickiness

A module-level dict (not contextvars) holds per-request state: each AgentCore
session runs in its own microVM and MCP sessions are one-request-at-a-time, so a
request cannot be clobbered by a concurrent one.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from runtime.agent import SERVICE
from runtime.dynamic_config import get_config_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("agentcore-ias-mcp-llm")

# Per-request state (see module docstring for why a dict is safe here).
_REQ: dict[str, str] = {"bearer": "", "wat": "", "session_id": ""}

# AgentCore Runtime with protocol=MCP expects an MCP server on 0.0.0.0:8000/mcp,
# which is FastMCP's streamable-http default.
#
# host MUST be passed to the constructor (not set afterward): FastMCP auto-enables
# DNS-rebinding protection with a localhost-only Host allowlist when it is
# constructed with the default host (127.0.0.1), which then rejects AgentCore's
# forwarded Host header with HTTP 421. Passing host="0.0.0.0" avoids that, and we
# also disable the protection explicitly since AgentCore fronts the ingress.
mcp = FastMCP(
    "aws-agentcore-ias-llm",
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _user_identity(token: str) -> tuple[str, str | None]:
    """Best-effort identity from the already-validated bearer (authorizer ran).

    Returns (cache_key, email_or_none). The bearer is a Cognito access token that
    carries `email` (added by a pre-token-generation Lambda; falls back to `sub` if
    absent). The authoritative user identity for the IAS OBO is carried by the
    AgentCore workload token, so this decode is only for the token cache key +
    logging.
    """
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        claims = json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return ("unknown", None)
    email = claims.get("email")
    email = email if isinstance(email, str) and email else None
    return (email or claims.get("sub") or "unknown", email)


@mcp.tool()
def sap_assistant(message: str, resume_token: str = "") -> str:
    """Handle an SAP business request end to end.

    Delegate the user's full goal (purchase orders, and any other configured SAP
    domains). The assistant decides which SAP agent(s) to call, may chain several
    calls, and returns a single natural-language answer.

    Multi-turn tasks: if a previous call's result included a `resume_token`, the
    SAP agent asked a follow-up question. Collect the user's answer and call this
    tool again with `message` set to that answer and `resume_token` set to the
    exact value you received, so the same SAP task is continued rather than
    restarted.

    Args:
        message: The user's natural-language request / goal.
        resume_token: Opaque continuation handle from a prior result. Leave empty
            for a new request; set it only to continue a prior input-required task.
    """
    bearer = _REQ.get("bearer") or ""
    wat = _REQ.get("wat") or ""
    if not bearer:
        return json.dumps({"error": "missing_authorization",
                           "detail": "No Authorization bearer token on the request"})
    if not wat:
        return json.dumps({"error": "missing_workload_token",
                           "detail": "No workload access token on the request"})

    user_key, email = _user_identity(bearer)
    session_id = (_REQ.get("session_id") or str(uuid.uuid4()))[:128]
    config = get_config_store().get()

    log.info("sap_assistant user=%s email=%s session=%s resume=%s model=%s agents=%d",
             user_key, bool(email), session_id, bool(resume_token),
             config.model_id, len(config.agents))
    try:
        result = SERVICE.chat(
            message=message[:20_000],
            session_id=session_id,
            email=email or user_key,
            workload_access_token=wat,
            config=config,
            resume_token=resume_token or None,
        )
    except Exception as exc:  # AgentService catches tool errors internally; this is a last resort
        log.exception("sap_assistant failed")
        return json.dumps({"error": "assistant_failed", "detail": type(exc).__name__})

    if isinstance(result, dict):
        # outcome only (status + which tools the LLM called) — no prompt/response content
        log.info("sap_assistant done model=%s status=%s tools=%s resume=%s",
                 config.model_id, result.get("status"), result.get("tools"),
                 bool(result.get("resume_token")))
    # When the SAP agent needs more input, return structured JSON carrying the
    # resume_token so the caller (Quick) can echo it back on the next turn. The
    # message text alone would force the model to copy an opaque string verbatim.
    if isinstance(result, dict) and result.get("resume_token"):
        return json.dumps({
            "message": result.get("message") or "",
            "resume_token": result["resume_token"],
            "instructions": (
                "The SAP agent needs more information. Ask the user the question in "
                "`message`, then call sap_assistant again with resume_token set to this "
                "exact value and message set to the user's answer."
            ),
        })
    message_out = result.get("message") if isinstance(result, dict) else None
    return message_out if isinstance(message_out, str) and message_out else json.dumps(result)


class _HeaderCapture:
    """Stash Authorization + workload-token + session headers before FastMCP dispatches."""

    _WAT_HEADERS = (b"workloadaccesstoken", b"x-amz-bedrock-agentcore-identity-wat")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            _REQ["bearer"] = ""
            _REQ["wat"] = ""
            _REQ["session_id"] = ""
            for name, value in scope.get("headers", []):
                lname = name.lower()
                if lname == b"authorization":
                    v = value.decode("latin-1", errors="ignore")
                    if v.lower().startswith("bearer "):
                        _REQ["bearer"] = v[7:].strip()
                elif lname in self._WAT_HEADERS and not _REQ["wat"]:
                    _REQ["wat"] = value.decode("latin-1", errors="ignore").strip()
                elif lname == b"mcp-session-id" and not _REQ["session_id"]:
                    _REQ["session_id"] = value.decode("latin-1", errors="ignore").strip()
        return await self.app(scope, receive, send)

if __name__ == "__main__":
    import uvicorn

    app = _HeaderCapture(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
