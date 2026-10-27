# Amazon Web Services - SAP Agent Interoperability

This cookbook describes how Agents on Amazon Quick can involve functionality from 
agents to an agent on the **SAP Business AI Platform** through **SAP Agent Gateway**. It corresponds to the Reference Architecture [**Amazon Quick Integration with Joule Agents**](https://architecture.learning.sap.com/docs/ref-arch/b4d7e2).

## Architecture Overview

In this approach, we offer an **MCP proxy agent** for Amazon Quick. 
It forwards every business request to an
**SAP agent** over A2A, authenticating with the **signed-in user's identity** —
propagated from Amazon Quick all the way into SAP with no second login.

The agent sits behind **AWS AgentCore**: Amazon Cognito logs the user in, AgentCore
mints a workload token that carries the user identity, and **AgentCore Identity**
performs the on-behalf-of token exchange with SAP CIS. The runtime never holds a
client secret or signs an assertion itself.

> **A note on the OIDC provider.** This walkthrough uses **Amazon Cognito** as the
> connector's OIDC provider, but Cognito is one option, not a requirement. Amazon
> Quick authenticates its users through **AWS IAM Identity Center**, which can use
> its own identity store or federate an external corporate IdP (Microsoft Entra ID,
> Okta, Ping, etc.). Any OIDC-compliant provider that issues a standards-compliant
> JWT can play the connector-login role here — Cognito, or a corporate IdP that
> federates into IAM Identity Center. Wherever this document says "Cognito", read it
> as "the configured OIDC provider"; the token names and claims differ by provider,
> but the flow is identical.

![architecture](./images/architecture.png)

---

### SAP side

- **Joule Work** (powered by Joule) - authoring and orchestration: *Joule Studio* (build agents via *Custom Agents* and *Agent Extensions* / Intent-based Development), *Joule Orchestrator* (Agent Harness - routes via Skills and A2A), *Tools* (Data Products, Grounding, Knowledge Graph, Ontology).
- **Managed Runtimes** - host Joule Agents, Custom Agents & MCP Servers; connected to Agent Gateway via A2A and MCP.
- **Agent Gateway** - Discovery & Invocation of Agentic Resources; the entry point for external A2A traffic crossing the network boundary.
- **SAP Cloud Identity Services** - accepts OBO exchange, issues user-scoped tokens

---

### AWS side

- **Amazon Quick** Chat interface + Custom MCP Connector (handles Cognito login, carries the token on every request)
- **AWS Agentcore** Runtime (JWT authorizer, WAT minting, Bedrock tool loop) + Identity (OBO token exchange with SAP CIS via private_key_jwt)
- **Amazon Cognito** OIDC provider for user auth; AWS IAM Identity Center or any OIDC-compliant IdP can substitute

---

### Identity and Security

The agent runs **no OAuth consent flow of its own**. The chain is:

1. **Amazon Cognito** logs the user into the Amazon Quick MCP connector and issues
   an access token carrying the user's `email`.
2. The **Runtime** validates that token with its CUSTOM_JWT authorizer (Cognito
   JWKS, `aud` = app client), and the **Runtime hosting layer** mints a
   **WorkloadAccessToken (WAT)** from it — a platform credential binding the
   workload identity to the user identity — injected as a request header.
3. When a SAP agent must be called, the Runtime asks **AgentCore Identity** for an
   on-behalf-of token exchange with **SAP CIS**. CIS resolves the user by email
   and returns an access token scoped to **SAP Agent Gateway**, with the user's
   email as `sub`.
4. The Runtime sends that CIS token as a `Bearer` credential on the **A2A
   JSON-RPC** call to SAP Agent Gateway.

Client authentication to CIS uses **`private_key_jwt` (RFC 7523)** — the signing
key lives in **AWS KMS** and the assertion is minted inside AgentCore Identity, so
no secret ever lives in the runtime.

---

## Prerequisites

- An **AWS account** with: Bedrock model access enabled for your chosen model,
  and permission to create KMS keys, IAM roles, DynamoDB tables, Cognito pools,
  and AgentCore (Runtime / Identity) resources.
- A **SAP BTP** landscape exposing an A2A agent behind **SAP Agent Gateway**,
  protected by **SAP CIS**.
- Admin rights in **SAP CIS** to register an OAuth application and a JWT trust.
- A test user present in both Cognito and SAP CIS with the same `email` — the OBO
  exchange resolves the SAP user by that email.
- **AWS CLI** configured for the account/region, and Python 3.13.

---

## Cookbook AWS

### Configuration

> **All code lives in [`code/`](code/).** `cd code` first — every path and command
> in this README is relative to that directory.

Configuration comes from **two** places — deploy-time environment variables for
AWS resource identifiers, and a live DynamoDB row for runtime settings.

AWS resource identifiers used by the build/deploy scripts. Copy the template and
fill it in as you go:

```bash
cp env.example .envrc
```

| Variable | Used by | What it is |
|---|---|---|
| `AWS_REGION`, `AWS_ACCOUNT` | deploy | Target account and region. |
| `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID` | deploy | End-user auth; become the Runtime's JWT authorizer (issuer + allowed audience). |
| `COGNITO_DOMAIN` | smoke test | Cognito Hosted UI domain (`run_once.py` login). |
| `CIS_CREDENTIAL_PROVIDER` | deploy → runtime | Name of the AgentCore Identity credential provider that performs the CIS OBO exchange. Passed to the runtime as `CIS_PROVIDER_NAME`. |
| `CIS_KMS_KEY_ID` | deploy | KMS key the runtime role may `kms:Sign` with (for the client assertion). |
| `CIS_RESOURCE_URN` | reference | Default SAP Agent Gateway audience URN (the live value is in DynamoDB). |
| `AGENT_CONFIG_TABLE`, `AGENT_CONFIG_KEY` | runtime | DynamoDB table + row the runtime reads live config from. |
| `CONFIG_CACHE_TTL_SECONDS` | runtime | How long the runtime caches the config row in-process. |
| `RUNTIME_NAME`, `EXEC_ROLE` | deploy | AgentCore runtime name and its dedicated execution role. |
| `CODE_BUCKET`, `CODE_S3_KEY` | deploy | Where the deployment zip is uploaded. |
| `MCP_ENDPOINT_URL` | smoke test | Runtime `/invocations` URL that `run_once.py` (and the Quick connector) call. |

> No secrets live here — the KMS key signs server-side and Cognito values are
> public. `.envrc` is git-ignored for per-user overrides. See `env.example` for the
> complete set, including the SAP CIS tenant inputs needed for Task 4
> (`CIS_TENANT_URL`, `CIS_DISCOVERY_URL`, `CIS_AUD_CLAIM`, `CIS_CLIENT_ID`) and the
> output identifiers (runtime ID/ARN) you record as you create resources.

### Step 0 (AWS) - Create the Amazon Cognito user pool

Cognito authenticates the end user into the Amazon Quick MCP connector and issues the access token that carries the user's `email` forward to SAP CIS.

1. **Create a user pool** and an **app client** (OAuth 2.0 authorization code + PKCE). Enable the **Hosted UI domain**. Record:
   - Pool id → `COGNITO_USER_POOL_ID`
   - App client id → `COGNITO_APP_CLIENT_ID`
   - Domain → `COGNITO_DOMAIN`
2. **Add the Amazon Quick callback URL** to the app client's allowed callback URLs (per region):
   ```
   https://<region>.quicksight.aws.amazon.com/sn/oauthcallback
   ```
3. **Add a pre-token-generation Lambda trigger** that injects `email` and `aud` into the **access** token. A Cognito access token carries neither by default; SAP CIS needs `email` to resolve the user and requires `aud` on the OBO exchange. Set `aud` = `COGNITO_APP_CLIENT_ID`. See [info/pre-token-lambda.md](info/pre-token-lambda.md) for the function source and how to wire it to the pool (V2 trigger).
4. **Align the test user with SAP CIS.** The Cognito user's email must match a provisioned SAP CIS user (it becomes the token's `sub` in the OBO exchange).

