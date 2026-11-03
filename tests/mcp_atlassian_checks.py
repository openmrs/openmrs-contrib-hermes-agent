"""Checks the real mcp-atlassian container's configuration, used by tests/run.sh.

Runs in the litellm container (the only other service on the atlassian network) with the LiteLLM
config path as its argument. Nothing reaches Atlassian: listing tools needs no upstream call, and
the other request carries no credentials. Prints one line per check, `<name>: <result>`:

  tools        "match" if mcp-atlassian serves exactly the union of the atlassian_* servers'
               allowed_tools, otherwise the differences
  read-only    "ok" if exactly atlassian_read's tools are annotated read-only, otherwise the
               mismatches
  no-key       the HTTP status mcp-atlassian answers a request without credentials with
"""

import json
import sys
import urllib.error
import urllib.request

import yaml

URL = "http://mcp-atlassian:9000/mcp"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "check", "version": "1"}}}


def rpc(body, headers):
    request = urllib.request.Request(URL, json.dumps(body).encode(), {**HEADERS, **headers})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode()
    events = [json.loads(line[len("data: "):]) for line in text.splitlines() if line.startswith("data: ")]
    return events[0] if events else json.loads(text)


servers = yaml.safe_load(open(sys.argv[1]))["mcp_servers"]
allowed = {name: set(s["allowed_tools"]) for name, s in servers.items() if name.startswith("atlassian_")}
union = set().union(*allowed.values())

fake_key = {"Authorization": "Bearer not-a-real-key"}
rpc(INITIALIZE, fake_key)
tools = {t["name"]: t for t in rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, fake_key)["result"]["tools"]}

served = set(tools)
print("tools:", "match" if served == union else f"only served {sorted(served - union)}, only allowed {sorted(union - served)}")

read_only = {name for name, t in tools.items() if (t.get("annotations") or {}).get("readOnlyHint") is True}
wrong = sorted(read_only ^ allowed["atlassian_read"])
print("read-only:", "ok" if not wrong else f"mismatched {wrong}")

try:
    rpc(INITIALIZE, {})
    print("no-key: 200")
except urllib.error.HTTPError as e:
    print(f"no-key: {e.code}")
