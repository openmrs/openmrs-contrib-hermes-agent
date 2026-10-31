"""Stand-in for TypeSafe's Jev `POST /v1/systemone` endpoint, used by tests/run.sh.

Answers with the tier in /tmp/mode (default SIMPLE), or HTTP 500 if it's "fail". Logs a
"jev request" line per call, noting whether the test key was sent.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE_FILE = "/tmp/mode"
EXPECTED_AUTH = "Bearer test-typesafe-key"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            with open(MODE_FILE) as f:
                mode = f.read().strip()
        except FileNotFoundError:
            mode = "SIMPLE"
        auth_ok = self.headers.get("Authorization") == EXPECTED_AUTH
        print(f"jev request auth_ok={auth_ok} mode={mode}", flush=True)
        if mode == "fail":
            self.send_response(500)
            self.end_headers()
            return
        labels = list(body["questions"]["tier"]["criteria"])
        others = 0.1 / max(len(labels) - 1, 1)
        answer = {
            "type": "choice",
            "choice": mode,
            "probabilities": {label: 0.9 if label == mode else others for label in labels},
            "confidence": 0.9,
        }
        data = json.dumps({"model": "jev-latest", "answers": {"tier": answer}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
