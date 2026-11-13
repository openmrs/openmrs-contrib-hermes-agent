"""hermes-admin: manage Hermes agents' LiteLLM keys, MCP and GitHub access, and Slack settings, and Onyx.

Run with no arguments in a terminal for the interactive interface; subcommands are for scripts.
Secrets are never taken as command-line arguments (they would be visible in the host's process
list): Slack tokens come from masked prompts or, with --from-stdin, as KEY=VALUE lines on stdin.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from .agents import (DEFAULT_BUDGET, SLACK_REQUIRED_VARS, SLACK_TOKEN_VARS, SLACK_VARS, Agent, AgentError, Agents,
                     Settings, validate_name)
from .litellm import LiteLLMError
from .onyx import (DEFAULT_ONYX_BUDGET, DEFAULT_REFRESH_HOURS, SOURCE_TYPES, Onyx, OnyxSettings, SourceType,
                   source_settings)

RESTART_HINT = "Restart the agent to apply: docker compose restart agent-{name}"
ROTATE_HINT = "Restart the agent and its workspace to apply: docker compose restart workspace-{name} agent-{name}"

# The agent-<name> and workspace-<name> services for compose.yaml. tests/run.sh checks that
# compose.yaml has this output for `default` and that the output passes the compose isolation checks.
COMPOSE_SERVICE = """\
  agent-{name}:
    <<: *hermes-agent
    depends_on:
      litellm:
        condition: service_healthy
      workspace-{name}:
        condition: service_healthy
        restart: true
    volumes:
      - type: volume
        source: agents-data
        target: /opt/data
        volume:
          subpath: {name}
      - type: volume
        source: agents-data
        target: /opt/data/.env
        read_only: true
        volume:
          subpath: {name}/.env
      - ./hermes/souls/{name}.md:/opt/data/SOUL.md:ro
      - ./hermes/config.yaml:/opt/data/config.yaml:ro

  workspace-{name}:
    <<: *workspace
    environment:
      <<: *workspace-environment
      WORKSPACE_IDLE_MINUTES: "180"
      WORKSPACE_KEEP_FILES: "false"
    volumes:
      - type: volume
        source: workspace-config
        target: /config
        read_only: true
        volume:
          subpath: {name}
      - workspace-cache:/cache
      # Emptied whenever the workspace resets.
      - /work
"""

COMPOSE_STEPS = """\
Next steps:
  1. Add the blocks above under `services:` in compose.yaml.
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
        "workspace_keys": len(agent.workspace_key_hashes),
        "mcp_servers": agent.mcp_servers,
        "github_levels": agent.github_levels,
        "budget": agent.budget,
        "spend": agent.spend,
        "budget_resets": agent.budget_resets,
        "has_data": agent.has_data,
        "has_workspace": agent.has_workspace,
        "env_set": agent.env_set,
    }


