// Power Platform Custom Connector — Approach B (direct IAS OAuth, single exchange)
// SAP Agent Gateway access via a single IAS token exchange. No Microsoft Entra app required.
//
// Connector Security = OAuth 2.0 / "Generic OAuth 2", pointing directly at SCI/IAS.
// The connection delivers the signed-in user's IAS token; this script:
//   Step 1  read the IAS USER token from the Authorization header       (assertion)
//   Step 2  SCI jwt-bearer exchange (assertion, resource=agent-gateway) -> gateway-scoped token
// => real per-user principal propagation: the gateway token carries the real
//    user (sub), ias_apis=["external"].
//
// Secret: IAS_CLIENT_SECRET. It can't be removed inside the connector sandbox
// (no managed identity reachable from ScriptBase); the secretless path is the
// Azure façade — see the cookbook: Known limitations & roadmap.
//
// Fill the placeholders below in a LOCAL-ONLY copy. Do NOT commit real values.
//
// JSON-RPC note: this script does not rewrite the request envelope — in the
// Copilot Studio tool pin `jsonrpc` via a Formula ("2.0"), else it is coerced to
// the number 2 and the gateway returns "Invalid JSON-RPC Request". Alternatively
// repair the envelope in C# with a NormalizeBody() step before forwarding.

public class Script : ScriptBase
{
    private const string IAS_TOKEN_URL     = "https://<your-ias-tenant>.accounts400.ondemand.com/oauth2/token";
    private const string IAS_CLIENT_ID     = "<IAS_CLIENT_ID>";
    private const string IAS_CLIENT_SECRET = "<IAS_CLIENT_SECRET>";
    private const string IAS_RESOURCE_URN  = "urn:sap:identity:application:provider:name:agent-gateway";

    public override async Task<HttpResponseMessage> ExecuteAsync()
    {
        // Step 1 — the connection delivers the signed-in user's IAS token here.
        string userToken = this.Context.Request.Headers.Authorization?.Parameter ?? string.Empty;
        if (string.IsNullOrEmpty(userToken))
        {
            return ErrorResponse(HttpStatusCode.Unauthorized, "missing_user_token",
                "No Bearer token found in Authorization header.");
        }

        // Step 2 — SCI jwt-bearer exchange -> gateway-scoped token.
        string gatewayToken;
        try
        {
            gatewayToken = await IasExchangeJwtBearer(assertion: userToken).ConfigureAwait(false);
        }
        catch (TokenException ex)
        {
            return ErrorResponse(HttpStatusCode.BadGateway, "ias_exchange_failed", ex.Message);
        }

        // Attach the gateway token and forward the original A2A request.
        this.Context.Request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", gatewayToken);
        // Power Platform injects an accept-language header the gateway's A2A
        // validation rejects (-32600 "isNoValidLanguageCode"). Strip it.
        this.Context.Request.Headers.Remove("accept-language");
        // Set the content type only when there is a request body (not for GET).
        if (this.Context.Request.Content != null)
            this.Context.Request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");

        return await this.Context.SendAsync(this.Context.Request, this.CancellationToken)
            .ConfigureAwait(false);
    }

    // ── SCI jwt-bearer exchange -> gateway-scoped token ────────────────────────
    //   POST <IAS_TOKEN_URL>  (application/x-www-form-urlencoded)
    //   grant_type=jwt-bearer, assertion=<user token>, token_format=jwt,
    //   client_id, client_secret, resource=<agent-gateway URN>
    private async Task<string> IasExchangeJwtBearer(string assertion)
    {
        var fields = new Dictionary<string, string>
        {
            ["grant_type"]    = "urn:ietf:params:oauth:grant-type:jwt-bearer",
            ["assertion"]     = assertion,
            ["token_format"]  = "jwt",
            ["client_id"]     = IAS_CLIENT_ID,
            ["client_secret"] = IAS_CLIENT_SECRET,
            ["resource"]      = IAS_RESOURCE_URN,
        };
        return await PostForToken(IAS_TOKEN_URL, fields, "ias").ConfigureAwait(false);
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    // POST form fields to a token endpoint and return access_token (or throw).
    private async Task<string> PostForToken(string url, Dictionary<string, string> fields, string stage)
    {
        var request = new HttpRequestMessage(HttpMethod.Post, url)
        {
            Content = new FormUrlEncodedContent(fields),
        };

        HttpResponseMessage response = await this.Context.SendAsync(request, this.CancellationToken)
            .ConfigureAwait(false);
        string body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);

        if (!response.IsSuccessStatusCode)
        {
            string t = body.Length > 300 ? body.Substring(0, 300) : body;
            throw new TokenException($"{stage}: HTTP {(int)response.StatusCode} — {t}");
        }

        JObject json;
        try { json = JObject.Parse(body); }
        catch (JsonException ex) { throw new TokenException($"{stage}: non-JSON response — {ex.Message}"); }

        string token = json["access_token"]?.Value<string>();
        if (string.IsNullOrEmpty(token))
            throw new TokenException($"{stage}: access_token missing in response");
        return token;
    }

    private static HttpResponseMessage ErrorResponse(HttpStatusCode status, string code, string detail)
    {
        var body = new JObject { ["error"] = code, ["detail"] = detail };
        return new HttpResponseMessage(status) { Content = CreateJsonContent(body.ToString()) };
    }

    private class TokenException : Exception
    {
        public TokenException(string message) : base(message) { }
    }
}
