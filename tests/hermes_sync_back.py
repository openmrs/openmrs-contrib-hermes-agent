"""Plants a skill in the workspace's .hermes directory, then ends the terminal session, used by tests/run.sh.

Runs in the agent container (on stdin, as the Hermes user). Ending the session makes Hermes copy
changed files from the workspace's .hermes back into the agent's HERMES_HOME (sync_back). Prints one
line: whether the planted file existed in the workspace before the session ended.

Tools run under the profile's terminal scope, as in tests/hermes_workspace_tools.py.
"""

import json

from hermes_constants import get_hermes_home
from model_tools import handle_function_call
from tools.terminal_scope import install_profile_terminal_scope
from tools.terminal_tool_lifecycle import cleanup_vm

install_profile_terminal_scope(get_hermes_home())

TASK = "sync-check"
planted = json.loads(handle_function_call("terminal", {
    "command": "mkdir -p ~/.hermes/skills/planted && echo planted > ~/.hermes/skills/planted/SKILL.md"
               " && test -f ~/.hermes/skills/planted/SKILL.md && echo yes"}, task_id=TASK))["output"].strip()
cleanup_vm(TASK)
print(f"planted_in_workspace={planted}")
