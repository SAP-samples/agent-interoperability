# Microsoft - SAP Agent Interoperability

This cookbook explains how to connect a Microsoft Copilot Studio agent to an agent on the **SAP Business AI Platform** through **SAP Agent Gateway**, so the Copilot agent can delegate a task and get the SAP agent's answer back - with real per-user identity. It corresponds to the Reference Architecture [**Microsoft Copilot Integration with Joule Agents**](https://architecture.learning.sap.com/docs/ref-arch/a2c0f1).

## Architecture Overview

The following diagram depicts the architecture components relevant for agent integration between Microsoft and SAP Business AI Platform.

**Diagram:** [`./images/architecture.drawio`](./images/architecture.drawio)

![`./images/architecture.png`](./images/architecture.png)

### SAP side

- **Business Transformation Management** - cross-cutting governance layer: *SAP Signavio* (Agent Mining), *SAP LeanIX* (AI Governance), *SAP Cloud ALM* (AI Observability & Eval).
- **Joule Work** (powered by Joule) - authoring and orchestration: *Joule Studio* (build agents via *Custom Agents* and *Agent Extensions* / Intent-based Development), *Joule Orchestrator* (Agent Harness - routes via Skills and A2A), *Tools* (Data Products, Grounding, Knowledge Graph, Ontology).
- **Managed Runtimes** - host Joule Agents, Custom Agents & MCP Servers; connected to Agent Gateway via A2A and MCP.
- **Agent Gateway** - Discovery & Invocation of Agentic Resources; the entry point for external A2A traffic crossing the network boundary.
- **Global Account** (Customer-managed) - customer's own Subaccount hosting *Custom Agents* (SAP Cloud SDK for AI & Generative AI Hub) and *SAP Integration Suite* (MCP Gateway, A2A Connectivity).
- **Autonomous Suite** - SAP LoB agents and apps: Autonomous Finance, Autonomous Spend, Autonomous SCM, Autonomous HCM, Autonomous CX, Industry AI.
- **SAP Cloud Identity Services** - identity hub; bidirectional **OIDC trust** with Microsoft Entra ID, and authorization for Agent Gateway and SAP Integration Suite.

### Microsoft side

- **Application Clients** (Mobile/Desktop/Web) - the end-user entry point, including Microsoft 365 and Teams.
- **Microsoft Azure / Microsoft AI** - orchestration layer hosting *Copilot Studio* (the A2A client that delegates tasks to the SAP Agent Gateway) and the *Power Platform Custom Connector* (performs the token exchange before forwarding A2A calls).
- **Microsoft Entra ID** - corporate IdP; authenticates users and federates trust with SAP Cloud Identity Services.

### Identity and Security

**Identity bridge.** SAP Cloud Identity Services (SCI/IAS) and Microsoft Entra ID share a bidirectional **OIDC trust**: Entra authenticates end-users; the trust chain enables **principal propagation** downstream into SAP backends.

**Auth flow patterns.** When a third-party agent calls SAP agents on behalf of a user, two IAM integration patterns are possible:

| Pattern | How the user's SAP identity is established | Browser involvement |
|---|---|---|
| **Front-Channel** (Authorization Code Grant) | The third-party agent initiates an OAuth Authorization Code flow **directly with SAP Cloud Identity Services** - the user's browser is redirected to SCI/IAS for login | Browser redirected to SAP (cookie-based, potential extra attack vector) |
| **Back-Channel** (Token Exchange) | The user authenticates against the **third-party IdP** (e.g. Entra ID); the agent exchanges that token server-to-server with SCI - no browser redirect to SAP | No SAP browser flow; exchange happens in connector code |

**Main integration pattern.** Microsoft Copilot uses the **Back-Channel** pattern (Custom Connector via Power Apps). The connector's C# code performs the `jwt-bearer` token exchange against SCI before forwarding the A2A `message/send` to the Agent Gateway. See [`./code/README.md`](./code/README.md) for a detailed explanation with architecture diagrams.

**Token exchange mechanics.** The Agent Gateway only accepts a token minted with `resource=urn:sap:identity:application:provider:name:agent-gateway` - a plain OAuth token doesn't have the `aud` the gateway expects, which is why the exchange is required. Customer/partner apps always receive `ias_apis: ["external"]`; the `sap-internal` surface is UCL-managed and cannot be self-declared.

