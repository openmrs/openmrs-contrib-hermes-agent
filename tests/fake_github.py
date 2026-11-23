"""Stand-in for GitHub's "create an installation access token" and "create a review for a pull
request" endpoints, used by tests/run.sh.

Token requests: verifies the App JWT as GitHub would (RS256 against /app/public.pem, issuer,
lifetime of at most 10 minutes), logs a "mint" line with the results and the requested permissions
and repositories, and answers with a numbered token.

Review requests: logs a "review" line with the repository, pull request, commit, event, number of
inline comments, and token, and answers as GitHub does. A comment on the path "not-in-diff.txt"
gets GitHub's 422 for a line outside the diff.
"""

import base64
import json
import re
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


REVIEW_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/pulls/([0-9]+)/reviews$")
LINE_NOT_IN_DIFF = "Unprocessable Entity: pull_request_review_thread.line must be part of the diff"


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def review(self, repo, pr, body, auth):
        comments = body.get("comments") or []
        token = auth.split(" ", 1)[1] if " " in auth else ""
        print(f"review repo={repo} pr={pr} commit={body.get('commit_id')} event={body.get('event')} "
              f"comments={len(comments)} token={token}", flush=True)
        if any(c.get("path") == "not-in-diff.txt" for c in comments):
            self.send_json(422, {"message": LINE_NOT_IN_DIFF, "status": "422"})
            return
        self.send_json(200, {"id": 1, "html_url": f"https://github.com/{repo}/pull/{pr}#pullrequestreview-1"})

    def do_POST(self):
        global minted
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        auth = self.headers.get("Authorization", "")
        review = REVIEW_PATH.match(self.path)
        if review:
            self.review(review.group(1), review.group(2), body, auth)
            return
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
