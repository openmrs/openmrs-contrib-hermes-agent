"""Stand-in for onyx-mcp behind LiteLLM, used by tests/run.sh.

A minimal MCP streamable-HTTP endpoint (JSON responses, no streaming) offering Onyx's
search_indexed_documents with Onyx's parameters and a description of its own, so tests can tell
whether callers see Onyx's tool or onyx_search's pinned one. A call answers with its arguments as
JSON and is logged as `onyx call args=<arguments>`, so tests can see what got through.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def optional(schema):
    return {"anyOf": [schema, {"type": "null"}], "default": None}


STRINGS = {"type": "array", "items": {"type": "string"}}
TOOLS = [{
    "name": "search_indexed_documents",
    "description": "Fake Onyx search.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "source_types": optional(STRINGS),
            "document_set_names": optional(STRINGS),
            "time_cutoff": optional({"type": "string"}),
            "skip_query_expansion": {"type": "boolean", "default": False},
            "agent": optional({"type": "string"}),
        },
        "required": ["query"],
    },
}]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = message.get("method")
        if "id" not in message:  # notification, e.g. notifications/initialized
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-onyx", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            arguments = json.dumps(message["params"].get("arguments") or {}, sort_keys=True)
            print(f"onyx call args={arguments}", flush=True)
            result = {"content": [{"type": "text", "text": arguments}], "isError": False}
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


HTTPServer(("0.0.0.0", 8006), Handler).serve_forever()
