"""Changes the pr-review skill and plants a file next to it in the workspace's .hermes directory,
then ends the terminal session, used by tests/run.sh.

Runs in the agent container (on stdin, as the Hermes user). Ending the session makes Hermes copy
changed files from the workspace's .hermes back into the agent's HERMES_HOME (sync_back): a changed
file it pushed, and a new file in a folder that holds one it pushed (a new file anywhere else has no
host path, so it is skipped either way). Prints one line: whether the skill was pushed to the
workspace, and whether both changes were made there before the session ended.

Tools run under the profile's terminal scope, as in tests/hermes_workspace_tools.py.
"""

import json

from hermes_constants import get_hermes_home
from model_tools import handle_function_call
from tools.terminal_scope import install_profile_terminal_scope
from tools.terminal_tool_lifecycle import cleanup_vm

install_profile_terminal_scope(get_hermes_home())

TASK = "sync-check"
SKILL = "~/.hermes/skills/pr-review"


def terminal(command):
    return json.loads(handle_function_call("terminal", {"command": command}, task_id=TASK))["output"].strip()


pushed = terminal(f"test -f {SKILL}/SKILL.md && echo yes")
changed = terminal(f"echo planted >> {SKILL}/SKILL.md && echo planted > {SKILL}/planted.md"
                   f" && grep -qx planted {SKILL}/SKILL.md && test -f {SKILL}/planted.md && echo yes")
cleanup_vm(TASK)
print(f"pushed={pushed} changed_in_workspace={changed}")
