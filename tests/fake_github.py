"""Stand-in for GitHub's "create an installation access token" endpoint, used by tests/run.sh.

Verifies the App JWT the way GitHub would (RS256 signature against /app/public.pem, issuer, and a
lifetime of at most 10 minutes). Logs one "mint" line per request with the results and the
requested permissions and repositories (as compact JSON), and answers with a numbered token that
expires in an hour.
"""

import base64
import json
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

APP_ID = "12345"
INSTALLATION_ID = "67890"
PUBLIC_KEY = "/app/public.pem"
minted = 0


def b64url_decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def jwt_ok(token):
    try:
        header, payload, signature = token.split(".")
        if json.loads(b64url_decode(header)).get("alg") != "RS256":
            return False
        claims = json.loads(b64url_decode(payload))
        now = time.time()
        if claims.get("iss") != APP_ID or not claims["iat"] <= now < claims["exp"] or claims["exp"] - claims["iat"] > 600:
            return False
        with tempfile.NamedTemporaryFile() as sig, tempfile.NamedTemporaryFile() as data:
            sig.write(b64url_decode(signature))
            sig.flush()
            data.write(f"{header}.{payload}".encode())
            data.flush()
            return subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", PUBLIC_KEY, "-signature", sig.name, data.name],
                capture_output=True, check=False,
            ).returncode == 0
    except (ValueError, KeyError):
        return False


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global minted
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        auth = self.headers.get("Authorization", "")
        signed = auth.startswith("Bearer ") and jwt_ok(auth[len("Bearer "):])
        path_ok = self.path == f"/app/installations/{INSTALLATION_ID}/access_tokens"
        permissions = json.dumps(body.get("permissions"), sort_keys=True, separators=(",", ":"))
        repositories = json.dumps(body.get("repositories"), separators=(",", ":"))
        print(f"mint path_ok={path_ok} jwt_ok={signed} permissions={permissions} repositories={repositories}", flush=True)
        if not (path_ok and signed):
            self.send_response(401)
            self.end_headers()
            return
        minted += 1
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = json.dumps({"token": f"ghs_test_{minted}", "expires_at": expires, "permissions": body.get("permissions")})
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data.encode())

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 8004), Handler).serve_forever()
