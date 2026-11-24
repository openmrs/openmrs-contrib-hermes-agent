"""Fake OpenID Connect provider (standing in for Keycloak) for tests/run.sh's Kanban board checks.

Runs in the Hermes image, whose venv has PyJWT and cryptography. Serves HTTPS with the certificate
run.sh generates (the board's dashboard only accepts an https issuer), on port 8443:
  GET  /.well-known/openid-configuration   discovery
  GET  /jwks                                the signing key
  GET  /authorize                           logs in at once as `login_hint` (default board-user) and
                                            redirects back with a code, as Keycloak would after a
                                            successful login; `deny=1` answers access_denied instead,
                                            as a Keycloak flow with "Deny Access" does
  POST /token                               authorization_code (PKCE S256 checked) and refresh_token
Logs one line per token issued, so run.sh can count logins.
"""

import base64
import hashlib
import json
import secrets
import ssl
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://fake-oidc:8443"
CERT, KEY = "/tls/fake-oidc.crt", "/tls/fake-oidc.key"
KID = "fake-oidc-1"

signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
public_jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key())),
              "kid": KID, "use": "sig", "alg": "RS256"}
# code -> (client_id, redirect_uri, code_challenge, user)
codes: dict[str, tuple[str, str, str, str]] = {}
# refresh token -> (client_id, user)
refresh_tokens: dict[str, tuple[str, str]] = {}


def id_token(client_id: str, user: str) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": client_id, "sub": f"sub-{user}", "email": f"{user}@example.org",
              "name": user, "preferred_username": user, "iat": now, "exp": now + 300}
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": KID})


def tokens(client_id: str, user: str) -> dict:
    refresh = secrets.token_urlsafe(24)
    refresh_tokens[refresh] = (client_id, user)
    print(f"token issued user={user} client={client_id}", flush=True)
    return {"access_token": secrets.token_urlsafe(24), "token_type": "Bearer", "expires_in": 300,
            "id_token": id_token(client_id, user), "refresh_token": refresh}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status: int, body: dict | None = None, headers: dict | None = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/.well-known/openid-configuration":
            self.send(200, {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
                            "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks",
                            "response_types_supported": ["code"], "subject_types_supported": ["public"],
                            "id_token_signing_alg_values_supported": ["RS256"],
                            "code_challenge_methods_supported": ["S256"]})
        elif url.path == "/jwks":
            self.send(200, {"keys": [public_jwk]})
        elif url.path == "/authorize":
            redirect_uri = query.get("redirect_uri", "")
            params = {"state": query.get("state", "")}
            if query.get("deny") == "1":
                params["error"] = "access_denied"
            else:
                code = secrets.token_urlsafe(16)
                codes[code] = (query.get("client_id", ""), redirect_uri, query.get("code_challenge", ""),
                               query.get("login_hint") or "board-user")
                params["code"] = code
            self.send(302, headers={"Location": f"{redirect_uri}?{urllib.parse.urlencode(params)}"})
        else:
            self.send(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        if urllib.parse.urlparse(self.path).path != "/token":
            self.send(404, {"error": "not_found"})
            return
        if form.get("grant_type") == "authorization_code":
            entry = codes.pop(form.get("code", ""), None)
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if (not entry or entry[0] != form.get("client_id") or entry[1] != form.get("redirect_uri")
                    or entry[2] != challenge):
                self.send(400, {"error": "invalid_grant"})
                return
            self.send(200, tokens(entry[0], entry[3]))
        elif form.get("grant_type") == "refresh_token":
            entry = refresh_tokens.pop(form.get("refresh_token", ""), None)
            if not entry:
                self.send(400, {"error": "invalid_grant"})
                return
            self.send(200, tokens(*entry))
        else:
            self.send(400, {"error": "unsupported_grant_type"})


server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(CERT, KEY)
server.socket = context.wrap_socket(server.socket, server_side=True)
print("fake-oidc listening on 8443", file=sys.stderr, flush=True)
server.serve_forever()
