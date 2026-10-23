#!/usr/bin/env python3
"""End-to-end smoke test for the deployed MCP path (stdlib only).

Logs into Cognito via auth-code + PKCE (browser), takes the resulting **access
token** (the Bearer Amazon Quick sends on every MCP request), then drives
initialize -> tools/list -> tools/call sap_assistant against the AgentCore
Gateway MCP endpoint. This exercises the full chain the cookbook describes:
Cognito -> Gateway authorizer -> runtime -> IAS OBO -> SAP Agent Gateway.

No pip installs: uses urllib + http.server from the stdlib.

Configuration is read from the environment (copy env.example -> .envrc and let
direnv export it, or `source .envrc` first):
  COGNITO_APP_CLIENT_ID   Cognito app client id (PKCE public client).
  COGNITO_DOMAIN          Cognito Hosted UI domain (https://...).
  MCP_GATEWAY_URL         Gateway target /invocations URL.

Prerequisite: the Cognito app client must allow this script's loopback redirect
URI (http://localhost:8765/callback) in its callback URLs, in addition to the
Amazon Quick callback.

Usage:
  python3 tests/integration/run_once.py "show me my latest compliance training"
"""
from __future__ import annotations
import base64, hashlib, http.server, json, os, secrets, socketserver, sys, threading
import urllib.error, urllib.parse, urllib.request, uuid, webbrowser


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing {name} — copy env.example to .envrc and fill it in (or `source .envrc`)")
    return value


CLIENT_ID = _env("COGNITO_APP_CLIENT_ID")
COGNITO_DOMAIN = _env("COGNITO_DOMAIN").rstrip("/")
GATEWAY_URL = _env("MCP_GATEWAY_URL")
# NOTE: this loopback redirect URI must be listed in the Cognito app client's
# allowed callback URLs (in addition to the Amazon Quick callback), or the login
# redirect will fail with a redirect_mismatch error.
REDIRECT_URI = "http://localhost:8765/callback"
SCOPE = "openid email profile"
MESSAGE = " ".join(sys.argv[1:]) or "What can you help me with?"


def _pkce():
    v = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    return v, c


class _CB(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path != "/callback":
            self.send_response(404); self.end_headers(); return
        qs = urllib.parse.parse_qs(p.query)
        _CB.result = {k: (qs.get(k) or [""])[0] for k in ("code", "state", "error")}
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(b"<html><body style='font-family:sans-serif;padding:2em'>"
                         b"<h2>Login complete.</h2><p>You can close this tab.</p></body></html>")

    def log_message(self, *a):
        pass


def get_access_token() -> str:
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    url = f"{COGNITO_DOMAIN}/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "scope": SCOPE,
        "redirect_uri": REDIRECT_URI, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state,
    })
    print(f"[login] opening browser...\n  if it doesn't open, visit:\n  {url}\n")
    srv = socketserver.TCPServer(("127.0.0.1", 8765), _CB)
    t = threading.Thread(target=srv.handle_request, daemon=True); t.start()
    webbrowser.open(url)
    t.join(timeout=180); srv.server_close()
    if _CB.result.get("error"):
        raise SystemExit(f"OAuth error: {_CB.result['error']}")
    code = _CB.result.get("code")
    if not code:
        raise SystemExit("timed out waiting for the Cognito callback")
    if _CB.result.get("state") != state:
        raise SystemExit("state mismatch (CSRF guard)")
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": CLIENT_ID, "code": code,
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(f"{COGNITO_DOMAIN}/oauth2/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.load(r)
    tok = body["access_token"]
    claims = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "=="))
    print(f"[login] got access token  email={claims.get('email')}  aud={claims.get('aud')}  "
          f"token_use={claims.get('token_use')}  scope={claims.get('scope')!r}\n")
    return tok


def mcp(token: str, method: str, params: dict | None = None, session: str | None = None):
    body = {"jsonrpc": "2.0", "method": method}
    if not method.startswith("notifications/"):
        body["id"] = str(uuid.uuid4())
    if params is not None:
        body["params"] = params
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if session:
        headers["mcp-session-id"] = session
    req = urllib.request.Request(GATEWAY_URL, data=json.dumps(body).encode(), headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}  {e.reason}")
        print("  " + e.read().decode("utf-8", "ignore")[:2000]); return None, None
    sid = r.headers.get("mcp-session-id") or session
    raw = r.read().decode("utf-8", "ignore")
    print(f"  HTTP {r.status}  (session={sid})")
    out = None
    ct = r.headers.get("content-type", "")
    if "event-stream" in ct:
        for line in raw.splitlines():
            if line.startswith("data:"):
                d = line[5:].strip()
                if d:
                    try:
                        out = json.loads(d)
                    except json.JSONDecodeError:
                        print(f"  raw> {d}")
    elif raw.strip():
        try:
            out = json.loads(raw)
        except json.JSONDecodeError:
            print(f"  raw> {raw[:1000]}")
    if out is not None:
        print(json.dumps(out, indent=2)[:4000])
    return out, sid


def main():
    tok = get_access_token()
    print("[1/3] initialize")
    _, sid = mcp(tok, "initialize", params={
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "run_once", "version": "1.0"},
    })
    print("\n[.] notifications/initialized")
    mcp(tok, "notifications/initialized", session=sid)
    print("\n[2/3] tools/list")
    mcp(tok, "tools/list", session=sid)
    print(f"\n[3/3] tools/call sap_assistant  message={MESSAGE!r}")
    mcp(tok, "tools/call", params={"name": "sap_assistant",
                                   "arguments": {"message": MESSAGE}}, session=sid)
    print("\n[done] now check CloudWatch for the server-side trace")


if __name__ == "__main__":
    main()
