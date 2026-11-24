"""Fake OpenAI-compatible chat model for tests/run.sh's board page chat checks, on port 8009.

If the latest user message is JSON naming a tool (`{"tool": "terminal", "arguments": {...}}`), the
reply asks for that tool, unless the request already holds a tool result: then it answers with that
result, prefixed `tool-result:`. So one turn shows whether the agent ran the tool and what came back.
Any other message gets a plain answer. Streams when asked to, as Hermes does. Logs the tools each
request offered.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def requested_tool(messages):
    users = [m for m in messages if m.get("role") == "user"]
    content = users[-1].get("content") if users else None
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    try:
        request = json.loads(content or "")
    except (TypeError, ValueError):
        return None
    return request if isinstance(request, dict) and "tool" in request else None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send({"object": "list", "data": [{"id": "fast", "object": "model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        messages = body.get("messages") or []
        offered = sorted(t["function"]["name"] for t in body.get("tools") or [])
        print(f"openai request tools={','.join(offered)}", flush=True)
        results = [m for m in messages if m.get("role") == "tool"]
        request = requested_tool(messages)
        if results:
            content = results[-1].get("content")
            text = content if isinstance(content, str) else json.dumps(content)
            message, finish = {"role": "assistant", "content": f"tool-result: {text}"}, "stop"
        elif request:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": request["tool"], "arguments": json.dumps(request.get("arguments") or {})}}]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": "fake-openai answered"}, "stop"
        reply = {"id": "chatcmpl-fake", "created": int(time.time()), "model": body.get("model", "fast")}
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if not body.get("stream"):
            self.send({**reply, "object": "chat.completion", "usage": usage,
                       "choices": [{"index": 0, "message": message, "finish_reason": finish}]})
            return
        delta = dict(message)
        if "tool_calls" in delta:
            delta["tool_calls"] = [{"index": 0, **call} for call in delta["tool_calls"]]
        events = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                  {"choices": [], "usage": usage}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            self.wfile.write(f"data: {json.dumps({**reply, 'object': 'chat.completion.chunk', **event})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


ThreadingHTTPServer(("0.0.0.0", 8009), Handler).serve_forever()
