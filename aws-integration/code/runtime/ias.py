"""AgentCore Identity-managed IAS token retrieval with in-memory caching.

This is the on-behalf-of (OBO) step of the identity chain. The runtime never holds
an IAS client secret or signs an assertion itself: it hands AgentCore Identity the
user's WorkloadAccessToken (WAT) and asks it to exchange that for a user-scoped IAS
token whose audience is SAP Agent Gateway. AgentCore Identity performs the
`private_key_jwt` client authentication (KMS-signed) against IAS behind the scenes.

Tokens are cached per (user, resource, config fingerprint) until shortly before
expiry, so a burst of tool calls for the same user reuses one IAS token.
"""

from __future__ import annotations

import base64
import collections
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import boto3

from runtime.config import RuntimeConfig


class IasExchangeError(RuntimeError):
    pass


def _jwt_exp(token: str) -> float | None:
    # Read `exp` from the JWT payload to drive cache expiry. Unverified decode:
    # the token is validated by its consumer (SAP Agent Gateway), not here.
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded)).get("exp")
        return float(value) if isinstance(value, (int, float)) else None
    except Exception:
        return None


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: float


class IasTokenProvider:
    """Retrieve IAS OBO tokens through AgentCore Identity."""

    def __init__(self, max_users: int = 256, refresh_leeway: int = 30) -> None:
        self._max = max_users
        self._leeway = refresh_leeway
        self._tokens: collections.OrderedDict[
            tuple[str, str, str], _CachedToken
        ] = collections.OrderedDict()
        self._identity: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _client(self, region: str) -> Any:
        if region not in self._identity:
            self._identity[region] = boto3.client("bedrock-agentcore", region_name=region)
        return self._identity[region]

    def get_token(
        self,
        *,
        user_email: str,
        workload_access_token: str,
        resource_urn: str,
        config: RuntimeConfig,
        region: str,
    ) -> str:
        if not workload_access_token:
            raise IasExchangeError("AgentCore workload identity token is missing")
        # Fingerprint is part of the key so a config change (e.g. a new credential
        # provider or resource URN) can't serve a stale token from the cache.
        key = (user_email, resource_urn, config.fingerprint())
        now = time.time()
        with self._lock:
            cached = self._tokens.get(key)
            if cached and cached.expires_at > now + self._leeway:
                self._tokens.move_to_end(key)
                return cached.token
        try:
            # The OBO exchange: swap the workload token for a user-scoped IAS token.
            # `resources` sets the audience (SAP Agent Gateway's resource URN) and
            # token_format=jwt asks IAS for a JWT rather than an opaque token.
            payload = self._client(region).get_resource_oauth2_token(
                workloadIdentityToken=workload_access_token,
                resourceCredentialProviderName=config.ias.credential_provider_name,
                oauth2Flow="ON_BEHALF_OF_TOKEN_EXCHANGE",
                scopes=["openid"],
                resources=[resource_urn],
                customParameters={"token_format": "jwt"},
            )
        except Exception as exc:
            details = getattr(exc, "response", {}).get("Error", {})
            message = str(details.get("Message", type(exc).__name__))[:500]
            raise IasExchangeError(f"AgentCore Identity IAS exchange failed: {message}") from exc
        token = payload.get("accessToken")
        if not isinstance(token, str) or not token:
            raise IasExchangeError("AgentCore Identity returned no IAS access token")
        expires_at = _jwt_exp(token) or now + 300  # assume 5 min if the token has no exp
        with self._lock:
            self._tokens[key] = _CachedToken(token, expires_at)
            self._tokens.move_to_end(key)
            while len(self._tokens) > self._max:  # bounded LRU: evict least-recently-used
                self._tokens.popitem(last=False)
        return token
