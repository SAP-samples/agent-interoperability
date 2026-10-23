# AWS AgentCore → SAP BAIP Agent Gateway Interoperability Recipe

**Connect Amazon Quick to SAP BAIP agents with seamless user identity propagation**

This cookbook shows you how to build an MCP proxy agent on AWS AgentCore that forwards user requests
to SAP BAIP agents behind the SAP Agent Gateway, preserving the user's identity end-to-end with no
second login required.

---

## What You'll Build

An **MCP-to-A2A bridge agent** that:

- Sits behind **AWS AgentCore** (Gateway + Runtime + Identity)
- Authenticates users via **Amazon Cognito** (OAuth 2.0)
- Forwards requests to **one SAP BAIP agent** over the **A2A protocol**
- Propagates user identity through **On-Behalf-Of (OBO) token exchange**
- Uses **private_key_jwt** with AWS KMS (no shared secrets)

**User Experience:** Log in once to Amazon Quick via Cognito → ask questions → answers come from
your SAP BAIP agent with full user context and RBAC enforcement.

**Scope:** This cookbook focuses on the **single-agent scenario** (one AWS agent ↔ one SAP BAIP
agent). For multi-agent routing, see
[Advanced: Multi-Agent Routing](#advanced-multi-agent-routing-with-dynamodb).

---

## Architecture Overview

**Diagram:** ![`./images/architecture.png`](./images/architecture.png)

### Components

#### AWS Side

| Component              | Role                                                                            |
| ---------------------- | ------------------------------------------------------------------------------- |
| **Amazon Quick**       | AI chat interface where users interact                                          |
| **Amazon Cognito**     | Authenticates end users, issues JWT tokens with user email                      |
| **AgentCore Gateway**  | MCP front door; validates Cognito JWT, forwards to Runtime with JWT passthrough |
| **AgentCore Runtime**  | Executes MCP server code, mints WorkloadAccessToken (WAT) from Cognito token    |
| **AgentCore Identity** | Performs On-Behalf-Of (OBO) token exchange: WAT → IAS token                     |
| **AWS KMS**            | Signs client assertions for `private_key_jwt` authentication to IAS             |
| **S3**                 | Hosts deployment code zip and published JWKS for IAS                            |

#### SAP side - Business AI Platform (BAIP), SAP-managed

| Component                                     | Role                                                                                                                                                                                     |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **SAP IAS (Identity Authentication Service)** | Accepts OBO exchange, issues user-scoped tokens                                                                                                                                          |
| **SAP Agent Gateway**                         | Validates IAS tokens, routes to SAP BAIP agents via A2A protocol                                                                                                                         |
| **Joule Work** (powered by Joule)             | Authoring/orchestration: _Joule Studio_ (build agents), _Custom Agents_ (IBD), _Joule Orchestrator_ (routes via Skills and A2A), _MCP Builder_, _Tools_ (Data Products, Knowledge Graph) |
| **Managed Runtimes**                          | Host Joule Agents, Custom Agents, MCP Servers; reachable via A2A and MCP (internal)                                                                                                      |
| **Your SAP BAIP Agent**                       | Business agent (Finance, HR, Procurement, etc.) accessible through the Gateway                                                                                                           |

---

## Identity Flow: From User to SAP BAIP

```
User authenticates
  ↓
Amazon Cognito (issues JWT with email claim)
  ↓
AgentCore Gateway (validates JWT, JWT passthrough enabled)
  ↓
AgentCore Runtime (receives JWT, mints WorkloadAccessToken - WAT)
  ↓
AgentCore Identity (OBO exchange: WAT → IAS token)
  ↓  (client assertion signed by KMS, no secrets in runtime)
SAP IAS (validates, issues token with user email as sub, aud = Agent Gateway)
  ↓
SAP Agent Gateway (validates IAS token, enforces RBAC)
  ↓
SAP BAIP Agent (executes with full user context)
```

**Key Point:** The user's identity (email) flows unchanged through every hop, cryptographically
verified at each service boundary. No second login required.

---

## Why This Architecture?

### The Problem: Client Credentials Breaks RBAC

**Without OBO:** If you use client credentials (machine identity), the SAP Agent Gateway sees only
the client ID as `sub`, not the actual user. This causes:

- ❌ `403 RBAC: access denied` - Gateway doesn't know WHO is calling
- ❌ No audit trail of actual user actions
- ❌ No per-user authorization
- ❌ Compliance violations (all actions appear from one service account)

### The Solution: On-Behalf-Of Token Exchange

**With OBO:** Token exchange preserves user identity through the entire chain:

| Token                         | Purpose                                                | Contains                                                            |
| ----------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------- |
| **Cognito JWT**               | User authentication into Quick/MCP connector           | `email`, `sub` (Cognito user ID), `aud` (client ID)                 |
| **WorkloadAccessToken (WAT)** | Opaque platform credential preserving user identity    | Opaque to agent code, minted by AgentCore Runtime                   |
| **IAS Token**                 | Result of OBO exchange, presented to SAP Agent Gateway | `sub` = user email, `aud` = Agent Gateway, `ias_apis: ["external"]` |

✅ **Result:** SAP Agent Gateway accepts the request because it knows the real user and can enforce
per-user RBAC.

### Why `private_key_jwt` Authentication?

| Client Secret (traditional)              | Private Key JWT (KMS)                    |
| ---------------------------------------- | ---------------------------------------- |
| ❌ Shared secret stored in code          | ✅ Private key never leaves AWS          |
| ❌ Secret rotation requires code changes | ✅ Key rotation = publish new JWKS entry |
| ❌ Secret can be extracted from runtime  | ✅ Signing happens in KMS hardware       |
| ❌ Same secret for all environments      | ✅ Different keys per environment        |

**Security Benefits:**

- Private key material **never** leaves KMS
- Client assertions are **single-use** (include `jti`, `exp`)
- JWKS can be rotated without downtime
- Meets enterprise security standards (no shared secrets in agent runtime)

---

## Prerequisites

### AWS Account Requirements

- **AWS Bedrock AgentCore** access (Gateway, Runtime, Identity APIs)
- **Amazon Cognito** user pool creation permissions
- **AWS KMS** key creation and signing permissions
- **S3** bucket creation (for JWKS publication and code deployment)
- **IAM** role and policy management
- **CloudWatch Logs** access for observability
- **Amazon Quick Suite** subscription (Author tier or higher)

### SAP Requirements

- **SAP IAS Tenant** with admin access
- **SAP BTP** global account with **Formation** setup (for consuming Agent Gateway)
- **SAP Agent Gateway** access with target agent details:
  - Base URL: `https://<region>.access.sapdas.cloud.sap`
  - Agent ORD ID: `customer.build:agent:<name>.<name>:v1`
  - System/CAD artifact ID from Formation setup
- **Resource URN** for Agent Gateway (e.g.,
  `urn:sap:identity:application:provider:name:agent-gateway`)

### User Requirements

- Test user must exist in **both Cognito and SAP IAS**
- Email must match exactly between systems
- User must be in the agent's **RBAC group** in IAS
- User must be authorized via Formation/Systems in SAP for Me

### Tools & Environment

- **AWS CLI** configured for your account/region
- **Python 3.13+** (for runtime code, matches AgentCore Python runtime)
- **bash** (for deployment scripts)
- **curl/httpx** (for testing)

---

## Implementation Guide

**The setup is organized into 9 core tasks for single-agent deployment:**

### Phase 1: Identity Foundation (Tasks 0-1)

- **Task 0:** Create Amazon Cognito user pool with Pre-Token Generation Lambda
- **Task 1:** Create KMS signing key and publish JWKS

### Phase 2: SAP Configuration (Tasks 2-3)

- **Task 2:** Confirm SAP agent behind Agent Gateway
- **Task 3:** Provision AWS agent as trusted application in SAP IAS

### Phase 3: AWS AgentCore Setup (Tasks 4-7)

- **Task 4:** Create AgentCore Identity credential provider
- **Task 5:** Build and deploy the runtime (with environment variable config)
- **Task 6:** Create AgentCore Gateway
- **Task 7:** Add gateway target

### Phase 4: Amazon Quick Integration (Task 8-9)

- **Task 8:** Register Amazon Quick MCP connector
- **Task 9:** Validate end-to-end

**Detailed step-by-step instructions with commands, configurations, and troubleshooting are provided
in the sections below.**

> **Note:** The reference implementation in `code/` includes optional DynamoDB support for
> multi-agent routing. For single-agent deployment, use environment variables for configuration. See
> [Advanced: Multi-Agent Routing](#advanced-multi-agent-routing-with-dynamodb) for the DynamoDB
> approach.

---

## Code Structure

**All implementation code is in [`./code/`](./code/):**

```
code/
├── mcp_server.py              # MCP front door: exposes sap_assistant tool
├── runtime/
│   ├── agent.py               # Handles A2A communication with SAP BAIP agent
│   ├── ias.py                 # IAS OBO token exchange via AgentCore Identity
│   ├── sap_gateway.py         # Outbound A2A client (JSON-RPC)
│   ├── config.py              # RuntimeConfig schema validation
│   ├── dynamic_config.py      # (Optional) Reads live config from DynamoDB
│   ├── rate_limit.py          # Per-user rate limiting
│   └── resume_token.py        # Multi-turn conversation support
├── tests/
│   ├── unit/test_runtime.py   # Unit tests
│   └── integration/run_once.py# E2E smoke test (Cognito login → MCP call)
├── build_mcp.sh               # Package deployment zip
├── deploy.sh                  # Deploy runtime to AgentCore
├── env.example                # Environment variables template
├── requirements.txt           # Runtime dependencies
├── README.md                  # Code overview and usage
└── docs/
    ├── agent-config.md        # DynamoDB config schema (for multi-agent)
    └── pre-token-lambda.md    # Pre-token generation Lambda code
```

### Configuration Approach

**For single-agent deployment (this cookbook's focus):**

Configure the SAP agent details via **environment variables** in `env.example`:

- `SAP_AGENT_URL` - A2A JSON-RPC endpoint of your SAP BAIP agent
- `IAS_RESOURCE_URN` - Agent Gateway resource URN
- `IAS_CREDENTIAL_PROVIDER` - AgentCore Identity provider name

**For multi-agent routing (advanced):**

The code supports dynamic configuration via DynamoDB for scenarios where you need:

- Multiple SAP BAIP agents with runtime routing
- Bedrock model selection without redeployment
- Config changes without code redeployment

See [Advanced: Multi-Agent Routing](#advanced-multi-agent-routing-with-dynamodb) for details.

### Key Implementation Details

**OBO Token Exchange** (`runtime/ias.py`):

```python
def get_ias_token(wat: str, credential_provider: str, resource_urn: str) -> str:
    """
    Exchange WorkloadAccessToken for IAS token via AgentCore Identity.

    Args:
        wat: Opaque token from AgentCore Runtime
        credential_provider: Name of IAS credential provider (Task 4)
        resource_urn: SAP Agent Gateway resource URN (Task 2)

    Returns:
        IAS access token with user email as sub, aud = Agent Gateway
    """
    client = boto3.client("bedrock-agentcore")
    response = client.get_resource_oauth2_token(
        workloadIdentityToken=wat,
        credentialProvider=credential_provider,
        oauth2Flow="ON_BEHALF_OF_TOKEN_EXCHANGE",
        resource=resource_urn,
    )
    return response["accessToken"]
```

**A2A JSON-RPC Call** (`runtime/sap_gateway.py`):

```python
def call_sap_agent(message: str, ias_token: str, gateway_url: str) -> str:
    """
    Call SAP BAIP agent via A2A JSON-RPC protocol.

    Args:
        message: User's question
        ias_token: IAS access token from OBO exchange
        gateway_url: SAP Agent Gateway A2A endpoint

    Returns:
        Agent's response message
    """
    headers = {
        "Authorization": f"Bearer {ias_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "jsonrpc": "2.0",
        "method": "sendMessage",
        "params": {"message": message},
        "id": 1
    }

    response = httpx.post(gateway_url, headers=headers, json=payload)
    return response.json()["result"]["message"]
```

---

## Key Concepts Deep-Dive

### WorkloadAccessToken (WAT)

**What it is:**

- Opaque token minted by AgentCore Runtime from Cognito access token
- Contains user identity preserved from Cognito JWT
- Only valid within AgentCore ecosystem
- Short-lived, bound to the request context
- Automatically injected into agent code as request header by Runtime hosting layer

**How it's used:**

1. AgentCore Gateway validates incoming Cognito JWT
2. Gateway forwards JWT to Runtime (JWT passthrough)
3. Runtime calls `GetWorkloadAccessTokenForJWT` → mints WAT
4. Runtime injects WAT as header before executing your agent code
5. Your code extracts WAT, passes it to AgentCore Identity for OBO exchange

**Why it exists:** Decouples the "what user identity to exchange" from "how to authenticate to the
target OAuth server". Your agent code doesn't see the original Cognito JWT; it only handles the
opaque WAT.

### On-Behalf-Of (OBO) Token Exchange

**RFC 8693** token exchange flow that preserves user identity:

```http
POST https://<ias-tenant>.accounts400.ondemand.com/oauth2/token

grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
&assertion=<WAT>
&resource=urn:sap:identity:application:provider:name:agent-gateway
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion=<signed-jwt-by-kms>
```

**What happens:**

1. **`assertion` (WAT)** identifies the **user** (contains email from Cognito)
2. **`client_assertion` (signed by KMS)** authenticates the **client application** (your AWS agent)
3. **`resource`** specifies the target audience (SAP Agent Gateway)
4. IAS validates both assertions simultaneously:
   - Validates client assertion signature via published JWKS
   - Resolves user by email from WAT
   - Checks user exists in IAS and is authorized
5. IAS issues token with `sub` = user email, `aud` = Agent Gateway

**Result:** Token has user identity + correct audience for gateway, ready for A2A call.

### JWKS (JSON Web Key Set)

**What it is:**

- Published public key that IAS uses to verify client assertion signatures
- JSON document at publicly accessible HTTPS URL (typically S3)
- Contains RSA public key components (`n`, `e`, `kty`, `use`, `alg`)
- Must include `kid` (key ID) matching KMS key ID

**Example:**

```json
{
  "keys": [
    {
      "kty": "RSA",
      "use": "sig",
      "kid": "1e5e5cf2-9a6c-472e-9a53-7f7d2c9e2cc2",
      "alg": "RS256",
      "n": "sAAvp-l2iJSXt9...",
      "e": "AQAB"
    }
  ]
}
```

**Why it matters:**

- IAS cannot verify client assertion signatures without the public key
- URL must be reachable by IAS (public S3, not VPC-only)
- `kid` is how IAS looks up the right key from JWKS
- Multiple keys can coexist during rotation (publish new key, switch gradually)

**Generation:** Export public key from KMS, convert to JWK format, publish to S3 with public read
access.

### Resource URN

**Format:** `urn:sap:identity:application:provider:name:agent-gateway`

**Why it matters:**

- Only tokens minted with this `resource` parameter have the correct `aud` for Agent Gateway
- A plain OAuth token (even from IAS) doesn't have this specific `aud`
- The gateway rejects tokens without matching `aud` claim
- This is enforced by IAS during token issuance - you can't self-issue this `aud`
- Ensures tokens are purpose-bound (can't reuse a token meant for another service)

**Where it comes from:** SAP provides this URN as part of Formation setup / System configuration in
SAP for Me.

### `ias_apis` Claim

**Values:**

- `["external"]` - Customer/partner traffic (what you get with OBO exchange)
- `["sap-internal"]` - UCL-managed traffic (can't self-issue, requires SAP-internal flow)

**Why it matters:**

- The gateway uses this to distinguish traffic types and apply appropriate routing/rate limits
- `external` is the hard boundary - you cannot cross it without SAP-internal infrastructure
- Determines which rate limits, routes, and monitoring apply
- Customer agents always get `["external"]`, ensuring proper isolation

---

## Security Model

### Authentication Chain

Every hop validates identity independently:

| Hop                | Validator                        | What it validates                                                    |
| ------------------ | -------------------------------- | -------------------------------------------------------------------- |
| Quick → Gateway    | AgentCore Gateway CUSTOM_JWT     | Cognito JWT: signature (JWKS), `iss`, `aud`, `exp`, `nbf`            |
| Gateway → Runtime  | AgentCore Runtime JWT authorizer | Same validation on forwarded JWT (defense in depth)                  |
| Runtime → Identity | AgentCore Identity               | WAT validity (internal to AgentCore platform)                        |
| Identity → IAS     | SAP IAS                          | Client assertion signature (JWKS), user assertion (email resolution) |
| IAS → Gateway      | SAP Agent Gateway                | IAS token: signature, `iss`, `aud`, `exp`, `sub`, `ias_apis`         |

### Why Each Token is Needed

| Token                             | Without it...                                                         |
| --------------------------------- | --------------------------------------------------------------------- |
| **Cognito JWT**                   | Quick can't authenticate the user; no identity enters AWS             |
| **WAT**                           | AgentCore Identity can't know which user to exchange for              |
| **Client Assertion (KMS-signed)** | IAS doesn't know the calling application is authorized                |
| **IAS Token**                     | Agent Gateway rejects request (wrong `aud`) or can't see user (`sub`) |

### Trust Boundaries

1. **User → Cognito:** Password/federated authentication (OAuth 2.0 authorization code + PKCE)
2. **Cognito → AgentCore:** Cognito JWKS signature verification at Gateway + Runtime
3. **AgentCore → IAS:** KMS-signed client assertion + WAT validation by AgentCore Identity
4. **IAS → Agent Gateway:** IAS JWKS signature verification at Gateway

Each boundary has **cryptographic proof** - no trust-on-first-use, no shared secrets (replaced by
asymmetric KMS signing).

---

## Configuration Management

### Deploy-Time Configuration (`code/env.example`)

Environment variables used by build/deploy scripts:

| Variable                    | Purpose                                      | Example                                                    |
| --------------------------- | -------------------------------------------- | ---------------------------------------------------------- |
| `AWS_REGION`, `AWS_ACCOUNT` | Target deployment region and account         | `eu-central-1`, `782110148236`                             |
| `COGNITO_USER_POOL_ID`      | User pool for JWT validation                 | `eu-central-1_abc123`                                      |
| `COGNITO_APP_CLIENT_ID`     | App client ID (becomes `aud` in tokens)      | `6qreu97ifcpa1uih24lqnfiobh`                               |
| `COGNITO_DOMAIN`            | Hosted UI domain for OAuth                   | `my-app.auth.eu-central-1.amazoncognito.com`               |
| `IAS_CREDENTIAL_PROVIDER`   | AgentCore Identity provider name             | `sap-ias-provider`                                         |
| `IAS_KMS_KEY_ID`            | KMS signing key UUID                         | `1e5e5cf2-9a6c-472e-9a53-7f7d2c9e2cc2`                     |
| `IAS_RESOURCE_URN`          | SAP Agent Gateway resource URN               | `urn:sap:identity:application:provider:name:agent-gateway` |
| `SAP_AGENT_URL`             | A2A JSON-RPC endpoint of your SAP BAIP agent | `https://eu10.access.sapdas.cloud.sap/.../jsonrpc`         |
| `RUNTIME_NAME`              | AgentCore runtime name                       | `sap-agent-runtime`                                        |
| `CODE_BUCKET`               | S3 bucket for deployment zip                 | `my-agent-deployments`                                     |

> **Note:** For single-agent deployment, all configuration is in environment variables. No DynamoDB
> required.

---

## Deployment Workflow

### Initial Deployment

1. **Prerequisites:** Complete the setup tasks from the
   [Implementation Guide](#implementation-guide) section above
2. **Build:** `cd code && ./build_mcp.sh` (packages runtime into deployment zip)
3. **Deploy:** `./deploy.sh` (uploads to S3, creates/updates AgentCore Runtime)
4. **Gateway Setup:** Create Gateway and add target (see tasks 6-7)
5. **Quick Integration:** Register MCP connector (see task 8)
6. **Test:** `python tests/integration/run_once.py "test question"`

### Updates and Iterations

**Code changes** (agent logic, bug fixes):

```bash
./build_mcp.sh
./deploy.sh
```

No gateway or connector changes needed. The gateway target points to `qualifier=DEFAULT`, which
auto-follows the latest runtime version.

**Config changes** (SAP agent URL, IAS settings):

- Update environment variables in `.envrc`
- Redeploy with `./deploy.sh`

**For zero-downtime config updates**, consider the advanced multi-agent approach with DynamoDB.

**Key rotation:**

1. Create new KMS key
2. Export public key, add to JWKS with new `kid`
3. Update JWKS URL (both keys present)
4. Update credential provider to use new key ARN
5. Wait for token TTL (1 hour)
6. Remove old key from JWKS

---

## Testing and Validation

### Unit Tests

```bash
cd code
pip install -r requirements-dev.txt
python -m pytest tests/unit
```

Tests cover: config validation, rate limiting, token caching, routing logic.

### Integration Test (E2E Smoke Test)

```bash
cd code
python tests/integration/run_once.py "show me my compliance training status"
```

**What it does:**

1. Performs Cognito OAuth login (opens browser for authorization code flow)
2. Exchanges code for Cognito access token
3. Calls AgentCore Gateway MCP endpoint with token
4. Exercises full chain: Gateway → Runtime → WAT → OBO exchange → IAS token → SAP Agent
5. Prints response and timing

**Expected output:**

```
✓ Cognito login succeeded
✓ MCP initialize succeeded
✓ Tools listed: ['sap_assistant']
✓ sap_assistant call succeeded
Response: "Your latest compliance training..."
Total time: 2.3s
```

### Manual Testing via Amazon Quick

1. Open Amazon Quick chat interface
2. Type a question related to your SAP agent
3. First time: Cognito login popup appears
4. After login: connector session is cached, no more popups
5. Agent response appears in chat

**Check CloudWatch logs:**

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --follow --region eu-central-1
```

---

## Troubleshooting

### Common Issues

| Symptom                                | Cause                             | Fix                                                                   |
| -------------------------------------- | --------------------------------- | --------------------------------------------------------------------- |
| `missing_authorization` error          | Quick connector not sending token | Verify connector OAuth config, check Cognito callback URL             |
| `missing_workload_token` error         | WAT not in event headers          | Check Gateway has JWT_PASSTHROUGH enabled, Runtime has JWT authorizer |
| `ias_exchange_failed: invalid_grant`   | User not found in IAS             | Create user in IAS with matching email, add to RBAC group             |
| `ias_exchange_failed: invalid_client`  | KMS signature invalid             | Check JWKS `kid` matches KMS key ID, verify JWKS URL is public        |
| `403 RBAC: access denied` from Gateway | User not authorized               | Add user to agent RBAC group in IAS, verify Formation setup           |
| `401 Unauthorized` from Gateway        | IAS token invalid                 | Check `resource` URN is correct, verify token has correct `aud`       |
| JWKS fetch fails                       | S3 not public                     | Apply bucket policy for public GetObject on jwks.json                 |
| Cognito token missing `email`          | Lambda not attached               | Add Pre-Token Lambda to pool, verify it sets `email` claim            |
| Cognito token missing `aud`            | Lambda not setting `aud`          | Update Lambda to set `aud` = `COGNITO_APP_CLIENT_ID`                  |
| `tools/list` shows no tools            | Gateway/Runtime not connected     | Re-check Tasks 6-8, verify Runtime is READY state                     |

### Validation Steps

**1. Check Cognito JWT has required claims:**

Get a token from Quick connector (or use `run_once.py`), decode at jwt.io:

```json
{
  "sub": "<cognito-user-id>",
  "email": "test.user@sap.com",
  "aud": "<cognito-client-id>",
  "iss": "https://cognito-idp.eu-central-1.amazonaws.com/<pool-id>",
  "exp": 1729612345
}
```

**2. Test JWKS is publicly accessible:**

```bash
curl https://your-bucket.s3.eu-central-1.amazonaws.com/jwks.json
# Should return JSON with "keys" array
```

**3. Check AgentCore Identity credential provider:**

```bash
aws bedrock-agentcore-control describe-credential-provider \
  --identifier sap-ias-provider \
  --region eu-central-1
# Should show ACTIVE state
```

**4. Test IAS token exchange manually:**

Get a WAT from CloudWatch logs, use boto3 to call `get_resource_oauth2_token`, decode resulting IAS
token:

```json
{
  "sub": "test.user@sap.com",
  "aud": "agent-gateway",
  "ias_apis": ["external"],
  "iss": "https://<tenant>.accounts.ondemand.com",
  "exp": 1729612345
}
```

**5. Check Runtime logs for errors:**

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --follow --region eu-central-1
```

Look for: OBO exchange calls, Bedrock tool loop, A2A JSON-RPC requests, errors.

---

## Production Readiness Checklist

### Security

- [ ] Rotate test credentials to production values
- [ ] Enable VPC endpoints for private AgentCore deployment
- [ ] Set up CloudWatch alarms for 4xx/5xx errors, OBO exchange failures
- [ ] Configure WAF rules if Gateway is internet-facing
- [ ] Enable CloudTrail for audit logging of AgentCore API calls
- [ ] Review IAM policies for least privilege (Runtime execution role)
- [ ] Set up KMS key rotation policy (annual recommended)
- [ ] Implement secret scanning in CI/CD (no client_secret leaks)

### Identity

- [ ] Configure Cognito → IAS federation for corporate IdP (single corporate login)
- [ ] Set up user provisioning (SCIM from corporate IdP to IAS)
- [ ] Document RBAC group membership requirements for each SAP agent
- [ ] Test with multiple user personas (admin, regular user, no access)
- [ ] Validate token expiration handling (refresh token flow in Quick)
- [ ] Test cross-region user access patterns
- [ ] Document user offboarding process (Cognito + IAS)

### Monitoring & Observability

- [ ] Set up CloudWatch dashboard (requests/min, latency, errors, token cache hit rate)
- [ ] Configure X-Ray tracing for AgentCore (trace ID propagation)
- [ ] Alert on OBO exchange failures (threshold: >5% error rate)
- [ ] Monitor KMS signing latency (alert if p99 > 200ms)
- [ ] Track DynamoDB config read latency (cache effectiveness)
- [ ] Set up A2A call latency monitoring (SAP Gateway response times)
- [ ] Create runbook for common failure modes

### Resilience & Performance

- [ ] Load test with expected concurrent users
- [ ] Validate token caching reduces OBO exchange calls (target: 90%+ cache hit rate)
- [ ] Test Runtime failover (deploy new version, ensure zero downtime)
- [ ] Configure DynamoDB auto-scaling if using provisioned capacity
- [ ] Set up S3 versioning for deployment artifacts
- [ ] Test Gateway rate limiting behavior (exceeding limits)
- [ ] Document RTO/RPO for disaster recovery

### Documentation

- [ ] Document all resource identifiers (ARNs, IDs, URNs) in runbook
- [ ] Create architecture diagram with actual ARNs/IDs for this deployment
- [ ] Document escalation procedures (AWS support, SAP support contacts)
- [ ] Maintain change log for config updates
- [ ] Version control all IaC (Terraform/CloudFormation if used)

### Compliance

- [ ] Data residency verification (AWS region, IAS tenant region)
- [ ] GDPR compliance check (user data handling, right to be forgotten)
- [ ] SOC 2 controls documentation (access logging, encryption at rest/in transit)
- [ ] Security review sign-off from InfoSec team
- [ ] Privacy impact assessment for user email propagation

---

## Advanced Topics

### Token Caching Strategy

IAS tokens are valid for 1 hour. Cache them to reduce OBO exchange calls:

```python
from functools import lru_cache
import time

@lru_cache(maxsize=100)
def get_cached_ias_token(user_email: str, issue_time: int) -> str:
    """
    Cache IAS tokens per user.

    Args:
        user_email: User's email (from WAT/Cognito)
        issue_time: Rounded to nearest 5 minutes (cache key)

    Returns:
        Cached or freshly exchanged IAS token
    """
    # issue_time rounds to nearest 5 minutes
    # Cache expires when rounded time changes (every 5 min)
    # Actual token lifetime is 1 hour
    return get_ias_token_fresh(user_email)

# Usage in runtime
issue_time = int(time.time() / 300) * 300  # Round to 5-min bucket
token = get_cached_ias_token(user_email, issue_time)
```

**Cache hit rate target:** 90%+ (most users make multiple requests within 5 minutes)

### Zero-Downtime KMS Key Rotation

1. Create new KMS key (RSA_2048, SIGN_VERIFY)
2. Export new public key, convert to JWK format
3. Add to JWKS with new `kid` (both keys now present)
4. Update S3 JWKS URL (atomic operation)
5. Update AgentCore Identity credential provider to use new `kmsKeyArn`
6. Wait 1 hour (old IAS tokens expire)
7. Remove old key from JWKS
8. (Optional) Disable old KMS key after 7 days

**No downtime:** IAS can verify tokens signed with either key during transition.

### Multi-Region Deployment

**Active-Passive:**

- Deploy AgentCore Runtime in multiple regions (us-east-1, eu-central-1)
- Use Route 53 health checks + failover routing to Gateway URLs
- Replicate JWKS to S3 buckets in each region
- Single DynamoDB global table for config (multi-region replication)
- Single KMS key per region (JWKS has entries for both)

**Active-Active:**

- Same as above, plus:
- Route users to nearest region (latency-based routing)
- Cognito user pool per region (or single pool with replication)
- Monitor cross-region latency for OBO exchanges

**Use CloudFormation StackSets for consistent deployment across regions.**

### Multi-Agent Routing with DynamoDB

**When you need it:**

- Multiple SAP BAIP agents with dynamic routing based on user question
- Change agent list without redeployment
- Different agents for different user groups/tenants
- Bedrock-powered intelligent routing ("ask finance" vs "ask HR")

**How it works:**

The reference implementation in `code/` supports DynamoDB-backed configuration for multi-agent
scenarios:

#### 1. DynamoDB Configuration

Create a DynamoDB table with config structure:

```json
{
  "config_id": "prod-config",
  "payload": {
    "model_id": "eu.amazon.nova-lite-v1:0",
    "agents": [
      {
        "id": "sap-finance",
        "name": "SAP Finance Agent",
        "description": "Handles finance, budgets, expenses",
        "gateway_url": "https://eu10.access.sapdas.cloud.sap/.../finance/jsonrpc"
      },
      {
        "id": "sap-hr",
        "name": "SAP HR Agent",
        "description": "Handles leave, payroll, training",
        "gateway_url": "https://eu10.access.sapdas.cloud.sap/.../hr/jsonrpc"
      }
    ],
    "ias": {
      "resource_urn": "urn:sap:identity:application:provider:name:agent-gateway",
      "credential_provider_name": "sap-ias-provider"
    },
    "rate_limit": {
      "window_seconds": 60,
      "max_tool_calls": 10
    }
  }
}
```

#### 2. Bedrock-Powered Routing

The runtime (`runtime/agent.py`) dynamically generates one Bedrock tool per agent:

```python
# User asks: "What's my leave balance?"
# Runtime generates tools: ask_sap_finance(), ask_sap_hr()
# Bedrock reads descriptions, selects ask_sap_hr()
# Runtime performs OBO exchange, calls HR agent via A2A
```

**Benefits:**

- Add agents by editing DynamoDB (no redeploy)
- Runtime picks up changes within 30 seconds (cache TTL)
- Bedrock handles routing intelligence
- Single MCP connector exposes all agents

#### 3. Setup Steps

**Follow standard setup Tasks 0-4, then:**

**Task 5 (DynamoDB):** Create table and seed config

```bash
aws dynamodb create-table \
  --table-name agent-runtime-config \
  --attribute-definitions AttributeName=config_id,AttributeType=S \
  --key-schema AttributeName=config_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region eu-central-1

aws dynamodb put-item \
  --table-name agent-runtime-config \
  --item file://config.json \
  --region eu-central-1
```

**Task 6 (Deploy):** Set environment variables

```bash
export AGENT_CONFIG_TABLE=agent-runtime-config
export AGENT_CONFIG_KEY=prod-config
export CONFIG_CACHE_TTL_SECONDS=30
./build_mcp.sh
./deploy.sh
```

**Continue with Tasks 7-9 (Gateway, Quick integration).**

#### 4. Adding Agents

Edit DynamoDB row, add agent to `agents[]` array:

```json
{
  "id": "sap-procurement",
  "name": "SAP Procurement Agent",
  "description": "Handles purchase orders, suppliers, contracts",
  "gateway_url": "https://eu10.access.sapdas.cloud.sap/.../procurement/jsonrpc"
}
```

Runtime picks up change within 30 seconds. No redeploy needed.

**See [`code/docs/agent-config.md`](./code/docs/agent-config.md)** for full schema and examples.

---

### Corporate IdP Federation

Instead of direct Cognito users, federate via SAP IAS as corporate IdP:

1. Create IAS OIDC application for Cognito (Task 0 extended)
2. Configure Cognito Corporate Identity Provider (IAS as IdP)
3. Users authenticate at IAS → Cognito mints federated token
4. OBO exchange still works (Cognito → IAS, but now with federated user identity)
5. **User experience:** Single corporate login for both Quick and SAP systems

**Benefits:**

- One user directory (IAS) for both AWS and SAP sides
- Simplified user provisioning (SCIM from corporate IdP to IAS)
- Consistent RBAC model across platforms

---

## Next Steps

1. **Follow the setup guide:** Work through the [Implementation Guide](#implementation-guide)
   section step-by-step
2. **Understand the code:** Review [`code/README.md`](./code/README.md) and key modules
   (`runtime/agent.py`, `runtime/ias.py`)
3. **Deploy to dev environment:** Complete all setup tasks with test credentials
4. **Test end-to-end:** Use `run_once.py` and Amazon Quick to validate
5. **Production readiness:** Work through the checklist above
6. **Scale (optional):** Implement
   [multi-agent routing](#advanced-multi-agent-routing-with-dynamodb) if needed
7. **Monitor and optimize:** Set up CloudWatch dashboards, optimize token caching

---

## References & Resources

- **Reference Implementation and recipe:** TODO: (Link to Praveen & Aravind's implementation repo )
- **AWS AgentCore Documentation:**
  https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html
- **RFC 8693 (Token Exchange):** https://datatracker.ietf.org/doc/html/rfc8693
- **RFC 7523 (JWT Bearer Grants):** https://datatracker.ietf.org/doc/html/rfc7523
- **SAP IAS OAuth Documentation:** https://help.sap.com/docs/identity-authentication
- **SAP Agent Gateway Documentation:** https://help.sap.com/docs/CROSS_DOMAIN_AGENTS
- **SAP Agent Gateway Setup (IAS Application Registration):**
  https://help.sap.com/docs/CROSS_DOMAIN_AGENTS/85abb1b3c5734138826f02d079b04975/f6f218644e704192b3a9b7af0f5794b4.html?state=DRAFT&ai=true
- **SAP A2A Protocol Specification:** https://github.com/SAP/agent-to-agent-protocol
- **Amazon Quick MCP Integration:**
  https://docs.aws.amazon.com/quick/latest/userguide/mcp-integration.html
- **Original Implementation:**
  https://github.tools.sap/OCTO/ag-gated-access/tree/docs/usecase-google-agent-a2a/code/aws-agent

---
