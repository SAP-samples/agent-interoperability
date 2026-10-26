# Microsoft - SAP Agent Interoperability: Alternative Approaches

> This document covers **Approach A** and **Approach C**. The recommended default is **[Approach B](./README.md)**, documented in the main README.

---

## Approach A - Agent Gateway (A2A) via Custom Connector, Entra Two-Step Exchange

Call an SAP agent **behind the Agent Gateway** from Copilot Studio using a **Power Apps custom connector** that performs a two-step token exchange in connector C# code before forwarding the A2A `message/send`.

The Gateway only accepts a token minted via a **`jwt-bearer` token exchange** against SCI:

- `assertion` = the end user's Entra token
- `client_assertion` = an Entra app token
- `resource = urn:sap:identity:application:provider:name:agent-gateway`

Copilot Studio's native A2A auth can only send one plain Bearer token (see [Known Limitations](./README.md#known-limitations--roadmap)), so the custom connector performs the exchange in **connector C# code** before forwarding.

Result: real **principal propagation** - the gateway token carries the actual user (`sub`), `ias_apis: ["external"]`.

> **Custom connector = a Tool, never "Add agent".** Approach A builds a Power Apps **custom connector** and attaches it via **Tools → + Add a tool → Connector**. A custom connector **never** appears in the **Add agent / Connect Agent2Agent** dialog - that dialog lists only native A2A agents (with an agent card) and has **no code step**. The exchange lives in the connector's C# (Code tab), which exists only on the custom-connector/Tool path. The native "Add agent" route is **Approach C** below.

![Back-channel token exchange](./images/back-channel.png)

### Prerequisites

- **Agent Gateway access** + the target agent's identifiers: base URL, agent ORD id, system/CAD artifact id.
- **SCI admin** on the third-party-agent application, plus a one-time **trust setup** (Step 2 preconditions).
- An **Entra app registration** for the connector client, with a client secret and an *Expose an API* scope (`access_as_user`).
- A **Copilot Studio** environment where custom connectors are allowed, and a user who can sign in to that Entra tenant.

### Step 1 - Gather the SAP Agent Details

| | Format |
|---|---|
| Agent Card (GET) | `<base>/v1/a2a/<agent-ord-id>/<cad-id>/.well-known/agent-card.json` |
| Agent (POST) | `<base>/v1/a2a/<agent-ord-id>/<cad-id>/jsonrpc` |

Confirm the raw call first in Bruno/curl (a JSON-RPC `message/send` body returns `result.artifacts[0].parts[0].text`).

### Step 2 - SCI Setup: Trust and User Authorization

> **One-time precondition:** Entra is trusted as a **corporate IdP** in SCI, and the Entra app token is accepted as a **client assertion** via **Applications → \<agent app\> → Client Authentication → Configure Trust by Issuer** (Issuer = Entra STS `https://sts.windows.net/<tenant>/`, JWKS = `https://login.windows.net/common/discovery/keys`, Subject = Entra app `oid`, Audience = `api://<entra-app-id>`).

The gateway authorizes the **real user** at two layers - a new user is rejected until **both** grants exist. In **SCI Admin Console → Users & Authorizations → User Management**:

**2a - Provision the user in IAS.** (Missing user → `invalid_grant: "User is not authorized for access"` at the exchange.)
- **+ Add** → name, **Email = the user's Entra email**, **Account Activation = "Set status active"** (no password/mail - login is federated). The incoming Entra token is matched to this IAS user by email.

**2b - Grant the agent's RBAC group.** (Otherwise the agent returns `state:"failed"` / **"403 RBAC: access denied"**.)
- Open a **working** user's **Groups** tab, note the agent group (e.g. `agent - Access`). Open **your** user → **Groups → Assign** → add that group.

> **Tip:** diagnose the exact SCI error in the connector's **Test** tab (Step 4) - the Response body shows the full `detail`. Copilot only surfaces the error *code*.

### Step 3 - Create the Custom Connector in Power Apps

