"""Checks what agents get from LiteLLM's onyx_search server, used by tests/run.sh.

Runs in the litellm container and talks MCP to LiteLLM's /mcp/onyx_search, as agents do, with
tests/fake_onyx.py upstream. Usage: python3 onyx_gateway_checks.py <litellm config.yaml> <key>

Prints one line per check, `<name>: <result>`:

  description  "pinned" if the served description is the config's pinned one, otherwise it
  params       the served tool's parameter names, space-separated
  call         what fake_onyx received for a call with every served parameter (its arguments as
               JSON), or the error
  hidden       for a call that adds `agent`, "error: <message>" if it was refused, otherwise
               what fake_onyx received
"""

import json
import sys
import urllib.request

import yaml

URL = "http://127.0.0.1:4000/mcp/onyx_search"
TOOL = "onyx_search-search_indexed_documents"
ARGUMENTS = {"query": "how do I configure O3?", "source_types": ["web"], "time_cutoff": "2026-01-01T00:00:00Z",
             "skip_query_expansion": True}

pinned = yaml.safe_load(open(sys.argv[1]))["mcp_servers"]["onyx_search"]["pinned_tools"]["search_indexed_documents"]
headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
           "x-litellm-api-key": f"Bearer {sys.argv[2]}"}


def rpc(method, params=None, id=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id is not None:
        body["id"] = id
    request = urllib.request.Request(URL, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        if session := response.headers.get("mcp-session-id"):
            headers["mcp-session-id"] = session
        text = response.read().decode()
    if id is None:
        return None
    events = [json.loads(line[len("data: "):]) for line in text.splitlines() if line.startswith("data: ")]
    return events[0] if events else json.loads(text)


def call(arguments):
    reply = rpc("tools/call", {"name": TOOL, "arguments": arguments}, id=3)
    if "error" in reply:
        return f"error: {reply['error'].get('message')}"
    result = reply["result"]
    text = " ".join(block.get("text", "") for block in result.get("content", []))
    return f"error: {text}" if result.get("isError") else text


rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "check", "version": "1"}})
rpc("notifications/initialized", id=None)
tools = {t["name"]: t for t in rpc("tools/list", id=2)["result"]["tools"]}
tool = tools.get(TOOL)
if tool is None:
    sys.exit(f"no {TOOL}; served {sorted(tools)}")

print("description:", "pinned" if tool.get("description") == pinned["description"] else tool.get("description"))
print("params:", " ".join(sorted(tool["inputSchema"]["properties"])))
print("call:", call(ARGUMENTS))
print("hidden:", call({**ARGUMENTS, "agent": "Engineering Support"}))
