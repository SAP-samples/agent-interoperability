#!/usr/bin/env bash
# Deploy the LLM-backed MCP runtime ALONGSIDE the existing one.
#
# This is an ADDITIVE deploy. It never touches the live resources:
#   - new runtime name : aws_agentcore_ias_mcp_llm   (separate from aws_agentcore_ias_mcp)
#   - new S3 key       : aws-agentcore-ias/deployment-mcp-llm.zip
#   - config           : reads the shared "current" row (same Nova Lite + agents as the live
#                        system). This is READ-ONLY from the runtime — only the web admin UI ever
#                        writes config — so sharing "current" cannot mutate the live system.
#                        (If you later want to experiment with a different model/agent list,
#                        switch AGENT_CONFIG_KEY to "current-llm" and seed that row — see DEPLOY-LLM.md.)
#   - execution role   : CREATES a NEW dedicated role aws-agentcore-ias-mcp-llm-runtime, scoped to
#                        THIS runtime's workload identity (aws_agentcore_ias_mcp_llm-*). The live
#                        role aws-agentcore-ias-mcp-runtime is NEVER touched. A per-runtime role is
#                        required because the WorkloadAccessToken permission is name-scoped to the
#                        runtime, so the live role would not authorize this runtime's identity.
#
# Rollback: delete this runtime, its gateway target, this role, and the S3 object. Nothing else changes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load configuration. direnv may already have exported it; source directly too so
# this works without direnv installed.
if [ -f "$HERE/.envrc" ]; then
  set -a; . "$HERE/.envrc"; set +a
fi

# Required configuration — fail loudly if anything is unset (copy .envrc.example -> .envrc).
: "${AWS_REGION:?set it in .envrc}"
: "${AWS_ACCOUNT:?set it in .envrc}"
: "${COGNITO_USER_POOL_ID:?set it in .envrc}"
: "${COGNITO_APP_CLIENT_ID:?set it in .envrc}"
: "${IAS_CREDENTIAL_PROVIDER:?set it in .envrc}"
: "${IAS_KMS_KEY_ID:?set it in .envrc}"
: "${AGENT_CONFIG_TABLE:?set it in .envrc}"
: "${AGENT_CONFIG_KEY:?set it in .envrc}"
: "${CONFIG_CACHE_TTL_SECONDS:?set it in .envrc}"
: "${RUNTIME_NAME:?set it in .envrc}"
: "${EXEC_ROLE:?set it in .envrc}"
: "${CODE_BUCKET:?set it in .envrc}"
: "${CODE_S3_KEY:?set it in .envrc}"

# Internal aliases to keep the body below readable.
REGION="$AWS_REGION"
ACCOUNT="$AWS_ACCOUNT"
BUCKET="$CODE_BUCKET"
S3_KEY="$CODE_S3_KEY"
S3_PREFIX="${CODE_S3_KEY%/*}"   # directory part, e.g. aws-agentcore-ias
ROLE="$EXEC_ROLE"
COGNITO_ISSUER="https://cognito-idp.${REGION}.amazonaws.com/${COGNITO_USER_POOL_ID}"
ZIP="$HERE/deployment-mcp.zip"                          # produced by ./build_mcp.sh in THIS folder

[ -f "$ZIP" ] || { echo "missing $ZIP — run ./build_mcp.sh first"; exit 1; }

echo "==> 1/3 ensure NEW dedicated execution role (live role untouched)"
TRUST=$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT"},"ArnLike":{"aws:SourceArn":"arn:aws:bedrock-agentcore:$REGION:$ACCOUNT:*"}}}]}
EOF
)
# Identical to the live role's policy EXCEPT the WorkloadToken resource is scoped to THIS runtime
# name (\${RUNTIME_NAME}-* = aws_agentcore_ias_mcp_llm-*), so the platform can mint this runtime's
# WorkloadAccessToken for the IAS on-behalf-of exchange.
POLICY=$(cat <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"ReadCodeArtifact","Effect":"Allow","Action":["s3:GetObject","s3:GetObjectVersion","s3:ListBucket","s3:GetBucketLocation"],"Resource":["arn:aws:s3:::$BUCKET","arn:aws:s3:::$BUCKET/$S3_PREFIX/*"]},
 {"Sid":"RuntimeLogs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams","logs:DescribeLogGroups","logs:PutResourcePolicy"],"Resource":"arn:aws:logs:$REGION:$ACCOUNT:log-group:/aws/bedrock-agentcore/runtimes/*"},
 {"Sid":"WorkloadToken","Effect":"Allow","Action":["bedrock-agentcore:GetWorkloadAccessToken","bedrock-agentcore:GetWorkloadAccessTokenForJWT"],"Resource":["arn:aws:bedrock-agentcore:$REGION:$ACCOUNT:workload-identity-directory/default","arn:aws:bedrock-agentcore:$REGION:$ACCOUNT:workload-identity-directory/default/workload-identity/${RUNTIME_NAME}-*"]},
 {"Sid":"ResourceOauth2","Effect":"Allow","Action":"bedrock-agentcore:GetResourceOauth2Token","Resource":"*"},
 {"Sid":"InvokeModel","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"},
 {"Sid":"ReadConfig","Effect":"Allow","Action":["dynamodb:GetItem"],"Resource":"arn:aws:dynamodb:$REGION:$ACCOUNT:table/${AGENT_CONFIG_TABLE}"},
 {"Sid":"SignIasAssertion","Effect":"Allow","Action":"kms:Sign","Resource":"arn:aws:kms:$REGION:$ACCOUNT:key/${IAS_KMS_KEY_ID}"}
]}
EOF
)
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "    role exists, updating inline policy"
else
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" >/dev/null
  echo "    role created"
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name inline --policy-document "$POLICY"
ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$ROLE"
echo "    $ROLE_ARN"
sleep 10  # let the new role propagate before AgentCore assumes it

