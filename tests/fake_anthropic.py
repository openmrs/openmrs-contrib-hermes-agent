"""Stand-in for Anthropic's Messages API, so tests/run.sh can see what LiteLLM sends upstream.

Logs an "anthropic request" line per call saying whether the system prompt and the final message
carry cache_control, the names of the tools offered, and whether the system prompt or messages
contain each of MARKERS. Always answers with plain text, so an agent run ends without calling a
tool; streams the answer when the request asks for it.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEXT = "cache-check-answered"
# Log field: text in hermes/souls/reviewer.md and hermes/reviewer/skills/pr-review/SKILL.md.
MARKERS = {"soul_marker": "REVIEWER-SOUL-MARKER", "skill_marker": "PR-REVIEW-SKILL-MARKER"}


def has_marker(content):
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("cache_control") for b in content)


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body.get("messages") or [{}]
        prompt = json.dumps([body.get("system"), body.get("messages")])
        tools = ",".join(t.get("name", "") for t in body.get("tools") or [] if isinstance(t, dict))
        markers = " ".join(f"{field}={text in prompt}" for field, text in MARKERS.items())
        print(
            f"anthropic request model={body.get('model')} "
            f"system_cached={has_marker(body.get('system'))} "
            f"last_cached={has_marker(messages[-1].get('content'))} "
            f"stream={bool(body.get('stream'))} tools={tools} {markers}",
            flush=True,
        )
        message = {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": body.get("model"),
            "content": [{"type": "text", "text": TEXT}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 3},
        }
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            start = dict(message, content=[], stop_reason=None, usage={"input_tokens": 10, "output_tokens": 1})
            for event, data in (
                ("message_start", {"type": "message_start", "message": start}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": TEXT}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": 3}}),
                ("message_stop", {"type": "message_stop"}),
            ):
                self.wfile.write(sse(event, data))
            return
        data = json.dumps(message).encode()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 8003), Handler).serve_forever()
