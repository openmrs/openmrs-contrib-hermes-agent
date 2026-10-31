"""Agent provisioning: LiteLLM virtual keys, MCP and GitHub grants, and each agent's Hermes home.

LiteLLM is the source of truth. An agent is the set of keys whose metadata has
`managed_by: hermes-admin` and `hermes_agent: <name>`; other keys are never touched. MCP grants are
the key's object_permission; GitHub grants are levels (from github-token/levels.json) in its
`github_levels` metadata, checked by github-token. More than one key per agent means an interrupted
rotation, which `rotate` or `delete` cleans up.

Each agent also has a workspace key (`hermes_role: workspace`) so `gh` in its workspace can get
GitHub tokens. It has the same `github_levels` but may only call /key/info, so code run in the
workspace can't spend the agent's budget or use its MCP grants.

The monthly budget is on the agent's LiteLLM user (`hermes-agent-<name>`), not a key, so spend
survives `rotate`.

`<agents_dir>/<name>` is the agent's Hermes home (/opt/data), mounted only by that agent. Its `.env`
holds the LiteLLM key and Slack tokens; `ssh/` holds the agent's SSH key and the workspace's host
key. `<workspaces_dir>/<name>` holds the workspace's authorized key, host key, and workspace key,
root-only and mounted only by that workspace. Both are read at startup, so changes need a restart.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .litellm import LiteLLM

MANAGED_BY = "hermes-admin"

# Model names from litellm/config.yaml that agents may request.
AGENT_MODELS = ["auto", "fast", "balanced", "strong"]

# Explicit rather than LiteLLM's `llm_api_routes` group, which also allows raw provider passthroughs
# (/anthropic/*, ...) that bypass the `auto` router. /key/info lets github-token check a key's grants
# with the key itself; a non-admin key can only read keys with its own user_id (see _user_id).
ALLOWED_ROUTES = [
    "/v1/chat/completions", "/chat/completions", "/v1/models", "/models", "mcp_inference_routes", "/key/info",
]

# LiteLLM's sentinel for "no MCP servers"; an empty list would mean "inherit".
NO_MCP_SERVERS = "no-mcp-servers"

# Workspace keys only need github-token's grant check. "no-default-models" is LiteLLM's sentinel for
# no models; an empty list means all.
WORKSPACE_ROLE = "workspace"
WORKSPACE_ROUTES = ["/key/info"]
WORKSPACE_MODELS = ["no-default-models"]
# Where agents reach their workspace's sshd (see workspace/supervisor.py and hermes/config.yaml).
WORKSPACE_SSH_PORT = 2222

# SLACK_TOKEN_VARS are masked when entered. Slack setup is complete once SLACK_REQUIRED_VARS are set.
SLACK_TOKEN_VARS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
SLACK_REQUIRED_VARS = (*SLACK_TOKEN_VARS, "SLACK_ALLOWED_USERS")
SLACK_VARS = (*SLACK_REQUIRED_VARS, "SLACK_HOME_CHANNEL")
KEY_VAR = "LITELLM_API_KEY"
SECRET_VARS = (KEY_VAR, *SLACK_VARS)

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# Monthly spend limit, in USD, for agents created without an explicit budget.
DEFAULT_BUDGET = 1000.0
BUDGET_DURATION = "1mo"


class AgentError(Exception):
    """An operation was refused or failed; the message is meant for the operator."""


@dataclass(frozen=True)
class Settings:
    litellm_url: str
    master_key: str
    agents_dir: Path
    workspaces_dir: Path
    github_levels_file: Path
    uid: int
    gid: int

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            litellm_url=os.environ.get("LITELLM_URL", "http://litellm:4000"),
            master_key=os.environ.get("LITELLM_MASTER_KEY", ""),
            agents_dir=Path(os.environ.get("AGENTS_DIR", "/agents")),
            workspaces_dir=Path(os.environ.get("WORKSPACES_DIR", "/workspaces")),
            github_levels_file=Path(os.environ.get("GITHUB_LEVELS_FILE", "/templates/github-levels.json")),
            uid=int(os.environ.get("HERMES_UID", "10000")),
            gid=int(os.environ.get("HERMES_GID", "10000")),
        )


@dataclass
class Agent:
    name: str
    key_hashes: list[str] = field(default_factory=list)
    workspace_key_hashes: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    github_levels: list[str] = field(default_factory=list)
    budget: float | None = None
    spend: float = 0.0
    budget_resets: str | None = None
    has_data: bool = False
    has_workspace: bool = False
    env_set: dict[str, bool] = field(default_factory=dict)

    @property
    def provisioned(self) -> bool:
        return bool(self.key_hashes)

    def describe_budget(self) -> str:
        if self.budget is None:
            return "none"
        resets = f", resets {self.budget_resets[:10]}" if self.budget_resets else ""
        return f"${self.spend:,.2f} of ${self.budget:,.2f} this month{resets}"


def validate_name(name: str) -> str:
    if not NAME_PATTERN.match(name):
        raise AgentError(f"invalid agent name {name!r}: use lowercase letters, digits, and dashes (max 63)")
    return name


def validate_budget(budget: float) -> float:
    if not math.isfinite(budget) or budget <= 0:
        raise AgentError(f"invalid budget {budget!r}: give a positive amount in USD")
    return budget


def read_env(path: Path) -> list[tuple[str | None, str]]:
    """Lines of a .env file as (name, line) pairs; name is None for comments and blanks."""
    if not path.exists():
        return []
    lines = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        name = stripped.split("=", 1)[0].strip() if stripped and not stripped.startswith("#") and "=" in stripped else None
        lines.append((name, line))
    return lines


def env_values(path: Path) -> dict[str, str]:
    return {name: line.split("=", 1)[1] for name, line in read_env(path) if name}


class Agents:
    def __init__(self, settings: Settings, api: LiteLLM | None = None) -> None:
        self.settings = settings
        self.api = api or LiteLLM(settings.litellm_url, settings.master_key)
        # Non-fatal problems from the last create or grant, for the operator.
        self.warnings: list[str] = []

    # -- queries -------------------------------------------------------------------------------

    def mcp_servers(self) -> list[str]:
        return sorted(self.api.mcp_server_ids())

    def unusable_mcp_servers(self) -> dict[str, str]:
        """Configured MCP servers LiteLLM can't use (e.g. an empty credential). Slow: checks each
        server upstream. Servers that are only down aren't included."""
        return {name: fault for name, fault in self._server_faults(self.api.mcp_server_ids()).items()
                if fault == "internal"}

    def github_levels(self) -> list[str]:
        """Access levels defined in github-token/levels.json; none if the file isn't mounted."""
        try:
            return sorted(json.loads(self.settings.github_levels_file.read_text()))
        except FileNotFoundError:
            return []

    def list(self) -> list[Agent]:
        agents: dict[str, Agent] = {}
        for key in self.api.keys():
            metadata = key.get("metadata") or {}
            if metadata.get("managed_by") != MANAGED_BY or not metadata.get("hermes_agent"):
                continue
            agent = agents.setdefault(metadata["hermes_agent"], Agent(metadata["hermes_agent"]))
            if metadata.get("hermes_role") == WORKSPACE_ROLE:
                agent.workspace_key_hashes.append(key["token"])
                continue
            agent.key_hashes.append(key["token"])
            servers = (key.get("object_permission") or {}).get("mcp_servers") or []
            agent.mcp_servers = sorted(s for s in servers if s != NO_MCP_SERVERS)
            agent.github_levels = sorted(metadata.get("github_levels") or [])
        if self.settings.agents_dir.is_dir():
            for path in self.settings.agents_dir.iterdir():
                if path.is_dir() and NAME_PATTERN.match(path.name):
                    agents.setdefault(path.name, Agent(path.name))
        for agent in agents.values():
            self._add_budget(agent)
            self._add_local_state(agent)
        return sorted(agents.values(), key=lambda a: a.name)

    def get(self, name: str) -> Agent:
        validate_name(name)
        for agent in self.list():
            if agent.name == name:
                return agent
        raise AgentError(f"no agent named {name!r}")

    def _add_budget(self, agent: Agent) -> None:
        user = self.api.user_info(self._user_id(agent.name)) or {}
        agent.budget = user.get("max_budget")
        agent.spend = user.get("spend") or 0.0
        agent.budget_resets = user.get("budget_reset_at")

    def _add_local_state(self, agent: Agent) -> None:
        home = self._home(agent.name)
        agent.has_data = home.is_dir()
        agent.has_workspace = ((self._workspace_config(agent.name) / "litellm_key").exists()
                               and (home / "ssh" / "id_ed25519").exists())
        values = env_values(home / ".env")
        agent.env_set = {var: bool(values.get(var)) for var in SECRET_VARS}

    # -- changes -------------------------------------------------------------------------------

    def create(
        self, name: str, mcp: list[str], slack: dict[str, str] | None = None, github: list[str] | None = None,
        budget: float = DEFAULT_BUDGET,
    ) -> Agent:
        """Issue the agent's and its workspace's keys and prepare their homes. `budget` is monthly USD.

        Refuses if the agent already has a key. Existing data (e.g. after `delete`) and this month's
        spend are kept.
        """
        validate_name(name)
        validate_budget(budget)
        self.warnings = []
        if any(a.name == name and a.provisioned for a in self.list()):
            raise AgentError(f"agent {name!r} already exists; use `grant` to change access or `rotate` for a new key")
        servers = self._checked_servers(mcp)
        levels = self._checked_levels(github or [])
        self._set_budget(name, budget)
        key, key_hash = self._issue_key(name, servers, levels)
        workspace_key, workspace_key_hash = self._issue_workspace_key(name, levels)
        try:
            self._prepare_home(name)
            self._write_env(name, {KEY_VAR: key, **(slack or {})})
            self._write_workspace(name, workspace_key)
        except Exception:
            self.api.delete_keys([key_hash, workspace_key_hash])
            raise
        return self.get(name)

    def grant(
        self, name: str, mcp: list[str] | None = None, github: list[str] | None = None, budget: float | None = None,
    ) -> Agent:
        """Replace the agent's MCP and/or GitHub grants and/or monthly budget (None leaves that
        unchanged). An agent with no budget yet gets DEFAULT_BUDGET.

        MCP and budget changes take effect immediately; GitHub changes within a minute
        (github-token caches key checks). No restart needed.
        """
        if budget is not None:
            validate_budget(budget)
        self.warnings = []
        agent = self._existing(name)
        if budget is not None or agent.budget is None:
            self._set_budget(name, DEFAULT_BUDGET if budget is None else budget)
        changes: dict = {}
        if mcp is not None:
            changes["object_permission"] = {"mcp_servers": self._checked_servers(mcp) or [NO_MCP_SERVERS]}
        levels = agent.github_levels if github is None else self._checked_levels(github)
        # Metadata is replaced whole. user_id and allowed_routes are reset so older keys get current values.
        changes["metadata"] = self._metadata(name, levels)
        changes["user_id"] = self._user_id(name)
        changes["allowed_routes"] = ALLOWED_ROUTES
        for key_hash in agent.key_hashes:
            self.api.update_key(key_hash, changes)
        workspace_changes = {"metadata": self._metadata(name, levels, WORKSPACE_ROLE), "user_id": self._user_id(name),
                             "allowed_routes": WORKSPACE_ROUTES}
        for key_hash in agent.workspace_key_hashes:
            self.api.update_key(key_hash, workspace_changes)
        return self.get(name)

    def rotate(self, name: str) -> Agent:
        """Issue new LiteLLM and SSH keys for the agent and its workspace, then revoke the old ones.
        Also creates a missing workspace key."""
        agent = self._existing(name)
        key, key_hash = self._issue_key(name, agent.mcp_servers, agent.github_levels)
        workspace_key, workspace_key_hash = self._issue_workspace_key(name, agent.github_levels)
        try:
            self._write_env(name, {KEY_VAR: key})
            self._write_workspace(name, workspace_key)
        except Exception:
            self.api.delete_keys([key_hash, workspace_key_hash])
            raise
        self.api.delete_keys(agent.key_hashes + agent.workspace_key_hashes)
        return self.get(name)

    def set_slack(self, name: str, values: dict[str, str]) -> Agent:
        unknown = set(values) - set(SLACK_VARS)
        if unknown:
            raise AgentError(f"unknown Slack settings {sorted(unknown)}; expected {list(SLACK_VARS)}")
        self._existing(name)
        self._write_env(name, values)
        return self.get(name)

    def delete(self, name: str, purge: bool = False) -> None:
        """Revoke the agent's and its workspace's keys and remove their secrets. `purge` also deletes
        its data and LiteLLM user (budget and spend); otherwise both are kept for `create`."""
        agent = self.get(name)
        self.api.delete_keys(agent.key_hashes + agent.workspace_key_hashes)
        home = self._home(name)
        shutil.rmtree(self._workspace_config(name), ignore_errors=True)
        if purge:
            if self.api.user_info(self._user_id(name)) is not None:
                self.api.delete_user(self._user_id(name))
            shutil.rmtree(home, ignore_errors=True)
            return
        shutil.rmtree(home / "ssh", ignore_errors=True)
        if (home / ".env").exists():
            self._write_env(name, {var: None for var in SECRET_VARS})

    # -- helpers -------------------------------------------------------------------------------

    def _existing(self, name: str) -> Agent:
        agent = self.get(name)
        if not agent.provisioned:
            raise AgentError(f"agent {name!r} has no key; use `create`")
        return agent

    def _checked_servers(self, mcp: list[str]) -> list[str]:
        """Refuses unknown servers and ones LiteLLM can't use; ones only down upstream get a warning."""
        servers = sorted({s.strip() for s in mcp if s.strip()})
        ids = self.api.mcp_server_ids()
        unknown = [s for s in servers if s not in ids]
        if unknown:
            raise AgentError(f"unknown MCP servers {unknown}; available: {sorted(ids) or 'none'}")
        faults = self._server_faults({s: ids[s] for s in servers})
        unusable = sorted(s for s, fault in faults.items() if fault == "internal")
        if unusable:
            raise AgentError(f"LiteLLM can't use MCP servers {unusable}; check their credentials in "
                             "secrets/litellm.env and LiteLLM's logs")
        self.warnings.extend(f"couldn't check MCP server {s!r} ({fault}); granted anyway"
                             for s, fault in sorted(faults.items()) if fault)
        return servers

    def _server_faults(self, ids: dict[str, str]) -> dict[str, str | None]:
        return {name: self.api.mcp_server_fault(server_id) for name, server_id in ids.items()}

    def _checked_levels(self, github: list[str]) -> list[str]:
        levels = sorted({level.strip() for level in github if level.strip()})
        available = set(self.github_levels())
        unknown = [level for level in levels if level not in available]
        if unknown:
            raise AgentError(f"unknown GitHub levels {unknown}; available: {sorted(available) or 'none'}")
        return levels

    @staticmethod
    def _user_id(name: str) -> str:
        return f"hermes-agent-{name}"

    @staticmethod
    def _metadata(name: str, github_levels: list[str], role: str | None = None) -> dict:
        metadata = {"managed_by": MANAGED_BY, "hermes_agent": name, "github_levels": github_levels}
        return {**metadata, "hermes_role": role} if role else metadata

    def _set_budget(self, name: str, budget: float) -> None:
        user_id = self._user_id(name)
        limits = {"max_budget": budget, "budget_duration": BUDGET_DURATION}
        if self.api.user_info(user_id) is None:
            self.api.new_user({"user_id": user_id, **limits})
        else:
            self.api.update_user(user_id, limits)

    def _issue_key(self, name: str, servers: list[str], github_levels: list[str]) -> tuple[str, str]:
        """Returns the new key and its hash (LiteLLM's id for the key)."""
        response = self.api.generate_key({
            # Aliases must be unique across keys, so each issued key gets its own.
            "key_alias": f"hermes-{name}-{time.strftime('%Y%m%d%H%M%S')}",
            "user_id": self._user_id(name),
            "models": AGENT_MODELS,
            "allowed_routes": ALLOWED_ROUTES,
            "object_permission": {"mcp_servers": servers or [NO_MCP_SERVERS]},
            "metadata": self._metadata(name, github_levels),
        })
        return response["key"], response["token"]

    def _issue_workspace_key(self, name: str, github_levels: list[str]) -> tuple[str, str]:
        """Returns the new workspace key and its hash."""
        response = self.api.generate_key({
            "key_alias": f"hermes-{name}-workspace-{time.strftime('%Y%m%d%H%M%S')}",
            "user_id": self._user_id(name),
            "models": WORKSPACE_MODELS,
            "allowed_routes": WORKSPACE_ROUTES,
            "object_permission": {"mcp_servers": [NO_MCP_SERVERS]},
            "metadata": self._metadata(name, github_levels, WORKSPACE_ROLE),
        })
        return response["key"], response["token"]

    def _home(self, name: str) -> Path:
        return self.settings.agents_dir / name

    def _workspace_config(self, name: str) -> Path:
        return self.settings.workspaces_dir / name

    def _write_workspace(self, name: str, workspace_key: str) -> None:
        """Generate SSH keys for the agent and its workspace. The agent's private key and the
        workspace's host key (as known_hosts) go to the agent's home; the rest, with the workspace
        key, to the root-only workspace configuration."""
        if "\n" in workspace_key or "\r" in workspace_key:
            raise AgentError("the workspace key must be a single line")
        with tempfile.TemporaryDirectory() as tmp:
            for key_file, comment in (("client", f"hermes-{name}"), ("host", f"workspace-{name}")):
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", f"{tmp}/{key_file}"],
                               check=True, capture_output=True)
            client_key, client_public = Path(tmp, "client").read_text(), Path(tmp, "client.pub").read_text()
            host_key, host_public = Path(tmp, "host").read_text(), Path(tmp, "host.pub").read_text()
        config = self._workspace_config(name)
        config.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(config, 0, 0)
        os.chmod(config, 0o700)
        self._write_file(config / "authorized_keys", client_public, 0o600, 0, 0)
        self._write_file(config / "ssh_host_ed25519_key", host_key, 0o600, 0, 0)
        self._write_file(config / "litellm_key", workspace_key + "\n", 0o600, 0, 0)
        ssh = self._home(name) / "ssh"
        ssh.mkdir(mode=0o700, exist_ok=True)
        uid, gid = self.settings.uid, self.settings.gid
        os.chown(ssh, uid, gid)
        self._write_file(ssh / "id_ed25519", client_key, 0o600, uid, gid)
        # Drop the comment, which known_hosts would read as a marker or host name.
        host_entry = " ".join(host_public.split()[:2])
        self._write_file(ssh / "known_hosts", f"[workspace-{name}]:{WORKSPACE_SSH_PORT} {host_entry}\n", 0o644, uid, gid)

    @staticmethod
    def _write_file(path: Path, content: str, mode: int, uid: int, gid: int) -> None:
        """Atomically replace `path`, setting mode and owner before it becomes visible."""
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.chown(tmp, uid, gid)
        os.replace(tmp, path)

    def _prepare_home(self, name: str) -> None:
        home = self._home(name)
        home.mkdir(parents=True, exist_ok=True)
        os.chown(home, self.settings.uid, self.settings.gid)

    def _write_env(self, name: str, changes: dict[str, str | None]) -> None:
        """Set (or, for None, remove) variables in the agent's .env, keeping all other lines."""
        path = self._home(name) / ".env"
        for var, value in changes.items():
            if value is not None and ("\n" in value or "\r" in value):
                raise AgentError(f"{var} must be a single line")
        pending = dict(changes)
        lines = []
        for var, line in read_env(path):
            if var in pending:
                value = pending.pop(var)
                if value is not None:
                    lines.append(f"{var}={value}")
            else:
                lines.append(line)
        lines.extend(f"{var}={value}" for var, value in pending.items() if value is not None)
        self._write_file(path, "\n".join(lines) + "\n", 0o600, self.settings.uid, self.settings.gid)
