# Agent Runtime Config

The DynamoDB item (`config_id = <AGENT_CONFIG_KEY>`) stores the runtime configuration as a JSON string in its `payload` attribute. The structure below shows all fields with placeholders — replace angle-bracketed values with your own.

```json
{
  "version": 1,
  "model_id": "<bedrock-model-id>",
  "log_level": "INFO",
  "agents": [
    {
      "id": "<agent-id>",
      "name": "<agent-name>",
      "description": "<one-sentence description shown to the LLM>",
      "gateway_url": "https://<tenant>.access.sapdas.cloud.sap/v1/a2a/<agent-path>/jsonrpc",
      "aud_urn": null
    }
  ],
  "ias": {
    "resource_urn": "<IAS_RESOURCE_URN>",
    "credential_provider_name": "<IAS_CREDENTIAL_PROVIDER>"
  },
  "rate_limit": {
    "window_seconds": 60,
    "max_tool_calls": 30
  }
}
```

## Field reference

| Field | Description |
|---|---|
| `version` | Start at `1`; increment on every update so the runtime detects stale in-process state. |
| `model_id` | Bedrock model id, e.g. `eu.amazon.nova-lite-v1:0`. |
| `log_level` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `agents[].id` | Lowercase alphanumeric slug (max 40 chars). Used internally to route tool calls. |
| `agents[].name` | Short human-readable label. |
| `agents[].description` | Shown to the LLM as the tool description — be specific about what the agent does. |
| `agents[].gateway_url` | SAP Agent Gateway JSON-RPC endpoint (must end in `/jsonrpc`, host must be `*.sapdas.cloud.sap`). |
| `agents[].aud_urn` | Optional per-agent audience URN override. If `null`, falls back to `ias.resource_urn`. |
| `ias.resource_urn` | URN identifying the Agent Gateway to SAP IAS, e.g. `urn:sap:identity:application:provider:name:agent-gateway`. |
| `ias.credential_provider_name` | Name of the AgentCore Identity credential provider created in Task 5. |
| `rate_limit.window_seconds` | Rolling window for the per-session tool-call counter. |
| `rate_limit.max_tool_calls` | Maximum tool calls allowed within the window. |