**Principal propagation.** A **user** token passes the agent's RBAC (if that user is authorized). An app-only (`client_credentials`) token receives **403 "RBAC: access denied"** unless the app/Agent User has an AMS policy - so the user-scoped token is the correct path. For the Entra two-step exchange (Approach A), two tokens travel:

| Token | Is | Does |
|---|---|---|
| `assertion` | the end user's **Entra token** | identifies the **user**; SCI keeps `sub` through the exchange → **principal propagation** (the agent and backend see the real user; RBAC is per user) |
| `client_assertion` | an Entra **app** token | identifies the **calling app**; app tokens can't be *exchanged*, but may be used *as* a client assertion |

In Approach B (direct IAS OAuth), the connection delivers an IAS user token directly, so a single `jwt-bearer` exchange suffices - no separate `client_assertion` needed.

---

## Prerequisites

**SAP side** - complete the → [Common Setup](../00-common-setup/README.md) before proceeding.

**Microsoft side:**
- A **Copilot Studio** environment (Power Platform) where custom connectors are allowed.
- A user account that can sign in to the Entra tenant (and/or IAS, depending on the chosen approach).
- Access to **Power Apps** for custom connector creation and management.
- **Agent Gateway access** with the target agent's identifiers: base URL, agent ORD id, system/CAD artifact id.

---

## Integration Patterns

Three integration variants are available for calling an agent behind the SAP Agent Gateway from Copilot Studio:

| | Channel | What it is | Identity | Setup complexity | Wired as |
|---|---|---|---|---|---|
| **Approach A** | Back-Channel | Custom connector, **Entra** two-step exchange (user token → Entra app token → gateway token) | Per-user | Requires an Entra app registration | Tool |
| **Approach B** | Back-Channel | Custom connector, **direct IAS OAuth**, single exchange | Per-user | Simplest - no Entra app needed | Tool |
| **Approach C** | Front-Channel | Native **Connect Agent2Agent** for a BTP A2A agent that accepts a plain IAS Bearer token | Per-user | No gateway exchange needed | Add agent |

**Approach B is the recommended default** for the Agent Gateway. It requires the fewest moving parts - one IAS application, one client secret, real per-user principal propagation. Use it whenever users can authenticate at IAS directly (natively or federated into IAS).

