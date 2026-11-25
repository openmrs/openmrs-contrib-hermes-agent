"""Sends a signed GitHub webhook, as GitHub does, for tests/run.sh.

Usage: python3 webhook_post.py <url> <secret> <event> [--bad-signature] < body.json

Signs the body with HMAC-SHA256 (X-Hub-Signature-256) and sends a new X-GitHub-Delivery id each
time. --bad-signature signs with a different secret. Prints the HTTP status, then the answer's
"status" and "reason" fields if it has them.

Ignores proxy settings: the URL is always a test container or its port on 127.0.0.1, and on macOS
urllib otherwise sends even 127.0.0.1 to the system's HTTP proxy.
"""

import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request
import uuid

if len(sys.argv) not in (4, 5) or sys.argv[4:] not in ([], ["--bad-signature"]):
    sys.exit(__doc__)
url, secret, event = sys.argv[1:4]
if sys.argv[4:]:
    secret += "-wrong"
body = sys.stdin.buffer.read()
request = urllib.request.Request(url, data=body, method="POST", headers={
    "Content-Type": "application/json",
    "X-GitHub-Event": event,
    "X-GitHub-Delivery": str(uuid.uuid4()),
    "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
})
try:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30) as response:
        status, raw = response.status, response.read()
except urllib.error.HTTPError as e:
    status, raw = e.code, e.read()
except urllib.error.URLError as e:
    sys.exit(f"unreachable: {e.reason}")
try:
    answer = json.loads(raw)
except ValueError:
    answer = {}
if not isinstance(answer, dict):
    answer = {}
print(" ".join(str(v) for v in (status, answer.get("status"), answer.get("reason")) if v))
