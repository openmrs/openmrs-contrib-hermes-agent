"""Checks that a failed `agent rotate` leaves no file holding a revoked key, used by tests/run.sh.

Usage: python admin_rotate_checks.py <agent with a board page>

Runs in the admin container against the test LiteLLM. Makes writing the board page's new keys fail,
as a full disk would, after the agent's own new key was written, then checks that the agent's and the
page's files hold their old keys and that those still work.
"""

import sys
from unittest import mock

from hermes_admin.agents import KEY_VAR, Agents, Settings, env_values

name = sys.argv[1]
agents = Agents(Settings.from_env())
before = agents.get(name)
agent_env = agents._home(name) / ".env"
board_env = agents._board_home(name) / ".env"
workspace_key = agents._workspace_config(name) / "litellm_key"
old_agent_key, old_board = env_values(agent_env)[KEY_VAR], env_values(board_env)
old_workspace_key = workspace_key.read_text()

with mock.patch.object(Agents, "_write_board_page", side_effect=OSError("disk full")):
    try:
        agents.rotate(name)
        print("  FAIL: rotate should have failed")
        sys.exit(1)
    except OSError:
        pass

after = agents.get(name)
failures = []
if env_values(agent_env)[KEY_VAR] != old_agent_key:
    failures.append("the agent's .env has a new key")
if workspace_key.read_text() != old_workspace_key:
    failures.append("the workspace has a new key")
if env_values(board_env) != old_board:
    failures.append("the board page's .env changed")
for kind in ("key_hashes", "workspace_key_hashes", "board_key_hashes"):
    if sorted(getattr(after, kind)) != sorted(getattr(before, kind)):
        failures.append(f"{kind} changed: {getattr(before, kind)} -> {getattr(after, kind)}")
for failure in failures:
    print(f"  FAIL: {failure}")
sys.exit(1 if failures else 0)
