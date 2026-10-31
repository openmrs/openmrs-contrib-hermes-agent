"""Sends one HTTP request to LiteLLM from inside its container, used by tests/run.sh.

Usage: python3 http_request.py METHOD PATH KEY [JSON_BODY]

Prints the status code, then the body. Running in the container avoids depending on the host port.
"""

import sys
import urllib.error
import urllib.request

method, path, key = sys.argv[1:4]
body = sys.argv[4] if len(sys.argv) > 4 else None
request = urllib.request.Request(
    f"http://127.0.0.1:4000{path}",
    method=method,
    data=body.encode() if body is not None and method != "GET" else None,
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        status, payload = response.status, response.read()
except urllib.error.HTTPError as e:
    status, payload = e.code, e.read()
print(status)
sys.stdout.write(payload.decode(errors="replace"))
