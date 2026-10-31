"""Calls Hermes' terminal, file, and code tools the way the agent does, used by tests/run.sh.

Runs in the agent container (on stdin, as the Hermes user). Prints one summary line: the user and
host the terminal ran as, whether write_file's file reached the terminal, whether read_file can see
the agent's own SOUL.md, and the user execute_code ran as.

Gateway turns and subagents run tools under a terminal scope built only from the profile's .env and
config.yaml, ignoring the process environment, so the tools are called under that scope here too.
"""

import json

from hermes_constants import get_hermes_home
from model_tools import handle_function_call
from tools.terminal_scope import install_profile_terminal_scope

install_profile_terminal_scope(get_hermes_home())


def call(name, args):
    return json.loads(handle_function_call(name, args, task_id="workspace-check"))


terminal = call("terminal", {"command": "echo $(id -u)@$(hostname)"})["output"].strip()
call("write_file", {"path": "/work/home/hermes-check.txt", "content": "written\n"})
written = call("terminal", {"command": "cat /work/home/hermes-check.txt"})["output"].strip()
agent_file = call("read_file", {"path": "/opt/data/SOUL.md"})
code = call("execute_code", {"code": "import os\nprint(os.getuid())"})["output"].strip()
print(f"terminal={terminal} write_file={written} agent_file_visible={not agent_file.get('not_found')} "
      f"execute_code={code}")