---

### Step 1 (SAP) - Add Amazon Cognito as a corporate identity provider in SAP CIS

Register the Cognito user pool from Task 0 as a **corporate identity provider** in SAP Cloud Identity Services (CIS) so CIS trusts the identities Cognito issues to the end user.

1. Open the **CIS admin console** → **Identity Providers** → **Corporate Identity Providers** → **Create**.
2. Set the identity provider **type** to **OpenID Connect Compliant**.
3. In the **OpenID Connect configuration**, enter the Cognito app client details from Task 0.
4. Under **Identity Federation → Single Sign-On**, enable **Use Identity Authentication user store**.

---

### Step 2 (AWS) - Create the KMS signing key and publish its JWKS

The client assertion that authenticates to SAP CIS is signed inside AWS with a
KMS key; SAP CIS verifies it against the matching **public** key published at a
JWKS URL.

1. **Create an asymmetric RSA signing key** (`RSA_2048`, usage `SIGN_VERIFY`,
   `RS256`). Record its key id → `CIS_KMS_KEY_ID`.
2. **Export the public key** and publish it as a JWKS document at a public HTTPS
   URL SAP CIS can reach (e.g. a static file in S3). The JWKS entry must carry
   `kty: RSA`, `use: sig`, `alg: RS256`, and a **`kid` equal to the KMS key id**.