def _describe(agent: Agent) -> str:
    status = "active" if agent.provisioned else "no key"
    if len(agent.key_hashes) > 1:
        status += f" ({len(agent.key_hashes)} keys: interrupted rotation, run `rotate` to clean up)"
    workspace = "ready" if agent.has_workspace and len(agent.workspace_key_hashes) == 1 else (
        f"{len(agent.workspace_key_hashes)} keys: run `rotate` to clean up" if agent.workspace_key_hashes
        else "no key: run `rotate`" if agent.provisioned else "none")
    slack = "set" if all(agent.env_set.get(var) for var in SLACK_REQUIRED_VARS) else "incomplete" if any(
        agent.env_set.get(var) for var in SLACK_VARS) else "not set"
    return "\n".join([
        f"{agent.name}",
        f"  status:      {status}",
        f"  mcp servers: {', '.join(agent.mcp_servers) or 'none'}",
        f"  github:      {', '.join(agent.github_levels) or 'none'}",
        f"  budget:      {agent.describe_budget()}",
        f"  workspace:   {workspace}",
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
        ask = getpass.getpass if var in SLACK_TOKEN_VARS else input
        value = ask(f"{var}{hint}: ").strip()
        if value:
            values[var] = value
    return values


def _confirm(question: str, expected: str = "y") -> bool:
    """Asks on stdin, which needn't be a terminal: bin/admin passes `-T` when its output is
    redirected, and some Compose setups don't allocate a TTY. No input (EOF) means no."""
    try:
        return input(f"{question} ").strip() == expected
    except EOFError:
        print()
        return False


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

    p = sub.add_parser("onyx", help="configure Onyx, the search index behind the onyx_search MCP server")
    onyx = p.add_subparsers(dest="onyx_command", required=True)
    q = onyx.add_parser("setup", help="log in (registering the administrator on a new Onyx) and point Onyx at LiteLLM")
    q.add_argument("--budget", type=float,
                   help=f"Onyx's monthly LiteLLM spend limit in USD (default on first setup: {DEFAULT_ONYX_BUDGET:g})")
    q.add_argument("--rotate-key", action="store_true", help="issue Onyx a new LiteLLM key and revoke the old one")
    onyx.add_parser("token", help="print a new search token for ONYX_SEARCH_PAT, revoking the previous one")
    q = onyx.add_parser("sources", help="list Onyx's sources and their indexing state")
    q.add_argument("--json", action="store_true")
    q = onyx.add_parser("cleanup", help="delete connectors and credentials no source uses (left by a failed add)")
    q.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    q = onyx.add_parser("add", help="add a source for Onyx to index, asking for its settings",
                        description="Source types: " + "; ".join(f"{k}: {t.description}" for k, t in SOURCE_TYPES.items())
                        + ". Onyx doesn't copy sources' permissions: everything the account can see becomes "
                          "searchable by every caller.")
    q.add_argument("type", choices=list(SOURCE_TYPES))
    q.add_argument("name", help="a name for the source in Onyx, e.g. wiki")
    q.add_argument("--refresh-hours", type=float, default=DEFAULT_REFRESH_HOURS,
                   help=f"how often Onyx re-indexes it (default {DEFAULT_REFRESH_HOURS})")
    q.add_argument("--from-stdin", action="store_true",
                   help="read the settings as KEY=VALUE lines from stdin instead of asking (keys: see the prompts)")
    q.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    q = onyx.add_parser("edit", help="change a source's settings, asking for each with its current value")
    q.add_argument("name", nargs="?", help="the source's name (default: choose from a list)")
    q.add_argument("--refresh-hours", type=float, help="how often Onyx re-indexes it (default: unchanged)")
    q.add_argument("--from-stdin", action="store_true",
                   help="read the settings to change as KEY=VALUE lines from stdin; the rest stay as they are")
    q.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    q = onyx.add_parser("delete", help="delete a source, its documents in the index, and its credential")
    q.add_argument("name", nargs="?", help="the source's name (default: choose from a list)")
    q.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    return parser


def run_onyx(args: argparse.Namespace, onyx: Onyx) -> int:
    if args.onyx_command == "setup":
        print("\n".join(onyx.setup(args.budget, args.rotate_key)))
    elif args.onyx_command == "token":
        token = onyx.token()
        print("Put this in secrets/litellm.env, then restart LiteLLM (docker compose restart litellm).",
              file=sys.stderr)
        print("The previous token, if any, is revoked: search fails until LiteLLM restarts.", file=sys.stderr)
        print(f"ONYX_SEARCH_PAT={token}")
    elif args.onyx_command == "sources":
        sources = onyx.sources()
        if args.json:
            print(json.dumps(sources, indent=2))
        else:
            print("\n".join(f"{s['name']} ({s['source']}): {s['status']}, {s['documents']} documents, last index "
                            f"{s['last_index'] or 'none'}" for s in sources) or "(no sources)")
    elif args.onyx_command == "add":
        source_type = SOURCE_TYPES[args.type]
        values = _read_source_stdin(source_type) if args.from_stdin else _prompt_source(source_type)
        source_settings(args.type, args.name, values, args.refresh_hours)
        if not args.yes:
            print(f"Add {args.name} ({args.type}), re-indexed every {args.refresh_hours:g} hours:")
            _print_source(source_type, values)
            if not _confirm("Everything this account can see becomes searchable by every caller. Add it? [y/N]"):
                print("Cancelled (use --yes to skip confirmation).")
                return 1
        print(onyx.add(args.type, args.name, values, args.refresh_hours))
    elif args.onyx_command == "edit":
        name = args.name or _pick_source(onyx)
        if name is None:
            return 1
        kind, current, refresh_hours = onyx.source(name)
        source_type = SOURCE_TYPES[kind]
        values = {**current, **(_read_source_stdin(source_type) if args.from_stdin
                                else _prompt_source(source_type, current))}
        refresh_hours = args.refresh_hours or refresh_hours
        source_settings(kind, name, values, refresh_hours, keep_secrets=True)
        if not args.yes:
            print(f"Update {name} ({kind}), re-indexed every {refresh_hours:g} hours:")
            _print_source(source_type, values, blank_secret="(unchanged)")
            if not _confirm("Update it? [y/N]"):
                print("Cancelled (use --yes to skip confirmation).")
                return 1
        print(onyx.edit(name, values, refresh_hours))
    elif args.onyx_command == "delete":
        name = args.name or _pick_source(onyx)
        if name is None:
            return 1
        if not args.yes and not _confirm(f"Delete {name} and remove its documents from the index? [y/N]"):
            print("Cancelled (use --yes to skip confirmation).")
            return 1
        print(f"Deleting {name}; Onyx can take a few minutes to remove its documents...", flush=True)
        print(onyx.delete(name))
    elif args.onyx_command == "cleanup":
        connectors, credentials = onyx.unused()
        if not connectors and not credentials:
            print("Nothing to clean up")
            return 0
        if not args.yes:
            print("Not used by any source:")
            for kind, items in (("connector", connectors), ("credential", credentials)):
                for item in items:
                    print(f"  {kind} {item['name']!r} ({item['source']})")
            if not _confirm("Delete them? [y/N]"):
                print("Cancelled (use --yes to skip confirmation).")
                return 1
        print("\n".join(onyx.cleanup(connectors, credentials)))
    return 0


def _read_source_stdin(source_type: SourceType) -> dict[str, str]:
    keys = {f.key for f in source_type.fields}
    values = {}
    for line in sys.stdin.read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        if name.strip() not in keys:
            raise AgentError(f"unexpected line on stdin for {name.strip()!r}; expected {sorted(keys)}")
        values[name.strip()] = value.strip()
    return values


def _prompt_source(source_type: SourceType, current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Asks for each field. With `current` (editing), Enter keeps a field's current value; blank
    tokens are kept by Onyx."""
    values: dict[str, Any] = {}
    for f in source_type.fields:
        if f.kind == "bool":
            default = current[f.key] if current else f.default
            answer = input(f"{f.label} [{'Y/n' if default else 'y/N'}]: ").strip()
            values[f.key] = answer or default
        elif f.secret:
            hint = "input hidden; blank keeps the current one" if current is not None else "input hidden"
            values[f.key] = getpass.getpass(f"{f.label} ({hint}): ").strip()
        else:
            old = (current or {}).get(f.key, "")
            answer = input(f"{f.label}{f' [{old}]' if old else ''}{' (- to clear)' if old and not f.required else ''}: ")
            answer = answer.strip()
            values[f.key] = "" if answer == "-" else answer or old
    return values


def _print_source(source_type: SourceType, values: dict[str, Any], blank_secret: str = "(blank)") -> None:
    for f in source_type.fields:
        value = values.get(f.key, "")
        if f.secret:
            shown = "(set)" if value else blank_secret
        elif f.kind == "bool":
            shown = ("yes" if value else "no") if isinstance(value, bool) else value or "(default)"
        else:
            shown = value or "(blank)"
        print(f"  {f.label}: {shown}")


def _pick_source(onyx: Onyx) -> str | None:
    """Lists Onyx's sources numbered and asks which one; None if there are none or no answer."""
    names = [s["name"] for s in onyx.sources()]
    if not names:
        print("Onyx has no sources.")
        return None
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    try:
        answer = input(f"Which source? [1-{len(names)}] ").strip()
    except EOFError:
        print()
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(names):
        return names[int(answer) - 1]
    if answer in names:
        return answer
    print("Cancelled: no such source.")
    return None


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
        print(f"Issued new keys for {args.name} and its workspace and revoked the old ones.")
        print(ROTATE_HINT.format(name=args.name))
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
        if args.command == "onyx":
            return run_onyx(args, Onyx(OnyxSettings.from_env(), agents.api))
        if args.command is None:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                parser.print_help()
                return 2
            from .tui import AdminApp

            AdminApp(agents, Onyx(OnyxSettings.from_env(), agents.api)).run()
            return 0
        return run(args, agents)
    except (AgentError, LiteLLMError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
