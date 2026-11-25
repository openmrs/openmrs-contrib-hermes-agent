"""Checks the board chat's tools, used by tests/run.sh.

Runs in the board page container (on stdin, as the Hermes user), with hermes/board.yaml as its config.
Resolves the toolsets of the board chat (the API server's platform, `api_server`), then runs the agent
loop with them against tests/fake_openai.py, which asks for whatever tool the message names. Prints
one `name: value` line per check.
"""

import json

from hermes_cli.config import load_config
from hermes_cli.tools_config import _get_platform_tools
from model_tools import get_tool_definitions
from run_agent import AIAgent

# Tools the chat must never have: running commands or code, files, the web, other agents, schedules,
# memory, and skills.
FORBIDDEN = {"terminal", "process_manage", "read_file", "write_file", "patch", "search_files", "execute_code",
             "browser_navigate", "web_search", "web_extract", "delegate_task", "cronjob_manage", "memory",
             "skill_manage", "skill_view", "image_generate", "text_to_speech", "vision_analyze"}

config = load_config()
toolsets = sorted(_get_platform_tools(config, "api_server", include_default_mcp_servers=True))
disabled = config["agent"]["disabled_toolsets"]
tools = {d["function"]["name"] for d in get_tool_definitions(enabled_toolsets=toolsets, disabled_toolsets=disabled,
                                                               quiet_mode=True)}
print(f"toolsets: {' '.join(sorted(toolsets))}")
print(f"forbidden: {' '.join(sorted(tools & FORBIDDEN)) or 'none'}")
print(f"kanban: {'kanban_create' in tools and 'kanban_list' in tools and 'kanban_comment' in tools}")


def ask(tool, arguments):
    agent = AIAgent(base_url="http://fake-openai:8009/v1", api_key="test-key", model="fast", quiet_mode=True,
                    enabled_toolsets=toolsets, disabled_toolsets=disabled, max_iterations=4,
                    skip_context_files=True, skip_memory=True)
    reply = agent.chat(json.dumps({"tool": tool, "arguments": arguments}))
    return " ".join(str(reply).split())


print(f"terminal: {ask('terminal', {'command': 'id'})}")
print(f"tool_call terminal: {ask('tool_call', {'name': 'terminal', 'arguments': {'command': 'id'}})}")
print(f"kanban_list: {ask('kanban_list', {})}")
