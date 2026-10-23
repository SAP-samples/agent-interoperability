"""Validated live configuration shared by the runtime and web administration UI."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

_AGENT_ID = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


def _https_url(value: str, field_name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field_name} must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{field_name} contains unsupported URL components")
    return value.rstrip("/")


@dataclass(frozen=True)
class SapAgent:
    id: str
    name: str
    description: str
    gateway_url: str
    aud_urn: str | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> SapAgent:
        unknown = set(raw) - {"id", "name", "description", "gateway_url", "aud_urn"}
        if unknown:
            raise ValueError(f"unknown SAP agent fields: {sorted(unknown)}")
        agent_id = str(raw.get("id", "")).strip()
        if not _AGENT_ID.fullmatch(agent_id):
            raise ValueError(f"invalid SAP agent id: {agent_id!r}")
        name = str(raw.get("name", "")).strip()
        description = str(raw.get("description", "")).strip()
        if not name or not description:
            raise ValueError("SAP agent name and description are required")
        gateway = _https_url(str(raw.get("gateway_url", "")), "gateway_url")
        host = urlparse(gateway).hostname or ""
        if not host.endswith(".sapdas.cloud.sap"):
            raise ValueError("SAP agent URLs must use an SAP Agent Gateway host")
        aud = str(raw.get("aud_urn") or "").strip() or None
        return cls(agent_id, name, description, gateway, aud)

    @property
    def card_url(self) -> str:
        # gateway_url ends in /jsonrpc; agent card lives at /.well-known/agent-card.json
        # relative to the same tenant path prefix.
        base = self.gateway_url.rsplit("/jsonrpc", 1)[0]
        return f"{base}/.well-known/agent-card.json"


@dataclass(frozen=True)
class IasSettings:
    resource_urn: str
    credential_provider_name: str

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> IasSettings:
        resource = str(raw.get("resource_urn", "")).strip()
        provider = str(
            raw.get(
                "credential_provider_name",
                os.environ.get("IAS_PROVIDER_NAME", ""),
            )
        ).strip()
        if not resource.startswith("urn:") or not provider:
            raise ValueError("invalid IAS resource URN or AgentCore credential provider")
        return cls(resource, provider)


@dataclass(frozen=True)
class RateLimitSettings:
    window_seconds: int = 60
    max_tool_calls: int = 30

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> RateLimitSettings:
        window = int(raw.get("window_seconds", 60))
        maximum = int(raw.get("max_tool_calls", 30))
        if not 1 <= window <= 3600 or not 1 <= maximum <= 100_000:
            raise ValueError("invalid rate-limit settings")
        return cls(window, maximum)


@dataclass(frozen=True)
class RuntimeConfig:
    model_id: str
    log_level: str
    agents: tuple[SapAgent, ...]
    ias: IasSettings
    rate_limit: RateLimitSettings = field(default_factory=RateLimitSettings)
    version: int = 0
    updated_at: str | None = None
    updated_by: str | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> RuntimeConfig:
        agents = tuple(SapAgent.parse(item) for item in raw.get("agents", []))
        ids = [agent.id for agent in agents]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate SAP agent IDs")
        model_id = str(raw.get("model_id", "")).strip()
        if not model_id or len(model_id) > 256:
            raise ValueError("model_id is required")
        log_level = str(raw.get("log_level", "INFO")).upper()
        if log_level not in _LOG_LEVELS:
            raise ValueError("invalid log_level")
        return cls(
            model_id=model_id,
            log_level=log_level,
            agents=agents,
            ias=IasSettings.parse(dict(raw.get("ias") or {})),
            rate_limit=RateLimitSettings.parse(dict(raw.get("rate_limit") or {})),
            version=max(0, int(raw.get("version", 0))),
            updated_at=raw.get("updated_at"),
            updated_by=raw.get("updated_by"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def agent(self, agent_id: str) -> SapAgent:
        for item in self.agents:
            if item.id == agent_id:
                return item
        raise KeyError(f"unknown SAP agent: {agent_id}")

    def resource_for(self, agent: SapAgent) -> str:
        return agent.aud_urn or self.ias.resource_urn

    def fingerprint(self) -> str:
        stable = {
            "provider": self.ias.credential_provider_name,
            "resource": self.ias.resource_urn,
            "version": self.version,
        }
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


def default_config() -> RuntimeConfig:
    """Fallback config used only if the DynamoDB config row is missing/unreadable.

    The live config normally comes from the DynamoDB row (see docs/agent-config.md);
    replace the placeholders below with your own agent(s).
    """
    return RuntimeConfig.parse(
        {
            "model_id": "eu.amazon.nova-lite-v1:0",
            "log_level": "INFO",
            "agents": [
                {
                    "id": "example_agent",
                    "name": "Example SAP Agent",
                    "description": "Replace with the SAP agent's domain — what it handles.",
                    "gateway_url": (
                        "https://<tenant>.access.sapdas.cloud.sap/v1/a2a/"
                        "<agent-path>/jsonrpc"
                    ),
                }
            ],
            "ias": {
                "resource_urn": "urn:sap:identity:application:provider:name:agent-gateway",
                "credential_provider_name": os.environ.get(
                    "IAS_PROVIDER_NAME", "<ias-credential-provider-name>"
                ),
            },
            "rate_limit": {"window_seconds": 60, "max_tool_calls": 30},
        }
    )
