"""Stand-in for GitHub's "create an installation access token" and "create a review for a pull
request" endpoints, used by tests/run.sh.

Token requests: verifies the App JWT as GitHub would (RS256 against /app/public.pem, issuer,
lifetime of at most 10 minutes), logs a "mint" line with the results and the requested permissions
and repositories, and answers with a numbered token.

Review requests: logs a "review" line with the repository, pull request, commit, event, number of
inline comments, and token, and answers as GitHub does. A comment on the path "not-in-diff.txt"
gets GitHub's 422 for a line outside the diff; pull request 30 gets a 422 about the commit. Pull request 15 gets a 500 and pull request 16 a 200
whose body isn't JSON, both after the review is logged, as if GitHub had created it.

Pull request reads (GET /repos/<repo>/pulls/<n>): logs a "pull" line with the repository, pull
request, and token. Pull request 40 is closed and 43 a draft; pull request 44's answer isn't JSON;
pull request 41's head is "f" * 40 and 28's is "e" * 40; any other is open with head "c" * 40
(run.sh's webhook events use that commit).
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
PULL_PATH = re.compile(r"^/repos/([^/]+/[^/]+)/pulls/([0-9]+)$")
HEADS = {"41": "f" * 40, "28": "e" * 40}
LINE_NOT_IN_DIFF = "Unprocessable Entity: pull_request_review_thread.line must be part of the diff"
# A 422 that moving a comment can't fix, e.g. after a force-push. Not GitHub's exact wording.
COMMIT_NOT_IN_PR = "commit_id is not part of the pull request"


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
        if pr == "30":
            self.send_json(422, {"message": "Unprocessable Entity", "errors": [COMMIT_NOT_IN_PR], "status": "422"})
            return
        if pr in ("15", "16"):
            self.send_response(500 if pr == "15" else 200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>not JSON</html>")
            return
        self.send_json(200, {"id": 1, "html_url": f"https://github.com/{repo}/pull/{pr}#pullrequestreview-1"})

    def do_GET(self):
        pull = PULL_PATH.match(self.path)
        if not pull:
            self.send_json(404, {"message": "Not Found"})
            return
        auth = self.headers.get("Authorization", "")
        token = auth.split(" ", 1)[1] if " " in auth else ""
        repo, pr = pull.groups()
        print(f"pull repo={repo} pr={pr} token={token}", flush=True)
        if pr == "44":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>not JSON</html>")
            return
        self.send_json(200, {"number": int(pr), "state": "closed" if pr == "40" else "open", "draft": pr == "43",
                             "head": {"sha": HEADS.get(pr, "c" * 40)}})

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