> **Shortcut:** **Create → Import an OpenAPI file** and pick [`./code/agw-connector-entra.swagger.json`](./code/agw-connector-entra.swagger.json) - it scaffolds General (host/base path), the `InvokeA2A` POST action, and the OAuth security definition. After import, replace host, path IDs and scope with your own, then do 3b (secret) and 3d (code).

**3a - General:** Scheme `HTTPS`, Host `<base>`, Base URL `/`.

**3b - Security:** Authentication type **OAuth 2.0**, Identity Provider **Azure Active Directory**:

| Field | Value |
|---|---|
| Client ID | your Entra app |
| Secret options | Client secret |
| Client secret | your client secret |
| Authorization URL | `https://login.microsoftonline.com` |
| Tenant ID | your Entra tenant |
| Resource URL | `api://<your-ias-tenant>.accounts400.cloud.sap` |
| Enable on-behalf-of login | `true` |
| Scope | `api://<your-ias-tenant>.accounts400.cloud.sap/access_as_user` |

After saving, copy the generated **Redirect URL** (`https://global.consent.azure-apim.net/redirect/…`) into the Entra app's **Redirect URIs**.

**3c - Definition:** **+ New action** `InvokeA2A`. **Request → Import from sample:** Verb `POST`, **URL** = your Agent (POST) URL, **Header** `Content-Type: application/json`, **Body**:
```json
{"jsonrpc":"2.0","id":"test-1","method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"Tell me a finance joke"}],"messageId":"msg-1","kind":"message"}}}
```
Then **Add default response** (`{}` is fine). Do **not** add an `Authorization` header - the C# sets it.
> The path contains colons (`…customer.build:agent:…`). Verify it stayed one literal segment (no `{param}` generated).

**3d - Code:** toggle **Code Enabled**, select operation `InvokeA2A`, paste [`./code/agw-connector-entra.csx`](./code/agw-connector-entra.csx). In a **local-only copy** fill the placeholders (`ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ENTRA_TENANT_ID`, `ENTRA_SCOPE`, `SCI_TOKEN_URL`, `SCI_CLIENT_ID`); keep `SCI_RESOURCE_URN`. **Never commit real values.**

The script: (1) reads the incoming Entra user token; (2) `client_credentials` → Entra app token; (3) SCI `jwt-bearer` exchange (`assertion` = user token, `client_assertion` = app token, `resource` = agent-gateway URN) → gateway token; sets `Authorization: Bearer`, strips the Power-Platform `accept-language` header, forwards.

Then **Create / Update connector**.

### Step 4 - Test the Connector

**Test** tab → **New connection** → sign in with your Entra user (Step 2) → **Connected**. **Raw Body = On**, paste the `message/send` body, **Test operation** → expect **HTTP 200**, `result.status.state = "completed"`, result under `result.artifacts[0].parts[0].text`.

| If you see… | Cause | Fix |
|---|---|---|
| `502 … invalid_grant "User is not authorized for access"` | user not provisioned in IAS | Step 2a |
| `200` but `state:"failed"`, `"403 RBAC: access denied"` | user not in the agent's RBAC group | Step 2b |
| `400 … "Validation failure for accept-language"` | injected header | script already strips it |
| `-32600 "Caller could not be resolved to a known tenant, cannot verify mapping to target 'CAD…'"` | server-side: no consumer/tenant→deployment mapping for that CAD (typical after a Joule-Studio re-deploy, which also **rotates the CAD id** in the URL) | SAP side re-establishes the mapping; also confirm the CAD id in your POST URL + swagger is current |
| `Invalid JSON-RPC Request` (in Copilot, not here) | `jsonrpc` serialized as `2` not `"2.0"` | use a Formula in Step 5 |

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

### Step 6 - Test from Copilot and Verify Authenticity

Ask the agent to use the tool.
- The **first** call per session may hit a one-time **"Let's get you connected first"** consent → **Open connection manager → Submit → Retry**.
- **Prove it's real, not hallucinated:** open the tool node in the test flow and read its Outputs.

