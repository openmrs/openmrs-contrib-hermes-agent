"""Checks onyx_search's pinned tool against Onyx's real one, used by tests/run.sh.

LiteLLM serves the pinned description and schema in place of Onyx's and so never notices when an
Onyx upgrade changes the tool; this does. Runs in the onyx-backend image, which lists its MCP tools
in-process (no database or other Onyx service needed).

Usage: python onyx_tool_checks.py <admin's source types...> < litellm/config.yaml

Prints one line per check, `<name>: <result>`:

  params        "ok" if every pinned parameter exists in Onyx's tool with the same type (ignoring
                Onyx's null option and defaults) and allowed_params names exactly the pinned
                parameters, otherwise the differences
  required      "ok" if the pinned and Onyx's required parameters match, otherwise both
  source-types  "ok" if source_types' enum is exactly admin's source types and each is an Onyx
                source, otherwise the differences
"""

import asyncio
import json
import sys

import yaml

import onyx.mcp_server.tools  # noqa: F401 (registers the tools)
from onyx.configs.constants import DocumentSource
from onyx.mcp_server.api import mcp_server

TOOL = "search_indexed_documents"


def shape(schema):
    """A parameter's type, without what only describes it, and with an optional null unwrapped."""
    schema = {k: v for k, v in schema.items() if k not in ("default", "description", "title", "enum")}
    options = [o for o in schema.pop("anyOf", []) if o != {"type": "null"}]
    if len(options) == 1:
        schema.update(options[0])
    elif options:
        schema["anyOf"] = options
    if "items" in schema:
        schema["items"] = shape(schema["items"])
    return schema


async def onyx_schema():
    for tool in await mcp_server.list_tools():
        if tool.name == TOOL:
            return tool.to_mcp_tool().inputSchema
    sys.exit(f"Onyx has no {TOOL} tool")


server = yaml.safe_load(sys.stdin)["mcp_servers"]["onyx_search"]
pinned = server["pinned_tools"][TOOL]["input_schema"]
upstream = asyncio.run(onyx_schema())

problems = []
for name, schema in pinned["properties"].items():
    theirs = upstream["properties"].get(name)
    if theirs is None:
        problems.append(f"{name} not in Onyx's tool")
    elif shape(schema) != shape(theirs):
        problems.append(f"{name} is {json.dumps(shape(schema))}, Onyx's is {json.dumps(shape(theirs))}")
allowed = set(server["allowed_params"][TOOL])
if allowed != set(pinned["properties"]):
    problems.append(f"allowed_params {sorted(allowed)}, pinned {sorted(pinned['properties'])}")
print("params:", "ok" if not problems else "; ".join(problems))

ours, theirs = sorted(pinned.get("required", [])), sorted(upstream.get("required", []))
print("required:", "ok" if ours == theirs else f"pinned {ours}, Onyx's {theirs}")

enum = set(pinned["properties"]["source_types"]["items"]["enum"])
admin = set(sys.argv[1:])
problems = []
if enum != admin:
    problems.append(f"only pinned {sorted(enum - admin)}, only admin's {sorted(admin - enum)}")
unknown = sorted(enum - {s.value for s in DocumentSource})
if unknown:
    problems.append(f"not Onyx sources {unknown}")
print("source-types:", "ok" if not problems else "; ".join(problems))
