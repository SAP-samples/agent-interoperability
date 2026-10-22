# aws-agent

**MCP proxy agent** for Amazon Quick. It forwards every business request to a
**SAP agent** over A2A, authenticating with the **signed-in user's identity** —
propagated from Amazon Quick all the way into SAP with no second login.

The agent sits behind **AWS AgentCore**: Amazon Cognito logs the user in, AgentCore
mints a workload token that carries the user identity, and **AgentCore Identity**
performs the on-behalf-of token exchange with SAP IAS. The runtime never holds a
client secret or signs an assertion itself.

- **Setup & configuration** (KMS, SAP IAS, Cognito, credential provider, deploy,
  Quick connector): see [ROADMAP.md](ROADMAP.md).
- **Design rationale & why this model:** see [RESEARCH.md](RESEARCH.md).

---

## How auth works (short version)

The agent runs **no OAuth consent flow of its own**. The chain is:

1. **Amazon Cognito** logs the user into the Amazon Quick MCP connector and issues
   an access token carrying the user's `email`.
2. **AgentCore Gateway** validates that token (Cognito JWKS, `aud` = app client)
   and forwards it unchanged to the Runtime (JWT passthrough).
3. The **Runtime hosting layer** mints a **WorkloadAccessToken (WAT)** from the
   forwarded token — a platform credential binding the workload identity to the
   user identity — and injects it as a request header.
4. When a SAP agent must be called, the Runtime asks **AgentCore Identity** for an
   on-behalf-of token exchange with **SAP IAS**. IAS resolves the user by email
   and returns an access token scoped to **SAP Agent Gateway**, with the user's
   email as `sub`.
5. The Runtime sends that IAS token as a `Bearer` credential on the **A2A
   JSON-RPC** call to SAP Agent Gateway.

Client authentication to IAS uses **`private_key_jwt` (RFC 7523)** — the signing
key lives in **AWS KMS** and the assertion is minted inside AgentCore Identity, so
no secret ever lives in the runtime. See [RESEARCH.md](RESEARCH.md) for why this
model is used and how the IAS exchange works under the hood.

---

## Configuration

Configuration comes from **two** places — deploy-time environment variables for
AWS resource identifiers, and a live DynamoDB row for runtime settings.

### 1. Deploy / environment config (`.envrc`)

AWS resource identifiers used by the build/deploy scripts. Copy the template and
fill it in:

```bash
cp env.example .envrc
```

| Variable | Used by | What it is |
|---|---|---|
| `AWS_REGION`, `AWS_ACCOUNT` | deploy | Target account and region. |
| `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID` | deploy | End-user auth; become the Runtime's JWT authorizer (issuer + allowed audience). |
| `COGNITO_DOMAIN` | smoke test | Cognito Hosted UI domain (`run_once.py` login). |
| `IAS_CREDENTIAL_PROVIDER` | deploy → runtime | Name of the AgentCore Identity credential provider that performs the IAS OBO exchange. Passed to the runtime as `IAS_PROVIDER_NAME`. |
| `IAS_KMS_KEY_ID` | deploy | KMS key the runtime role may `kms:Sign` with (for the client assertion). |
| `IAS_RESOURCE_URN` | reference | Default SAP Agent Gateway audience URN (the live value is in DynamoDB). |
| `AGENT_CONFIG_TABLE`, `AGENT_CONFIG_KEY` | runtime | DynamoDB table + row the runtime reads live config from. |
| `CONFIG_CACHE_TTL_SECONDS` | runtime | How long the runtime caches the config row in-process. |
| `RUNTIME_NAME`, `EXEC_ROLE` | deploy | AgentCore runtime name and its dedicated execution role. |
| `CODE_BUCKET`, `CODE_S3_KEY` | deploy | Where the deployment zip is uploaded. |

> No secrets live here — the KMS key signs server-side and Cognito values are
> public. `.envrc` is git-ignored for per-user overrides.

### 2. Live runtime config (DynamoDB row)

The model, the SAP agent list, and the IAS settings are **not** env vars — the
runtime reads them from a DynamoDB row (`AGENT_CONFIG_KEY`) on every request
(cached for `CONFIG_CACHE_TTL_SECONDS`). The runtime **only reads** this row —
it is written out-of-band by an operator (an admin surface outside this agent),
never by the runtime itself. Validated by `RuntimeConfig` in
[`runtime/config.py`](runtime/config.py). The row holds:

| Field | Meaning |
|---|---|
| `model_id` | Bedrock model the routing brain runs on (e.g. `eu.amazon.nova-lite-v1:0`). |
| `agents[]` | Each SAP agent: `id`, `name`, `description`, `gateway_url` (A2A JSON-RPC endpoint), optional `aud_urn`. |
| `ias.resource_urn` | Default SAP Agent Gateway audience for the OBO exchange. |
| `ias.credential_provider_name` | AgentCore Identity credential provider that mints the IAS token. |
| `rate_limit` | `window_seconds`, `max_tool_calls` per user. |

Changing the model or adding a SAP agent is a **config edit, not a redeploy**.

---

## Project structure

```
aws-agent/
├── mcp_server.py              # MCP front door: captures Authorization / WAT /
│                              #   session headers, exposes the sap_assistant tool
├── runtime/
│   ├── agent.py               # Bedrock routing brain: one ask_/capabilities_ tool
│   │                          #   pair per SAP agent; runs the tool loop
│   ├── ias.py                 # IAS on-behalf-of token exchange via AgentCore
│   │                          #   Identity (+ per-user token cache)
│   ├── sap_gateway.py         # outbound A2A client: message/send + agent-card fetch
│   ├── config.py              # validated RuntimeConfig schema (model, agents, IAS)
│   ├── dynamic_config.py      # reads/caches the live config row from DynamoDB
│   ├── rate_limit.py          # per-user sliding-window limiter
│   └── resume_token.py        # opaque handle for multi-turn (input-required) tasks
├── tests/
│   ├── unit/test_runtime.py   # unit tests
│   └── integration/run_once.py# e2e smoke test (Cognito login → MCP tools/call)
├── build_mcp.sh               # package the deployment zip
├── deploy.sh                  # deploy the runtime to AgentCore (additive)
├── requirements.txt           # runtime dependencies
├── env.example                # copy to .envrc and fill in
├── ROADMAP.md                 # end-to-end setup recipe + troubleshooting
├── RESEARCH.md                # design rationale + token-exchange mechanism
└── README.md
```

---

## Requirements

- **AWS CLI** configured for the target account / region.
- **Python 3.13** (the AgentCore direct-code runtime; no Docker required).
- **Amazon Bedrock model access** enabled for the configured `model_id`.
- The supporting infrastructure set up per [ROADMAP.md](ROADMAP.md) (KMS key, SAP
  IAS credential provider, DynamoDB config table, Cognito pool, gateway) — follow
  it first if you are starting from scratch.

---

## Quick start (local smoke test)

Build the deployment zip and exercise the deployed endpoint end to end:

```bash
# package the runtime
./build_mcp.sh

# Cognito login → MCP initialize / tools/list / tools/call sap_assistant
python3 tests/integration/run_once.py "show me my latest compliance training"
```

`run_once.py` has no local-path dependencies — it drives the live gateway endpoint
through a real Cognito login, confirming the full chain works.

---

## Commands

| Command | What it does |
|---|---|
| `./build_mcp.sh` | Bundle `mcp_server.py` + `runtime/` into `deployment-mcp.zip`. |
| `./deploy.sh` | Upload the zip and create/update the AgentCore runtime (additive — does not touch the live runtime). |
| `python3 tests/integration/run_once.py "<prompt>"` | End-to-end smoke test against the gateway. |
| `python3 -m pytest tests/unit` | Run unit tests (`pip install -r requirements-dev.txt` first). |

---

## Development

- **Agent logic:** edit [`runtime/agent.py`](runtime/agent.py) (the Bedrock tool
  loop and routing). Redeploy with `./build_mcp.sh && ./deploy.sh`.
- **Model / SAP agent list / IAS settings:** edit the **DynamoDB config row**
  out-of-band (operator/admin surface) — no redeploy needed; the runtime picks it
  up within `CONFIG_CACHE_TTL_SECONDS`.

---

## Deployment

```bash
./build_mcp.sh        # package
./deploy.sh           # create/update the runtime, poll until READY
```

Then wire it to the front door (one-time, see [ROADMAP.md](ROADMAP.md)):

1. Add a **gateway target** on the AgentCore Gateway pointing at this runtime
   (`qualifier=DEFAULT`, `JWT_PASSTHROUGH`).
2. Register an **Amazon Quick MCP connector** at the gateway target's
   `/invocations` URL, with the Cognito app client as its OAuth client.

Future redeploys need no gateway or connector change — the target follows
`DEFAULT` automatically.

---

## Observability

Runtime logs stream to **CloudWatch** at:

```
/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT
```

The logs show the Bedrock tool loop (which `ask_`/`capabilities_` tools the model
called) and the outcome of each SAP call — never prompt/response content or tokens.