> Key rotation = publish a new JWKS entry with a new `kid` and point the
> credential provider at the new KMS key. There is no shared secret to rotate.

---

### Step 3 (SAP) - Onboard the AWS agent as an Agent Gateway Consumer in SAP CIS

Register the AWS agent in **SAP for Me** as an **Agent Gateway Consumer** system, assign it to the three formations, and configure IdP trust in **SAP Cloud Identity Services (CIS)**. Full click-by-click steps are in the SAP documentation:
→ [Creating an Agent Gateway Consumer System and assigning it to formations](https://help.sap.com/docs/CROSS_DOMAIN_AGENTS/4d8be6737cf24797bf753cbcc6e2288f/a0465991ba964736a781c275ddd852db.html)

1. **Create the Agent Gateway Consumer system** in SAP for Me. Set its **JWKS Endpoint URL** to the JWKS you published in Task 2.
2. **Assign the system to the three formations, in the mandatory order.**
3. **Record** the OIDC application's **client id** (CIS admin console → Applications & Resources → Applications — the application created for this system) → `CIS_CLIENT_ID`, and note your tenant base URL (e.g. `https://<tenant>.accounts.ondemand.com`) and its discovery URL (`/.well-known/openid-configuration`).
4. **Configure Identity Provider trust** so CIS validates the tokens issued via your IdP (the JWKS/IdP-trust path — no client secret is created):
   - Log in to the **CIS Administration Console** → **Applications & Resources → Applications**, and open the application created for this system.
   - Go to **Trust → Trust Corporate Identity Providers**, click **Edit**, select the Cognito corporate identity provider you added in Task 1, and click **Save**.

---

### Step 4 (SAP) - Discover the SAP agent behind Agent Gateway

Use the Agent Gateway's **Agentic Resource Discovery (ARD)** endpoints to list the available SAP agents and derive the two values this cookbook needs: the agent's A2A endpoint (`gateway_url`) and the Gateway resource URN (`ias.resource_urn`).

Full reference:
→ [Discovering SAP Agents Using Agentic Resource Discovery Endpoints](https://help.sap.com/docs/CROSS_DOMAIN_AGENTS/4d8be6737cf24797bf753cbcc6e2288f/418a00aac347473e866cb7987119b9f9.html)

1. **Get the Agent Gateway base URL** — in SAP for Me → Customer Landscape → Systems, open the **Agent Gateway** system and note its URL (e.g. `https://eu12.access.sapdas.cloud.sap`). This is `<agent-gateway-base-url>`.
2. **Get a token for the ARD call.** Discovery accepts a **technical token** (client credentials, no logged-in user) — note that invoking an agent later requires a *user* token, not this one. Request it from CIS:
   ```
   POST <CIS-tenant-url>/oauth2/token
   Content-Type: application/x-www-form-urlencoded

   grant_type=client_credentials
   resource=urn:sap:identity:application:provider:name:agent-gateway
   client_id=<CIS app client id>
   client_secret=<CIS app client secret>
   token_format=jwt
   ```
   Pass the returned token as `Authorization: Bearer <token>` on the ARD request.
3. **Fetch the agent catalog:**
   ```
   GET <agent-gateway-base-url>/v1/.well-known/ard.json
   ```
   (`/v1/.well-known/ai-catalog.json` is an identical alias.) Find your agent in the `entries[]` array; its `url` field is the agent-card URL, e.g.
   `<agent-gateway-base-url>/a2a/<ordId>/<agentTenantId>/.well-known/agent-card.json`.
4. **Record the two cookbook values:**
   - `gateway_url` — the agent's A2A JSON-RPC endpoint: take the catalog `url` and replace `/.well-known/agent-card.json` with `/jsonrpc`.
   - `ias.resource_urn` — the Gateway resource URN `urn:sap:identity:application:provider:name:agent-gateway` (the `resource` value from step 2).

> The runtime derives the agent card from `gateway_url` automatically (strip `/jsonrpc`, append `/.well-known/agent-card.json`) — no separate value to record.

---

### Step 5 (AWS) - Create the AgentCore Identity credential provider

Create an OAuth2 credential provider in AgentCore Identity using the JSON below. Record the `name` you choose → `CIS_CREDENTIAL_PROVIDER`.

```json
{
  "name": "<CIS_CREDENTIAL_PROVIDER>",
  "credentialProviderVendor": "CustomOauth2",
  "oauth2ProviderConfigInput": {
    "customOauth2ProviderConfig": {
      "oauthDiscovery": { "discoveryUrl": "https://<tenant>.accounts.ondemand.com/.well-known/openid-configuration" },
      "clientId": "<CIS client id from Task 3>",
      "clientAuthenticationMethod": "PRIVATE_KEY_JWT",
      "onBehalfOfTokenExchangeConfig": { "grantType": "JWT_AUTHORIZATION_GRANT" },
      "privateKeyJwtConfig": {
        "privateKeySource": { "kmsKeySource": { "kmsKeyArn": "arn:aws:kms:<region>:<account>:key/<CIS_KMS_KEY_ID>" } },
        "signingAlgorithm": "RS256",
        "additionalHeaderClaims": { "kid": "<CIS_KMS_KEY_ID>" },
        "additionalPayloadClaims": { "aud": "https://<tenant>.accounts.ondemand.com" }
      }
    }
  }
}
```

Key fields: `discoveryUrl` points to the CIS tenant, `clientId` is the CIS app client from Task 3, and `kmsKeyArn` is the signing key from Task 2. The `kid` in `additionalHeaderClaims` must match `CIS_KMS_KEY_ID` so CIS can look up the right public key in the JWKS.

---

### Step 6 (AWS) - Create the DynamoDB config table and seed the row

Create a DynamoDB table with a single string partition key named `config_id`, on-demand billing, and encryption at rest enabled. Then add one item with `config_id = <AGENT_CONFIG_KEY>` and a `payload` attribute containing the runtime config as a JSON string.

See [info/agent-config.md](info/agent-config.md) for the full structure with placeholders and a field reference.

---

### Step 7 (AWS) - Build and deploy the runtime

> **Swap in your own agent first.** The `runtime/` in this repo is a complete,
> working reference agent. Replace it with your own AgentCore agent before building,
> keeping the identity-propagation wiring intact — then run the steps below.

```bash
./build_mcp.sh        # vendors linux/aarch64 wheels + bundles mcp_server.py + runtime/
./deploy.sh           # creates the execution role, uploads the zip, creates/updates the runtime
```

`deploy.sh` sets the runtime's **JWT authorizer** from your Cognito values
(issuer = `https://cognito-idp.<region>.amazonaws.com/<pool-id>`, allowed
audience = app client id, allowed scopes = the app client's OAuth scopes
`openid email profile`) and grants the execution role exactly what the runtime
needs: read the config table, mint the WAT, call `GetResourceOauth2Token`, invoke
Bedrock, and `kms:Sign` with the signing key. It polls until the runtime is
`READY` and prints the runtime ARN.

> `allowedScopes` is **required** for the Amazon Quick connector (Task 8). The
> runtime advertises these scopes during Quick's OAuth discovery; without them
> Quick falls back to `offline_access`, which Cognito rejects with `invalid_scope`.
> The values must match the Cognito app client's `AllowedOAuthScopes`.

---

### Step 8 (AWS) - Register the Amazon Quick MCP connector

The connector is the bridge that triggers the Cognito login and carries the
resulting token to the AgentCore Runtime on every message. Register it in the
Amazon Quick admin console:

**Navigate:** Left panel → **Connectors → Create custom MCP connector**.

**Step 1 — Integration details**

| Field | What to enter |
|---|---|
| Name | A descriptive connector name. |
| Description | A short, clear sentence describing what SAP data/actions it exposes, so users know when to pick it. |
| MCP server endpoint | The runtime's direct `/invocations` URL (see below). |
| Connection type | Network path from Quick to the MCP endpoint — public network, or a VPC connection for a private deployment. |
| Auth server connection type | Network path from Quick to Cognito — public network, or a VPC connection for a private endpoint. |

Build the MCP server endpoint from the runtime ARN (`RUNTIME_ARN`, from Task 7):

```bash
ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$RUNTIME_ARN")
echo "https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/${ENC}/invocations?qualifier=DEFAULT"
```

The ARN is URL-encoded (every `:` and `/` becomes `%3A`/`%2F`) and `qualifier=DEFAULT`
means future redeploys are picked up automatically — no connector change needed.

**Step 2 — Authentication**

Select **Custom user based OAuth**, then fill in:

| Field | What to enter |
|---|---|
| Client ID | The Cognito app client id (`COGNITO_APP_CLIENT_ID`). |
| Public OAuth client | If the app client has **no** secret (PKCE-only), mark it public — no secret needed. If it has a secret, leave unchecked and provide the secret. |
| Token URL | `https://<COGNITO_DOMAIN>/oauth2/token` |
| Authorization URL | `https://<COGNITO_DOMAIN>/oauth2/authorize` |

These point directly at the Cognito Hosted UI endpoints — no intermediate service
is involved. Quick exchanges the authorization code at the token endpoint and uses
the returned access token as the Bearer credential on every MCP request.

**Step 3 — Manage read and write permissions**

Quick discovers the tools exposed by the endpoint (e.g. `sap_assistant`) and lists
them. Review each and assign it as a read or write operation according to what it
does; this controls what connector users may invoke.

**Publish**

Publish the connector. Quick runs a discovery pass against the MCP endpoint; when
it completes the connector shows as **Available** and its tools are ready. Users
can then invoke it directly from the Quick chat interface.

---

### Task 9 — Validate end to end

From the Amazon Quick chat interface, ask a question related to the SAP agent. On first run you will be prompted to log in via Cognito.

**Expected:** the connector triggers a Cognito login, forwards the token to the AgentCore Runtime, and returns the SAP agent's answer. CloudWatch logs at `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT` show the Bedrock tool loop and a successful CIS exchange.

**Terminal validation (alternative to Quick):** If the Cognito app client allows `http://localhost:8765/callback`, the smoke test can be run directly:

```bash
source .envrc
python3 tests/integration/run_once.py "show the team compliance trainings"
```

**Validated output:**
```
[1/3] initialize     → HTTP 200, session established
[2/3] tools/list     → sap_assistant tool listed
[3/3] tools/call     → HTTP 200, SAP agent answer received
```
Full chain confirmed: Cognito → Runtime → Bedrock → CIS OBO → SAP Agent Gateway → SAP agent → response.

---

## Project structure

```
aws-sap-interop-cookbook/
├── README.md                      # this file
├── assets/architecture.png
└── code/                          # ← cd here; all commands/paths below are relative to it
    ├── mcp_server.py              # MCP front door: captures Authorization / WAT /
    │                              #   session headers, exposes the sap_assistant tool
    ├── runtime/
    │   ├── agent.py               # Bedrock routing brain: one ask_/capabilities_ tool
    │   │                          #   pair per SAP agent; runs the tool loop
    │   ├── cis.py                 # CIS on-behalf-of token exchange via AgentCore
    │   │                          #   Identity (+ per-user token cache)
    │   ├── sap_gateway.py         # outbound A2A client: message/send + agent-card fetch
    │   ├── config.py              # validated RuntimeConfig schema (model, agents, CIS)
    │   ├── dynamic_config.py      # reads/caches the live config row from DynamoDB
    │   ├── rate_limit.py          # per-user sliding-window limiter
    │   └── resume_token.py        # opaque handle for multi-turn (input-required) tasks
    ├── tests/
    │   ├── unit/test_runtime.py   # unit tests
    │   └── integration/run_once.py# e2e smoke test (Cognito login → MCP tools/call)
    ├── build_mcp.sh               # package the deployment zip
    ├── deploy.sh                  # deploy the runtime to AgentCore (create or update)
    ├── requirements.txt           # runtime dependencies (vendored into the zip)
    ├── requirements-dev.txt       # dev/test-only deps (pytest)
    ├── env.example                # copy to .envrc and fill in
    ├── ROADMAP.md                 # end-to-end setup recipe + troubleshooting
    └── info/
        ├── agent-config.md        # DynamoDB config-row structure + field reference
        ├── pre-token-lambda.md    # Cognito pre-token Lambda source + wiring
        └── aws-resources.md       # log of every AWS resource + the exact create command
```

---

## Known Limitations

None identified at this time.

---

## References

- AWS AgentCore Documentation: <https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html>
- RFC 8693 (Token Exchange): <https://www.rfc-editor.org/rfc/rfc8693>
- RFC 7523 — JWT Profile for OAuth 2.0 Client Authentication (private_key_jwt): <https://www.rfc-editor.org/rfc/rfc7523>
- SAP IAS OAuth: <https://help.sap.com/docs/identity-authentication>
- SAP Agent Gateway: <https://help.sap.com/docs/CROSS_DOMAIN_AGENTS>
- Amazon Quick MCP: <https://docs.aws.amazon.com/quick/latest/userguide/mcp-integration.html>
- Reference Implementation (SAP Headless Team): <https://github.tools.sap/OCTO/ag-gated-access/tree/docs/usecase-google-agent-a2a/code/aws-agent>