echo "==> 2/3 upload code artifact to NEW key"
aws s3 cp "$ZIP" "s3://$BUCKET/$S3_KEY" --region "$REGION"

echo "==> 3/3 create or update the LLM MCP runtime"
# Same authorizer as the base MCP runtime (Quick sends the Cognito access token via the shim).
AUTHZ="{\"customJWTAuthorizer\":{\"discoveryUrl\":\"${COGNITO_ISSUER}/.well-known/openid-configuration\",\"allowedAudience\":[\"${COGNITO_APP_CLIENT_ID}\"]}}"
# AGENT_CONFIG_KEY=current -> shares the live settings row (read-only here; runtime never writes config).
# Only the vars the runtime actually reads are set here (AWS_REGION, IAS_PROVIDER_NAME fallback,
# AGENT_CONFIG_TABLE/KEY, CONFIG_CACHE_TTL_SECONDS). Cognito issuer/audience live in AUTHZ, not env.
ENV="{\"AWS_REGION\":\"${REGION}\",\"IAS_PROVIDER_NAME\":\"${IAS_CREDENTIAL_PROVIDER}\",\"AGENT_CONFIG_TABLE\":\"${AGENT_CONFIG_TABLE}\",\"AGENT_CONFIG_KEY\":\"${AGENT_CONFIG_KEY}\",\"CONFIG_CACHE_TTL_SECONDS\":\"${CONFIG_CACHE_TTL_SECONDS}\"}"
ARTIFACT="{\"codeConfiguration\":{\"code\":{\"s3\":{\"bucket\":\"$BUCKET\",\"prefix\":\"$S3_KEY\"}},\"runtime\":\"PYTHON_3_13\",\"entryPoint\":[\"mcp_server.py\"]}}"

EXISTING=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeId | [0]" --output text 2>/dev/null)

if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  echo "    updating existing LLM runtime $EXISTING"
  aws bedrock-agentcore-control update-agent-runtime \
    --agent-runtime-id "$EXISTING" \
    --agent-runtime-artifact "$ARTIFACT" \
    --role-arn "$ROLE_ARN" \
    --network-configuration '{"networkMode":"PUBLIC"}' \
    --protocol-configuration '{"serverProtocol":"MCP"}' \
    --authorizer-configuration "$AUTHZ" \
    --request-header-configuration '{"requestHeaderAllowlist":["Authorization"]}' \
    --environment-variables "$ENV" \
    --region "$REGION" --output json | tee "$HERE/.mcp-llm-runtime.json"
  RID="$EXISTING"
else
  echo "    creating new LLM runtime"
  aws bedrock-agentcore-control create-agent-runtime \
    --agent-runtime-name "$RUNTIME_NAME" \
    --description "LLM-backed MCP front for Quick -> AgentService -> IAS -> SAP" \
    --agent-runtime-artifact "$ARTIFACT" \
    --role-arn "$ROLE_ARN" \
    --network-configuration '{"networkMode":"PUBLIC"}' \
    --protocol-configuration '{"serverProtocol":"MCP"}' \
    --authorizer-configuration "$AUTHZ" \
    --request-header-configuration '{"requestHeaderAllowlist":["Authorization"]}' \
    --environment-variables "$ENV" \
    --region "$REGION" --output json | tee "$HERE/.mcp-llm-runtime.json"
  RID=$(python3 -c "import json;print(json.load(open('$HERE/.mcp-llm-runtime.json'))['agentRuntimeId'])")
fi

echo "==> runtime id: $RID — polling for READY"
for i in $(seq 1 30); do
  S=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RID" --region "$REGION" --query status --output text 2>&1)
  echo "    status=$S"
  [ "$S" = "READY" ] && break
  sleep 10
done
echo "done. LLM runtime ARN:"
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RID" --region "$REGION" --query agentRuntimeArn --output text
echo ""
echo "Next: add a gateway target to the EXISTING gateway pointing at this runtime"
echo "      (new target name, e.g. sap-agent-llm) — see DEPLOY-LLM.md step 4."