Approach C applies only when the endpoint accepts a plain Bearer token directly (your own BTP agent, or a middleware proxy that performs the exchange - see [Known Limitations](#known-limitations)).

> For structured data / tool access (rather than full task delegation), the alternative is the MCP Gateway in Integration Suite - out of scope here (see *References*, d2e34e).

Detailed instructions for **Approach A** (Entra two-step exchange) and **Approach C** (native Connect Agent2Agent) are covered in → [Alternative Approaches](./alternative-approaches.md).

---

## Cookbook - Approach B: Agent Gateway via Direct IAS OAuth

Call an SAP agent behind the Agent Gateway from Copilot Studio. The connector authenticates the user **directly against SCI/IAS** and performs a **single** `jwt-bearer` exchange (`resource=agent-gateway`) to obtain the gateway token.

**Why Approach B over A:** no Entra app registration, no Entra client-credentials step, no Entra redirect - one IAS client secret, still real per-user principal propagation. The setup is the simplest of the three variants while delivering the same security guarantee.

> This is still a **custom connector = a Tool**. It does **not** go through "Add agent" / Connect Agent2Agent - that dialog is for Approach C. The exchange lives in connector C# code, which exists only on the custom-connector/Tool path.

**Specific prerequisites for this approach:**
- **Agent Gateway access** + the agent identifiers (base URL, ORD id, CAD id).
- An **IAS application** (the agent app) with: **client ID + secret**, and **two grant types enabled** - `authorization_code` (connection sign-in) **and** `jwt-bearer` (the exchange). Add `refresh_token` + scope `offline_access` for durable connections.
- Each user **provisioned in IAS** and in the agent's **RBAC group** (see Step 2).

### Step 1 - Gather the SAP Agent Details

| | Format |
|---|---|
| Agent Card (GET) | `<base>/v1/a2a/<agent-ord-id>/<cad-id>/.well-known/agent-card.json` |
| Agent (POST) | `<base>/v1/a2a/<agent-ord-id>/<cad-id>/jsonrpc` |

Confirm the raw call first in Bruno/curl (a JSON-RPC `message/send` body returns `result.artifacts[0].parts[0].text`).

### Step 2 - IAS App Configuration and User Authorization

On the IAS application, enable grant types **`authorization_code`** and **`jwt-bearer`** (+ `refresh_token` if using `offline_access`). Note the **client ID** and create/read a **client secret**.

Provision the user in IAS and grant the agent's RBAC group:

**2a - Provision the user in IAS.** (Missing user → `invalid_grant: "User is not authorized for access"` at the exchange.)
In **SCI Admin Console → Users & Authorizations → User Management**, **+ Add** → name, **Email = the user's email**, **Account Activation = "Set status active"**.

**2b - Grant the agent's RBAC group.** (Otherwise the agent returns `state:"failed"` / **"403 RBAC: access denied"**.)
Open a working user's **Groups** tab, note the agent group (e.g. `agent - Access`). Open your user → **Groups → Assign** → add that group.

### Step 3 - Create the Custom Connector in Power Apps

> **Shortcut:** **Create → Import an OpenAPI file** and pick [`./code/agw-connector-ias-oauth.swagger.json`](./code/agw-connector-ias-oauth.swagger.json) - it scaffolds General, the `InvokeA2A` POST action, and the IAS (Generic OAuth 2) security definition. After import, replace host, path IDs and the IAS tenant with your own, then do 3b (secret) and 3d (code).

**3a - General:** Scheme `HTTPS`, Host `<base>`, Base URL `/`.

**3b - Security:** Authentication type **OAuth 2.0**, Identity Provider **Generic Oauth 2**:

| Field | Value |
|---|---|
| Client ID | the IAS client ID |
| Client secret | the IAS client secret |
| Authorization URL | `https://<ias-tenant>.accounts400.ondemand.com/oauth2/authorize` |
| Token URL | `https://<ias-tenant>.accounts400.ondemand.com/oauth2/token` |
| Refresh URL | *(same as Token URL)* |
| Scope | `openid` (use `openid offline_access` for refresh) |

After saving, copy the generated **Redirect URL** into the IAS client's **redirect-uris** **before** the first sign-in.

**3c - Definition:** **+ New action** `InvokeA2A`, Verb `POST`, URL = your Agent (POST) URL, Header `Content-Type: application/json`, Body:
```json
{"jsonrpc":"2.0","id":"test-1","method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"Tell me a finance joke"}],"messageId":"msg-1","kind":"message"}}}
```
No `Authorization` header parameter.

**3d - Code:** toggle **Code Enabled**, operation `InvokeA2A`, paste [`./code/agw-connector-ias-oauth.csx`](./code/agw-connector-ias-oauth.csx). In a **local-only copy** fill `IAS_TOKEN_URL`, `IAS_CLIENT_ID`, `IAS_CLIENT_SECRET`; keep `IAS_RESOURCE_URN`. **Never commit real values.**

The script reads the IAS user token the connection delivers, performs the `jwt-bearer` exchange (`resource=agent-gateway`), sets `Authorization: Bearer`, strips `accept-language`, and forwards. It does **not** rewrite the envelope - so pin `jsonrpc` as a Formula in the tool (Step 5).

Then **Create / Update connector**.

### Step 4 - Test the Connector

**Test** tab → **New connection** → the IAS login popup opens (Generic OAuth 2) → sign in → **Connected**. Raw Body = On, paste the `message/send` body, **Test operation** → expect **200**, `state:"completed"`, result under `result.artifacts[0].parts[0].text`.

| If you see… | Cause | Fix |
|---|---|---|
| `401 missing_user_token` | connection didn't attach a token | reconnect / confirm the OAuth connection is signed in |
| `502 ias_exchange_failed … invalid_grant` | user not provisioned / not authorized in IAS | Step 2a |
| `200` but `state:"failed"`, `403 RBAC` | user not in the agent's RBAC group | Step 2b |
| `-32600 "Caller could not be resolved to a known tenant… target 'CAD…'"` | server-side tenant→deployment mapping missing (post re-deploy) | SAP side restores the mapping; check the CAD id didn't rotate |

### Step 5 - Create the Copilot Agent and Add the Tool

1. **Copilot Studio → Create → New agent** (same environment), name it, Create.
2. **Tools → + Add a tool → Connector →** your connector → `InvokeA2A` → **Add and configure** → pick the connection from Step 4.
3. **Description:** describe when to call it, e.g. *"Calls the SAP agent via SAP Agent Gateway (A2A). Use whenever the user asks for …"*
4. **Inputs:**

   | Input | Fill using | Value |
   |---|---|---|
   | `parts` | **Dynamically fill with AI** | - (the user's question) |
   | `method` | Custom value | `message/send` |
   | `jsonrpc` | **Custom value → Formula** | `"2.0"` **must be a Formula.** A plain custom value is coerced to the number `2` → gateway `"Invalid JSON-RPC Request"`. |
   | `id` | Custom value | `test-1` |
   | `messageId` | Custom value | `msg-1` |
   | `role` | Custom value | `user` |

5. **Additional details → Credentials to use = "End user credentials"** (per-user principal propagation).
6. **Agent → Overview → Instructions → Edit:**
   > When you call the tool, answer only with the text in `result.artifacts[0].parts[0].text`. If it fails or returns a connection/auth prompt, say so and do NOT invent an answer.
7. **Save.**

### Step 6 - Validate / Test

Ask the agent to use the tool.
- The **first** call per idle session may show a one-time connection consent - reconnect once and retry.
- **Prove it's real, not hallucinated:** open the tool node in the test flow and read its Outputs.

---

## Project Structure

```
03-microsoft-azure-integration/
├── README.md                                   ← This file (Approach B - main cookbook)
├── alternative-approaches.md                   ← Approach A & Approach C
├── images/
│   ├── architecture.drawio
│   ├── architecture.png
│   ├── back-channel.png
│   ├── end-to-end-architecture.png
│   └── front-channel.png
└── code/
    ├── README.md                               ← Code documentation and token-exchange diagrams
    ├── agw-connector-entra.swagger.json        ← Approach A: OpenAPI scaffold
    ├── agw-connector-entra.csx                 ← Approach A: C# connector code
    ├── agw-connector-ias-oauth.swagger.json    ← Approach B: OpenAPI scaffold
    └── agw-connector-ias-oauth.csx             ← Approach B: C# connector code
```

---

## Known Limitations

**Native A2A reaches the gateway only via a proxy.** Copilot Studio's native *Connect Agent2Agent* auth offers only None / API key / authorization_code OAuth - it can't itself perform the `jwt-bearer` exchange, and the agent card's `securitySchemes` don't drive it. So a native A2A connection can't talk to the gateway **directly** - that's what Approaches A/B's custom connector is for. It **can**, however, point at the **Agent Gateway Middleware** (`packages/agent-gateway-middleware`): the middleware serves the agent card at `/.well-known/agent-card.json`, accepts the user's plain IAS/Entra ID token, and performs the `jwt-bearer` exchange itself before forwarding (`app/middleware/auth.py`: ID-token mode → exchange). Native A2A then stays native (SAP agent as a connected peer, not a Tool), principal propagation preserved.

**The last secret can't leave the connector.** The connector's C# sandbox has no managed identity, no IMDS/egress, no client_credentials grant on the connection, no WIF source. So the client secret in the connector can't be replaced by a managed identity *inside the connector*.

---

## References

- RA0029 - "A2A and MCP for Interoperability": <https://github.com/SAP/architecture-center/blob/dev/docs/ref-arch/RA0029/1-a2a-and-mcp/drawio/architecture.drawio>
- Microsoft Copilot Integration with Joule Agents: <https://architecture.learning.sap.com/docs/ref-arch/a2c0f1>
- Copilot Studio × MCP Gateway (d2e34e): <https://architecture.learning.sap.com/docs/ref-arch/d2e34e>
- SAP Help - Invoking an SAP Agent Through IDP Trust and JWKS-Based Integration: <https://help.sap.com/docs/CROSS_DOMAIN_AGENTS/4d8be6737cf24797bf753cbcc6e2288f/e9da5158a18649168e040495b26c43bc.html>
- Copilot Studio native A2A auth options: <https://learn.microsoft.com/en-us/microsoft-copilot-studio/add-agent-agent-to-agent>
- Custom connector custom-code sandbox: <https://learn.microsoft.com/en-us/connectors/custom-connectors/write-code>
- Custom connector connection auth types: <https://learn.microsoft.com/en-us/connectors/custom-connectors/connection-parameters>
- Entra Workload Identity Federation: <https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation>
