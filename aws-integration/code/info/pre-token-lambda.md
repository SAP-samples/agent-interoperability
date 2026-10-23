# Pre-token-generation Lambda

Amazon Quick presents the Cognito **access token** as the Bearer on every MCP
request. The access token is missing claims the downstream chain needs, so a
**V2 pre-token-generation Lambda trigger** adds them:

- access token `email` — SAP IAS resolves the end user by email during the
  on-behalf-of exchange (it becomes the IAS token's `sub`).
- access token `aud` — the runtime authorizer checks `aud` against the
  app client id, and the SAP IAS OBO trust rejects an assertion with no `aud`.
  Cognito only allows `aud` on the access token when it equals the current
  session's app client id, so it is derived from `event.callerContext.clientId` —
  never hardcoded.
- ID token `scope` — a Cognito ID token carries no `scope` claim; this sets one so
  a scope-enforcing authorizer accepts the ID token too.

The trigger is additive and defensive: it only adds claims and never raises, so it
cannot block sign-in for any client.

## Function source

Runtime `python3.13`, handler `lambda_function.handler`.

```python
"""Cognito V2 pre-token-generation trigger.

Adds the claims the downstream chain needs, additively (it only ever adds claims
and never raises, so it cannot block sign-in):

* access token `email` — SAP IAS resolves the end user by email during the
  on-behalf-of exchange (it becomes the IAS token's `sub`).
* access token `aud` — the runtime authorizer checks `aud` against the
  app client id, and the SAP IAS OBO trust rejects an assertion with no `aud`.
  Cognito only allows `aud` on the access token when it equals the current
  session's app client id, so it is derived from event.callerContext.clientId
  (never hardcoded).
* ID token `scope` — a Cognito ID token carries no `scope` claim; this sets one so
  a scope-enforcing authorizer accepts the ID token too.
"""


def handler(event, context):
    try:
        attrs = (event.get("request") or {}).get("userAttributes") or {}
        email = attrs.get("email")
        client_id = (event.get("callerContext") or {}).get("clientId")
        override = {
            "idTokenGeneration": {
                "claimsToAddOrOverride": {"scope": "openid email profile"},
            }
        }
        access_claims = {}
        if email:
            access_claims["email"] = email
        if client_id:
            access_claims["aud"] = client_id
        if access_claims:
            override["accessTokenGeneration"] = {
                "claimsToAddOrOverride": access_claims,
            }
        resp = event.setdefault("response", {})
        resp["claimsAndScopeOverrideDetails"] = override
    except Exception:
        # Never block token issuance; return the event unchanged.
        pass
    return event
```

## Wire it to the user pool

1. Create the Lambda (Python 3.13, handler `lambda_function.handler`) with the
   source above.
2. Grant Cognito permission to invoke it — `lambda:InvokeFunction` from principal
   `cognito-idp.amazonaws.com`, scoped to your user-pool ARN.
3. Set it as the pool's pre-token-generation trigger using the **V2_0** trigger
   version.

> The V2_0 trigger version is required: the `accessTokenGeneration` override used
> here is only available on `LambdaVersion = V2_0`. The classic V1 trigger cannot
> modify access-token claims.
