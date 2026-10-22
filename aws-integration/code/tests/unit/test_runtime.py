from __future__ import annotations

import json

import pytest

from runtime.agent import AgentService
from runtime.config import RuntimeConfig, SapAgent, default_config
from runtime.dynamic_config import ConfigStore


def test_default_config_has_ask_and_capabilities_tools() -> None:
    config = default_config()
    tools = AgentService._tools(config)
    names = [item["toolSpec"]["name"] for item in tools]
    assert names == ["ask_example_agent", "capabilities_example_agent"]
    assert config.agents[0].gateway_url.endswith("/jsonrpc")


def test_agent_card_url_derived_from_gateway_url() -> None:
    agent = default_config().agents[0]
    assert agent.card_url == agent.gateway_url.rsplit("/jsonrpc", 1)[0] + "/.well-known/agent-card.json"
    assert agent.card_url.startswith("https://")
    assert agent.card_url.endswith("/.well-known/agent-card.json")


def test_config_rejects_non_sap_gateway_host() -> None:
    raw = default_config().to_dict()
    raw["agents"][0]["gateway_url"] = "https://evil.example/jsonrpc"
    with pytest.raises(ValueError, match="SAP Agent Gateway"):
        RuntimeConfig.parse(raw)


class FakeTable:
    def __init__(self, payload: dict) -> None:
        self.item = {"config_id": "current", "payload": json.dumps(payload)}
        self.written = None

    def get_item(self, **_kwargs: object) -> dict:
        return {"Item": self.item}

    def put_item(self, *, Item: dict) -> None:  # noqa: N803 - boto3 shape
        self.written = Item
        self.item = Item


def test_config_store_increments_version() -> None:
    table = FakeTable(default_config().to_dict())
    store = ConfigStore(table)
    saved = store.write(store.get(), updated_by="ADMIN@EXAMPLE.COM")
    assert saved.version == 1
    assert saved.updated_by == "admin@example.com"
    assert table.written["version"] == 1
