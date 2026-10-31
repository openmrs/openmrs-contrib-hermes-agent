"""Stand-in for Anthropic's Messages API, so tests/run.sh can see what LiteLLM sends upstream.

Logs an "anthropic request" line per call saying whether the system prompt and the final message
carry cache_control.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def has_marker(content):
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("cache_control") for b in content)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body.get("messages") or [{}]
        print(
            f"anthropic request model={body.get('model')} "
            f"system_cached={has_marker(body.get('system'))} "
            f"last_cached={has_marker(messages[-1].get('content'))}",
            flush=True,
        )
        data = json.dumps({
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": body.get("model"),
            "content": [{"type": "text", "text": "cache-check-answered"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 3},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 8003), Handler).serve_forever()
