"""Stand-in for the Atlassian Rovo MCP server, used by tests/run.sh.

A minimal MCP streamable-HTTP endpoint (JSON responses, no streaming) whose one tool, `whoami`,
reports which test key LiteLLM injected ("read", "jira-write", "confluence-write", or "none")
without echoing it.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

KEYS = {
    "Bearer test-rovo-read-key": "read",
    "Bearer test-rovo-jira-write-key": "jira-write",
    "Bearer test-rovo-confluence-write-key": "confluence-write",
}

WHOAMI = {
    "name": "whoami",
    "description": "Reports which Rovo service account the caller authenticated as.",
    "inputSchema": {"type": "object", "properties": {}},
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = message.get("method")
        account = KEYS.get(self.headers.get("Authorization", ""), "none")
        print(f"rovo request method={method} account={account}", flush=True)
        if "id" not in message:  # notification, e.g. notifications/initialized
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-rovo", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": [WHOAMI]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": account}], "isError": False}
        elif method == "ping":
            result = {}
        else:
            self._reply({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "not found"}})
            return
        self._reply({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def do_GET(self):
        self.send_response(405)
        self.end_headers()

    def do_DELETE(self):
        self.send_response(200)
        self.end_headers()

    def _reply(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 8002), Handler).serve_forever()
