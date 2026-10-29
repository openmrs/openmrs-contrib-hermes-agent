"""hermes-admin: manage Hermes agents' LiteLLM keys, MCP and GitHub access, and Slack settings.

Run with no arguments in a terminal for the interactive interface; subcommands are for scripts.
Secrets are never taken as command-line arguments (they would be visible in the host's process
list): Slack tokens come from masked prompts or, with --from-stdin, as KEY=VALUE lines on stdin.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from .agents import DEFAULT_BUDGET, SLACK_VARS, Agent, AgentError, Agents, Settings, validate_name
from .litellm import LiteLLMError

RESTART_HINT = "Restart the agent to apply: docker compose restart agent-{name}"

# The agent-<name> service for compose.yaml. tests/run.sh checks that agent-default in compose.yaml
# matches this output and that the output passes the compose isolation checks.
COMPOSE_SERVICE = """\
  agent-{name}:
    <<: *hermes-agent
    volumes:
      - type: volume
        source: agents-data
        target: /opt/data
        volume:
          subpath: {name}
      - ./hermes/souls/{name}.md:/opt/data/SOUL.md:ro
      - ./hermes/config.yaml:/opt/data/config.yaml:ro
"""

COMPOSE_STEPS = """\
Next steps:
  1. Add the block above under `services:` in compose.yaml.
  2. Write the agent's identity in hermes/souls/{name}.md.
  3. docker compose run --rm admin create {name} ...  (if not done yet)
  4. docker compose up -d agent-{name}"""


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _agent_json(agent: Agent) -> dict:
    return {
        "name": agent.name,
        "provisioned": agent.provisioned,
        "keys": len(agent.key_hashes),
        "mcp_servers": agent.mcp_servers,
        "github_levels": agent.github_levels,
        "budget": agent.budget,
        "spend": agent.spend,
        "budget_resets": agent.budget_resets,
        "has_data": agent.has_data,
        "env_set": agent.env_set,
    }


def _describe(agent: Agent) -> str:
    status = "active" if agent.provisioned else "no key"
    if len(agent.key_hashes) > 1:
        status += f" ({len(agent.key_hashes)} keys: interrupted rotation, run `rotate` to clean up)"
    slack = "set" if all(agent.env_set.get(var) for var in SLACK_VARS) else "incomplete" if any(
        agent.env_set.get(var) for var in SLACK_VARS) else "not set"
    return "\n".join([
        f"{agent.name}",
        f"  status:      {status}",
        f"  mcp servers: {', '.join(agent.mcp_servers) or 'none'}",
        f"  github:      {', '.join(agent.github_levels) or 'none'}",
        f"  budget:      {agent.describe_budget()}",
        f"  slack:       {slack}",
        f"  data:        {'present' if agent.has_data else 'none'}",
    ])


def _read_slack_stdin() -> dict[str, str]:
    values = {}
    for line in sys.stdin.read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        if name.strip() not in SLACK_VARS:
            raise AgentError(f"unexpected line on stdin for {name.strip()!r}; expected {list(SLACK_VARS)}")
        values[name.strip()] = value.strip()
    return values


def _prompt_slack(keep_existing: bool) -> dict[str, str]:
    hint = " (blank keeps the current value)" if keep_existing else " (blank to skip)"
    values = {}
    for var in SLACK_VARS:
        ask = input if var == "SLACK_ALLOWED_USERS" else getpass.getpass
        value = ask(f"{var}{hint}: ").strip()
        if value:
            values[var] = value
    return values


def _confirm(question: str, expected: str = "y") -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{question} ").strip() == expected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-admin", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("servers", help="list MCP servers agents can be granted")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("levels", help="list GitHub access levels agents can be granted")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="list agents")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="show one agent")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("create", help="create an agent's key and Hermes home")
    p.add_argument("name")
    p.add_argument("--mcp", default="", help="comma-separated MCP servers to grant (default: none)")
    p.add_argument("--github", default="", help="comma-separated GitHub access levels to grant (default: none)")
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                   help=f"monthly spend limit in USD (default: {DEFAULT_BUDGET:g})")
    slack = p.add_mutually_exclusive_group()
    slack.add_argument("--slack", action="store_true", help="prompt for Slack settings")
    slack.add_argument("--slack-from-stdin", action="store_true", help="read SLACK_*=... lines from stdin")

    p = sub.add_parser("grant", help="replace an agent's MCP and/or GitHub grants, or change its budget")
    p.add_argument("name")
    mcp = p.add_mutually_exclusive_group()
    mcp.add_argument("--mcp", help="comma-separated MCP servers")
    mcp.add_argument("--no-mcp", action="store_true", help="remove all MCP access")
    github = p.add_mutually_exclusive_group()
    github.add_argument("--github", help="comma-separated GitHub access levels")
    github.add_argument("--no-github", action="store_true", help="remove all GitHub access")
    p.add_argument("--budget", type=float, help="monthly spend limit in USD")

    p = sub.add_parser("slack", help="set an agent's Slack settings")
    p.add_argument("name")
    p.add_argument("--from-stdin", action="store_true", help="read SLACK_*=... lines from stdin")

    p = sub.add_parser("rotate", help="replace an agent's key")
    p.add_argument("name")

    p = sub.add_parser("compose", help="print the agent's service block for compose.yaml")
    p.add_argument("name")

    p = sub.add_parser("delete", help="revoke an agent's key and remove its secrets")
    p.add_argument("name")
    p.add_argument("--purge", action="store_true", help="also delete the agent's data (sessions, memories, skills)")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    return parser


def run(args: argparse.Namespace, agents: Agents) -> int:
    if args.command == "servers":
        servers = agents.mcp_servers()
        if args.json:
            print(json.dumps(servers))
        else:
            unusable = agents.unusable_mcp_servers()
            print("\n".join(f"{s}  (unusable: check its credential)" if s in unusable else s for s in servers)
                  or "(no MCP servers configured)")
    elif args.command == "levels":
        levels = agents.github_levels()
        print(json.dumps(levels) if args.json else "\n".join(levels) or "(no GitHub access levels configured)")
    elif args.command == "list":
        listed = agents.list()
        if args.json:
            print(json.dumps([_agent_json(a) for a in listed], indent=2))
        else:
            print("\n".join(_describe(a) for a in listed) or "(no agents)")
    elif args.command == "show":
        agent = agents.get(args.name)
        print(json.dumps(_agent_json(agent), indent=2) if args.json else _describe(agent))
    elif args.command == "create":
        slack = _read_slack_stdin() if args.slack_from_stdin else _prompt_slack(False) if args.slack else None
        agent = agents.create(args.name, _split(args.mcp), slack, _split(args.github), args.budget)
        _print_warnings(agents)
        print(_describe(agent))
        print(f"Start the agent: docker compose up -d agent-{agent.name}")
        print(f"If compose.yaml has no agent-{agent.name} service yet, see: docker compose run --rm admin compose {agent.name}")
    elif args.command == "grant":
        mcp = [] if args.no_mcp else None if args.mcp is None else _split(args.mcp)
        github = [] if args.no_github else None if args.github is None else _split(args.github)
        if mcp is None and github is None and args.budget is None:
            raise AgentError("nothing to change: give --mcp/--no-mcp, --github/--no-github, and/or --budget")
        agent = agents.grant(args.name, mcp, github, args.budget)
        _print_warnings(agents)
        print(_describe(agent))
        print("MCP and budget changes take effect immediately, GitHub changes within a minute.")
    elif args.command == "slack":
        values = _read_slack_stdin() if args.from_stdin else _prompt_slack(True)
        if not values:
            print("Nothing changed.")
            return 0
        agents.set_slack(args.name, values)
        print(RESTART_HINT.format(name=args.name))
    elif args.command == "rotate":
        agents.rotate(args.name)
        print(f"Issued a new key for {args.name} and revoked the old one.")
        print(RESTART_HINT.format(name=args.name))
    elif args.command == "compose":
        print(COMPOSE_SERVICE.format(name=validate_name(args.name)), end="")
        print(COMPOSE_STEPS.format(name=args.name), file=sys.stderr)
    elif args.command == "delete":
        if not args.yes:
            prompt = (f"Delete {args.name} and ALL its data? Type the agent name to confirm:" if args.purge
                      else f"Revoke {args.name}'s key and remove its secrets? [y/N]")
            if not _confirm(prompt, args.name if args.purge else "y"):
                print("Cancelled (use --yes to skip confirmation).")
                return 1
        agents.delete(args.name, purge=args.purge)
        print(f"Deleted {args.name}" + ("" if args.purge else " (data kept)") + ".")
    return 0


def _print_warnings(agents: Agents) -> None:
    for warning in agents.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        agents = Agents(Settings.from_env())
        if args.command is None:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                parser.print_help()
                return 2
            from .tui import AdminApp

            AdminApp(agents).run()
            return 0
        return run(args, agents)
    except (AgentError, LiteLLMError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
