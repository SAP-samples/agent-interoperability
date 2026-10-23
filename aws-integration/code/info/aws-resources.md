# AWS Resources

A running log of every AWS resource created while standing up this POC. Each
entry records the **exact command used** and a **crisp explanation** of what it
creates and why, so the recipe is reproducible by any customer.

## Conventions

| | |
|---|---|
| **Region** | `<region>` |
| **Account** | `<account-id>` |
| **Created with** | AWS CLI v2 (`aws ...`), credentials for IAM user `<iam-user>` |

> All `<...>` below are placeholders — replace them with your own values (most map
> to variables in [env.example](../env.example)). Generated identifiers (runtime
> suffix) are shown as placeholders because AWS assigns them at
> creation time; record the real values it returns in your `.envrc`.

**What we create here:** AgentCore Identity credential provider,
DynamoDB config table, S3 code bucket, IAM execution role, and AgentCore Runtime.

---

## Resources

### Task 4 — AgentCore Identity OAuth2 Credential Provider

**Command**

```bash
aws bedrock-agentcore-control create-oauth2-credential-provider \
  --name "cookbook-sap-ias-obo" \
  --credential-provider-vendor "CustomOauth2" \
  --oauth2-provider-config-input '{
    "customOauth2ProviderConfig": {
      "oauthDiscovery": {
        "discoveryUrl": "https://<tenant>.<ias-landscape>.cloud.sap/.well-known/openid-configuration"
      },
      "clientId": "<ias-client-id>",
      "clientAuthenticationMethod": "PRIVATE_KEY_JWT",
      "onBehalfOfTokenExchangeConfig": { "grantType": "JWT_AUTHORIZATION_GRANT" },
      "privateKeyJwtConfig": {
        "privateKeySource": {
          "kmsKeySource": {
            "kmsKeyArn": "arn:aws:kms:<region>:<account-id>:key/<kms-key-id>"
          }
        },
        "signingAlgorithm": "RS256",
        "additionalHeaderClaims": { "kid": "<kms-key-id>" },
        "additionalPayloadClaims": { "aud": "https://<tenant>.<ias-landscape>.ondemand.com" }
      }
    }
  }' \
  --region <region>
```

**What it does:** Creates an AgentCore Identity OAuth2 credential provider that performs an on-behalf-of (OBO) token exchange with SAP IAS. When the runtime needs to call a SAP agent, it calls `GetResourceOauth2Token` with the user's WorkloadAccessToken — this provider authenticates to IAS using `private_key_jwt` (signed via KMS, no secret in the runtime), exchanges the WAT for a user-scoped IAS token, and returns it as the Bearer on the A2A call.

**Output / identifiers recorded:**

| Field | Value |
|---|---|
| `name` | `cookbook-sap-ias-obo` → `IAS_CREDENTIAL_PROVIDER` in `.envrc` |
| `credentialProviderArn` | `arn:aws:bedrock-agentcore:<region>:<account-id>:token-vault/default/oauth2credentialprovider/cookbook-sap-ias-obo` |
| `status` | `READY` |

