"""Stand-in for the mcp-atlassian container behind LiteLLM, used by tests/run.sh.

A minimal MCP streamable-HTTP endpoint (JSON responses, no streaming). It offers one tool from each
level the atlassian_* servers distinguish, under mcp-atlassian's names: a read tool, a Jira write, a
Confluence write, and a delete no server allows. Every call answers with the test key LiteLLM
injected ("read", "jira-write", "confluence-write", or "none") without echoing it, and is logged as
`atlassian call tool=<name> account=<account>` so tests can tell whether a call got through.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

KEYS = {
    "Bearer test-atlassian-read-key": "read",
    "Bearer test-atlassian-jira-write-key": "jira-write",
    "Bearer test-atlassian-confluence-write-key": "confluence-write",
}

TOOLS = [
    {"name": name, "description": f"Fake {name}.", "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": read_only}}
    for name, read_only in [("jira_get_issue", True), ("jira_add_comment", False),
                            ("confluence_update_page", False), ("jira_delete_issue", False)]
]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = message.get("method")
        account = KEYS.get(self.headers.get("Authorization", ""), "none")
        if method == "tools/call":
            print(f"atlassian call tool={message['params']['name']} account={account}", flush=True)
        if "id" not in message:  # notification, e.g. notifications/initialized
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-atlassian", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
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