> **Residual secret:** the Entra client secret stays in the connector C# (it can't be removed inside the connector sandbox - no managed identity reachable from ScriptBase). See the main [README - Known Limitations](./README.md#known-limitations).

---

## Approach C - Native Connect Agent2Agent for a BTP A2A Agent (OAuth 2.0)

Connect your own A2A agent on SAP BTP to a Copilot Studio agent, per-user via the IAS OAuth 2.0 Authorization Code flow. This works because the BTP agent validates a **plain IAS Bearer token** directly - no gateway exchange needed.

### Prerequisites

- A working A2A agent on SAP BTP Cloud Foundry, on a public route, serving its Agent Card at `https://<your-route>/.well-known/agent-card.json`.
- A bound IAS service instance the agent uses to validate incoming Bearer tokens; access to its service key (client ID + secret).
- A Copilot Studio agent in an environment where *Connect Agent2Agent* (preview) is enabled.

### Step 1 - Make the Agent Card Copilot-Compatible

- `defaultInputModes` / `defaultOutputModes` = `text/plain` (a bare `"text"` is treated as incompatible).
- `capabilities.streaming = false` → Copilot uses the unary `message/send` (SSE `message/stream` stalls behind Copilot's APIM proxy).
- **No** `securitySchemes` / `security` block - OAuth is set on the Copilot connection (Step 3), not the card. The server still enforces the Bearer token.
- Default **JSONRPC** transport - do not set `preferredTransport`.
- **CORS** (`Access-Control-Allow-Origin`) on the public well-known paths, wrapping the auth middleware - the Connect dialog fetches the card from a browser context.

### Step 2 - Configure the IAS OAuth Client

Update the IAS service instance (config only - **never put the client secret in a file**):

```bash
cf update-service <your-ias-instance> -c '{
  "oauth2-configuration": {
    "grant-types": ["authorization_code", "refresh_token"],
    "redirect-uris": ["<REDIRECT_URI_FROM_STEP_3>"],
    "token-policy": { "refresh-parallel": 1 }
  }
}'
```

- Use `authorization_code` + `refresh_token` - **not** `authorization_code_pkce_s256` (PKCE fails here).
- Allow refresh-token issuance (+ `offline_access` scope in Step 3), or the connection dies once the first access token expires.
- Read client ID / secret from the service key: `cf create-service-key <your-ias-instance> copilot-key` then `cf service-key <your-ias-instance> copilot-key`.

### Step 3 - Create the Connection in Copilot Studio

**Agents → Add → Connect Agent2Agent (preview).** Agent card URL `https://<your-route>/.well-known/agent-card.json`; Authentication = **OAuth 2.0 → Authorization Code (Manual)**:

| Field | Value |
|---|---|
| Authorization URL | `https://<ias-tenant>.accounts400.ondemand.com/oauth2/authorize` |
| Token URL | `https://<ias-tenant>.accounts400.ondemand.com/oauth2/token` |
| Refresh URL | *(same as Token URL)* |
| Scopes | `openid email offline_access` |
| Client ID / Secret | from the IAS service key (Step 2) |

> **Register the redirect URI first.** Filling the form generates an agent-specific redirect URL (`https://global.consent.azure-apim.net/redirect/<connector>-<hash>`). Copy it into the IAS client's `redirect-uris` (Step 2) **before** completing consent, or the OAuth callback is rejected.

Click **Create** and complete the IAS login / consent in the popup.

### Step 4 - Add the Connected Agent and Publish

When the connection shows **Connected**, add the agent and **Publish** the Copilot Studio agent.

### Step 5 - Test End-to-End

Ask Copilot to delegate (e.g. *"Ask \<agent\>: How is the weather in Berlin today?"*) - the connected agent's answer flows back into the conversation.

> **Note:** The first call in a fresh/idle session may return a generic `SystemError` with **no** request reaching BTP - that is Power Platform (APIM) warmup, not an auth error. Just resend; do not recreate the connection. A real auth failure instead shows a `401` at BTP (check `cf logs <your-app>`).