> ⚠️ `aud` in `additionalPayloadClaims` must use `.ondemand.com`, **not** `.cloud.sap` — even though the discovery URL uses `.cloud.sap`. See [Troubleshooting in ROADMAP.md](../ROADMAP.md#troubleshooting).

---

### Task 5 — DynamoDB Config Table + Config Row

**Commands**

```bash
# 1. Create the table (partition key = config_id, on-demand billing)
aws dynamodb create-table \
  --table-name "cookbook-agent-runtime-config" \
  --attribute-definitions AttributeName=config_id,AttributeType=S \
  --key-schema AttributeName=config_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region <region>

# 2. Seed the config row (partition key = "current")
aws dynamodb put-item \
  --table-name "cookbook-agent-runtime-config" \
  --item '{
    "config_id": {"S": "current"},
    "payload":   {"S": "{\"version\":1,\"model_id\":\"eu.amazon.nova-lite-v1:0\",\"log_level\":\"INFO\",\"agents\":[{\"id\":\"lms_compliance\",\"name\":\"LMS Compliance Agent\",\"description\":\"SAP Learning (LMS) compliance, including compliance training, certifications, and learning compliance questions and tasks.\",\"gateway_url\":\"https://<sap-region>.access.sapdas.cloud.sap/v1/a2a/<agent-namespace>:agent:<agent-name>:v1/<instance-id>/jsonrpc\",\"aud_urn\":null}],\"ias\":{\"resource_urn\":\"urn:sap:identity:application:provider:name:agent-gateway\",\"credential_provider_name\":\"cookbook-sap-ias-obo\"},\"rate_limit\":{\"window_seconds\":60,\"max_tool_calls\":30}}"},
    "version":   {"N": "1"},
    "updated_at": {"S": "<timestamp>"},
    "updated_by": {"S": "<iam-user>"}
  }' \
  --region <region>
```

**What it does:** Creates the live config table the runtime reads on every request (cached for 30 s). A single row keyed `current` holds the full runtime config as a JSON string — model id, SAP agent list with A2A endpoints, IAS settings, and rate-limit settings. Changing the model or agent list is a DynamoDB edit, no redeploy needed.

**Output / identifiers recorded:**

| Field | Value |
|---|---|
| Table name | `cookbook-agent-runtime-config` → `AGENT_CONFIG_TABLE` in `.envrc` |
| Table ARN | `arn:aws:dynamodb:<region>:<account-id>:table/cookbook-agent-runtime-config` |
| Config key | `current` → `AGENT_CONFIG_KEY` in `.envrc` |
| Billing | On-demand (PAY_PER_REQUEST) |
| Encryption | AWS-managed (default at-rest encryption) |

---

### Task 6 — S3 Bucket (code artifact store)

**Commands**

```bash
# Create the bucket
aws s3api create-bucket \
  --bucket cookbook-agentcore-code-<account-id>-<region> \
  --region <region> \
  --create-bucket-configuration LocationConstraint=<region>

# Block all public access
aws s3api put-public-access-block \
  --bucket cookbook-agentcore-code-<account-id>-<region> \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
  --region <region>

# Upload deployment zip (built by ./build_mcp.sh)
aws s3 cp deployment-mcp.zip \
  s3://cookbook-agentcore-code-<account-id>-<region>/cookbook-agentcore-sap/deployment-mcp-llm.zip \
  --region <region>
```

**What it does:** Creates a private S3 bucket to store the runtime deployment zip. Public access is fully blocked. The runtime execution role is granted `s3:GetObject` on this bucket's prefix only.

**Output / identifiers recorded:**

| Field | Value |
|---|---|
| Bucket | `cookbook-agentcore-code-<account-id>-<region>` → `CODE_BUCKET` |
| S3 key | `cookbook-agentcore-sap/deployment-mcp-llm.zip` → `CODE_S3_KEY` |

---

### Task 6 — IAM Execution Role (Runtime)

**Commands**

```bash
# Trust policy — bedrock-agentcore.amazonaws.com, scoped to this account
aws iam create-role \
  --role-name "cookbook-agentcore-sap-mcp-runtime" \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {"aws:SourceAccount": "<account-id>"},
        "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:<region>:<account-id>:*"}
      }
    }]
  }'

# Inline policy — least-privilege permissions
aws iam put-role-policy \
  --role-name "cookbook-agentcore-sap-mcp-runtime" \
  --policy-name "inline" \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "ReadCodeArtifact",
        "Effect": "Allow",
        "Action": ["s3:GetObject","s3:GetObjectVersion","s3:ListBucket","s3:GetBucketLocation"],
        "Resource": [
          "arn:aws:s3:::cookbook-agentcore-code-<account-id>-<region>",
          "arn:aws:s3:::cookbook-agentcore-code-<account-id>-<region>/cookbook-agentcore-sap/*"
        ]
      },
      {
        "Sid": "RuntimeLogs",
        "Effect": "Allow",
        "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents",
                   "logs:DescribeLogStreams","logs:DescribeLogGroups","logs:PutResourcePolicy"],
        "Resource": "arn:aws:logs:<region>:<account-id>:log-group:/aws/bedrock-agentcore/runtimes/*"
      },
      {
        "Sid": "WorkloadToken",
        "Effect": "Allow",
        "Action": ["bedrock-agentcore:GetWorkloadAccessToken","bedrock-agentcore:GetWorkloadAccessTokenForJWT"],
        "Resource": [
          "arn:aws:bedrock-agentcore:<region>:<account-id>:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:<region>:<account-id>:workload-identity-directory/default/workload-identity/cookbook_agentcore_sap_mcp-*"
        ]
      },
      {
        "Sid": "ResourceOauth2",
        "Effect": "Allow",
        "Action": "bedrock-agentcore:GetResourceOauth2Token",
        "Resource": "*"
      },
      {
        "Sid": "InvokeModel",
        "Effect": "Allow",
        "Action": ["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],
        "Resource": "*"
      },
      {
        "Sid": "ReadConfig",
        "Effect": "Allow",
        "Action": "dynamodb:GetItem",
        "Resource": "arn:aws:dynamodb:<region>:<account-id>:table/cookbook-agent-runtime-config"
      },
      {
        "Sid": "SignIasAssertion",
        "Effect": "Allow",
        "Action": "kms:Sign",
        "Resource": "arn:aws:kms:<region>:<account-id>:key/<kms-key-id>"
      }
    ]
  }'
```

**What it does:** Creates a dedicated least-privilege execution role for the runtime. Each `Sid` is scoped tightly: S3 read limited to the cookbook prefix, WorkloadToken scoped to `cookbook_agentcore_sap_mcp-*` identities only, DynamoDB read limited to the config table, KMS Sign limited to the IAS assertion key.

**Output / identifiers recorded:** `arn:aws:iam::<account-id>:role/cookbook-agentcore-sap-mcp-runtime` → `EXEC_ROLE`

---

### Task 6 — AgentCore Runtime

**Command**

```bash
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name "cookbook_agentcore_sap_mcp" \
  --description "Cookbook POC: MCP proxy agent — Amazon Quick → AgentCore → SAP LMS Compliance via A2A + IAS OBO" \
  --agent-runtime-artifact '{"codeConfiguration":{"code":{"s3":{"bucket":"cookbook-agentcore-code-<account-id>-<region>","prefix":"cookbook-agentcore-sap/deployment-mcp-llm.zip"}},"runtime":"PYTHON_3_13","entryPoint":["mcp_server.py"]}}' \
  --role-arn "arn:aws:iam::<account-id>:role/cookbook-agentcore-sap-mcp-runtime" \
  --network-configuration '{"networkMode":"PUBLIC"}' \
  --protocol-configuration '{"serverProtocol":"MCP"}' \
  --authorizer-configuration '{"customJWTAuthorizer":{"discoveryUrl":"https://cognito-idp.<region>.amazonaws.com/<cognito-user-pool-id>/.well-known/openid-configuration","allowedAudience":["<cognito-app-client-id>"],"allowedScopes":["openid","email","profile"]}}' \
  --request-header-configuration '{"requestHeaderAllowlist":["Authorization"]}' \
  --environment-variables '{"AWS_REGION":"<region>","IAS_PROVIDER_NAME":"cookbook-sap-ias-obo","AGENT_CONFIG_TABLE":"cookbook-agent-runtime-config","AGENT_CONFIG_KEY":"current","CONFIG_CACHE_TTL_SECONDS":"30"}' \
  --region <region>
```

**What it does:** Deploys `mcp_server.py` as a direct-code AgentCore runtime on Python 3.13/ARM64. The JWT authorizer validates inbound Cognito access tokens (issuer = pool, audience = app client, allowed scopes = `openid email profile`). The runtime reads live config from DynamoDB, runs the Bedrock Nova Lite tool loop, and calls SAP Agent Gateway over A2A with the IAS OBO token.

> ⚠️ `allowedScopes` is **required** for the Amazon Quick connector (Task 7). The runtime advertises these scopes during Quick's OAuth discovery; without them Quick falls back to `offline_access` and Cognito rejects the login with `invalid_scope`. The values must match the Cognito app client's `AllowedOAuthScopes`. See [Troubleshooting in ROADMAP.md](../ROADMAP.md#troubleshooting).

**Output / identifiers recorded:**

| Field | Value |
|---|---|
| `agentRuntimeId` | `cookbook_agentcore_sap_mcp-<runtime-suffix>` → `RUNTIME_ID` |
| `agentRuntimeArn` | `arn:aws:bedrock-agentcore:<region>:<account-id>:runtime/cookbook_agentcore_sap_mcp-<runtime-suffix>` → `RUNTIME_ARN` |
| `status` | `READY` |
| CloudWatch logs | `/aws/bedrock-agentcore/runtimes/cookbook_agentcore_sap_mcp-<runtime-suffix>-DEFAULT` |
| MCP endpoint (→ Quick connector / smoke test) | `https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<url-encoded-RUNTIME_ARN>/invocations?qualifier=DEFAULT` → `MCP_ENDPOINT_URL` |

> The MCP endpoint is the runtime's direct `/invocations` URL: URL-encode `RUNTIME_ARN` (`:` → `%3A`, `/` → `%2F`) into the path. `qualifier=DEFAULT` follows the latest runtime version, so redeploys need no connector change. `env.example` derives this automatically.

---

### Task 6 (follow-up) — IAM policy: allow runtime updates (redeploys)

**Command**

```bash
aws iam put-user-policy \
  --user-name "<iam-user>" \
  --policy-name "cookbook-agentcore-runtime-update" \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "UpdateCookbookRuntime",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:UpdateAgentRuntime",
      "Resource": "arn:aws:bedrock-agentcore:<region>:<account-id>:runtime/*"
    }]
  }'
```

**What it does:** Grants the operator's IAM user permission to **update** (redeploy) an AgentCore runtime. The base permissions (via IAM groups) allowed `CreateAgentRuntime` but not `UpdateAgentRuntime`, so `deploy.sh` failed on the second deploy with `AccessDeniedException`. Scoped to runtimes in this account/region only. See [Troubleshooting in ROADMAP.md](../ROADMAP.md#troubleshooting). IAM changes take a few seconds to propagate before the next `update-agent-runtime` succeeds.

**Output / identifiers recorded:** inline policy `cookbook-agentcore-runtime-update` on IAM user `<iam-user>`.
