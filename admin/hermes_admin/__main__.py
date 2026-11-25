"""hermes-admin: manage Hermes agents (LiteLLM keys, MCP and GitHub access, budgets, Slack settings)
and the sources Onyx indexes.

Run with no arguments in a terminal for the interactive interface; subcommands are for scripts.
Commands that act on an existing agent or source ask which one when no name is given.
Secrets are never taken as command-line arguments (they would be visible in the host's process
list): they come from masked prompts or, with --from-stdin, as KEY=VALUE lines on stdin.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any, Callable

from .agents import (BOARD_MCP_SERVERS, DEFAULT_BOARD_BUDGET, DEFAULT_BUDGET, SLACK_TOKEN_VARS, SLACK_VARS, Agent,
                     AgentError, Agents, Settings, validate_name)
from .litellm import LiteLLMError
from .messages import (BOARD_EDIT_HINT, BOARD_HINT, BOARD_REVOKED, DELETED, EDIT_HINT, INDEX_BUSY, INDEX_QUEUED,
                       PURGED, ROTATE_DONE, ROTATE_HINT, ROTATE_WARNING, SLACK_HINT, START_HINT)
from .onyx import (DEFAULT_ONYX_BUDGET, DEFAULT_REFRESH_HOURS, SOURCE_TYPES, Onyx, OnyxError, OnyxSettings,
                   SourceType, format_errors, source_settings)

# The agent-<name>, workspace-<name>, board-<name>, and kanban-web-<name> services for compose.yaml. tests/run.sh checks
# that compose.yaml has this output for `default` and that the output passes the compose isolation
# checks. {var} is the name for host settings (BOARD_<VAR>_URL), {port} the board page's default port.
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

  board-{name}:
    <<: *hermes-board
    environment:
      <<: *board-environment
      # The page's address as users reach it, e.g. https://board.example.org; its /auth/callback must be
      # a redirect URI of the OIDC client. A loopback address turns off the login, so kanban-web refuses
      # every request until this is set.
      HERMES_DASHBOARD_PUBLIC_URL: ${{BOARD_{var}_URL:-http://127.0.0.1:{port}}}
    # kanban-web-{name}'s port, published from this namespace. Host-only; put a TLS reverse proxy in front.
    ports:
      - "127.0.0.1:${{BOARD_{var}_PORT:-{port}}}:8080"
    volumes:
      - type: volume
        source: board-homes
        target: /opt/data
        volume:
          subpath: {name}
      - type: volume
        source: board-homes
        target: /opt/data/.env
        read_only: true
        volume:
          subpath: {name}/.env
      - type: volume
        source: agents-data
        target: /opt/data/kanban-shared
        volume:
          subpath: {name}/kanban-shared
      - ./hermes/souls/board.md:/opt/data/SOUL.md:ro
      - ./hermes/board.yaml:/opt/data/config.yaml:ro

  kanban-web-{name}:
    <<: *kanban-web
    network_mode: service:board-{name}
    depends_on:
      board-{name}:
        condition: service_started
        restart: true
    volumes:
      - ./board-web:/app:ro
      - type: volume
        source: board-homes
        target: /run/board-web
        read_only: true
        volume:
          subpath: {name}/web
"""

COMPOSE_STEPS = """\
Next steps:
  1. Add the blocks above under `services:` in compose.yaml.
  2. Write the agent's identity in hermes/souls/{name}.md.
  3. docker compose run --rm admin agent add {name} ...  (if not done yet)
  4. docker compose up -d agent-{name}
  5. For its board page: docker compose run --rm admin agent board {name}, set BOARD_{var}_URL (and
     BOARD_{var}_PORT if {port} is taken) in ./.env, then docker compose --profile boards up -d kanban-web-{name}"""

CANCELLED = "Cancelled (use --yes to skip confirmation)."


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
        "board_keys": len(agent.board_key_hashes),
        "board_mcp_servers": agent.board_mcp_servers,
        "board_budget": agent.board_budget,
        "board_spend": agent.board_spend,
        "has_board_page": agent.has_board_page,
    }


def _describe(agent: Agent) -> str:
    status = agent.key_status
    if len(agent.key_hashes) > 1:
        status += " (interrupted rotation: run `agent rotate` to clean up)"
    workspace = agent.workspace_status
    if workspace not in ("ready", "none"):
        workspace += ": run `agent rotate`"
    board = agent.board_status
    if board == "ready":
        board += (f"; MCP servers: {', '.join(agent.board_mcp_servers) or 'none'}; "
                  f"budget: {agent.describe_board_budget()}")
    elif board != "none":
        board += ": run `agent board`" if board == "no page" else ": run `agent rotate`"
    return "\n".join([
        f"{agent.name}",
        f"  Status:      {status}",
        f"  MCP servers: {', '.join(agent.mcp_servers) or 'none'}",
        f"  GitHub:      {', '.join(agent.github_levels) or 'none'}",
        f"  Budget:      {agent.describe_budget()}",
        f"  Workspace:   {workspace}",
        f"  Slack:       {agent.slack_status}",
        f"  Board page:  {board}",
        f"  Data:        {'present' if agent.has_data else 'none'}",
    ])


def _read_stdin(keys: list[str] | tuple[str, ...]) -> dict[str, str]:
    """KEY=VALUE lines from stdin, skipping blanks and # comments; refuses keys not in `keys`."""
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


def _pick(noun: str, names: list[str]) -> str | None:
    """Lists `names` numbered and asks which one; None if there are none or no valid answer."""
    if not names:
        print(f"(no {noun}s)")
        return None
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    try:
        answer = input(f"Which {noun}? [1-{len(names)}] ").strip()
    except EOFError:
        print()
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(names):
        return names[int(answer) - 1]
    if answer in names:
        return answer
    print(f"Cancelled: no such {noun}.")
    return None


def _name(args: argparse.Namespace, noun: str, names: Callable[[], list[str]]) -> str | None:
    """The NAME argument, or the user's pick from `names()` when it's omitted."""
    if args.name:
        return args.name
    if getattr(args, "from_stdin", False):
        raise AgentError(f"give the {noun}'s name: with --from-stdin, stdin holds the settings")
    return _pick(noun, names())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-admin", description=__doc__.splitlines()[0])
    groups = parser.add_subparsers(dest="group")
    pick_agent = "the agent's name (default: choose from a list)"
    pick_source = "the source's name (default: choose from a list)"
    no_confirm = "don't ask for confirmation"

    sub = groups.add_parser("agent", help="manage agents").add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="list agents")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="show one agent")
    p.add_argument("name", nargs="?", help=pick_agent)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("add", help="add an agent: issue its keys and create its Hermes home")
    p.add_argument("name")
    p.add_argument("--mcp", default="", help="comma-separated MCP servers to grant (default: none)")
    p.add_argument("--github", default="", help="comma-separated GitHub access levels to grant (default: none)")
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                   help=f"monthly spend limit in USD (default: {DEFAULT_BUDGET:g})")
    slack = p.add_mutually_exclusive_group()
    slack.add_argument("--slack", action="store_true", help="ask for Slack settings")
    slack.add_argument("--from-stdin", action="store_true",
                       help="read Slack settings as SLACK_*=... lines from stdin instead of asking")
    p = sub.add_parser("edit", help="replace an agent's MCP and/or GitHub access, or change its budget")
    p.add_argument("name", nargs="?", help=pick_agent)
    mcp = p.add_mutually_exclusive_group()
    mcp.add_argument("--mcp", help="comma-separated MCP servers")
    mcp.add_argument("--no-mcp", action="store_true", help="remove all MCP access")
    github = p.add_mutually_exclusive_group()
    github.add_argument("--github", help="comma-separated GitHub access levels")
    github.add_argument("--no-github", action="store_true", help="remove all GitHub access")
    p.add_argument("--budget", type=float, help="monthly spend limit in USD (default: unchanged)")
    p = sub.add_parser("slack", help="set an agent's Slack settings, asking for each")
    p.add_argument("name", nargs="?", help=pick_agent)
    p.add_argument("--from-stdin", action="store_true",
                   help="read the settings to change as SLACK_*=... lines from stdin instead of asking")
    p = sub.add_parser("rotate", help="replace an agent's and its workspace's keys")
    p.add_argument("name", nargs="?", help=pick_agent)
    p.add_argument("--yes", action="store_true", help=no_confirm)
    p = sub.add_parser("delete", help="revoke an agent's keys and remove its secrets")
    p.add_argument("name", nargs="?", help=pick_agent)
    p.add_argument("--purge", action="store_true", help="also delete the agent's data (sessions, memories, skills)")
    p.add_argument("--yes", action="store_true", help=no_confirm)
    p = sub.add_parser("board", help="set up or change an agent's board page (its key, MCP servers, budget)",
                       description="The board page's chat may only use `fast` and the read-only MCP servers "
                                   f"{', '.join(BOARD_MCP_SERVERS)}, on its own budget.")
    p.add_argument("name", nargs="?", help=pick_agent)
    board_mcp = p.add_mutually_exclusive_group()
    board_mcp.add_argument("--mcp", help=f"comma-separated read-only MCP servers ({', '.join(BOARD_MCP_SERVERS)}; "
                                         "default: unchanged, or none for a new page, also after --revoke)")
    board_mcp.add_argument("--no-mcp", action="store_true", help="remove all MCP access")
    p.add_argument("--budget", type=float,
                   help=f"monthly spend limit in USD (default: unchanged, or {DEFAULT_BOARD_BUDGET:g} for a new page)")
    p.add_argument("--revoke", action="store_true", help="revoke the page's key, keeping the boards and chats")
    p.add_argument("--yes", action="store_true", help=no_confirm + " (with --revoke)")
    p = sub.add_parser("compose", help="print the agent's service blocks for compose.yaml")
    p.add_argument("name")
    p.add_argument("--board-port", type=int, default=9119,
                   help="the board page's default host port (default: 9119; each page needs its own)")
    p = sub.add_parser("servers", help="list MCP servers agents can be granted")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("levels", help="list GitHub access levels agents can be granted")
    p.add_argument("--json", action="store_true")

    sub = groups.add_parser("onyx", help="configure Onyx, the search index behind the onyx_search MCP server",
                            ).add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup", help="log in (registering the administrator on a new Onyx) and point Onyx at LiteLLM")
    p.add_argument("--budget", type=float,
                   help=f"Onyx's monthly LiteLLM spend limit in USD (default on first setup: {DEFAULT_ONYX_BUDGET:g})")
    p.add_argument("--rotate-key", action="store_true", help="issue Onyx a new LiteLLM key and revoke the old one")
    sub.add_parser("token", help="print a new search token for ONYX_SEARCH_PAT, revoking the previous one")
    p = sub.add_parser("list", help="list Onyx's sources and their indexing state")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("add", help="add a source for Onyx to index, asking for its settings",
                       description="Source types: " + "; ".join(f"{k}: {t.description}" for k, t in SOURCE_TYPES.items())
                       + ". Onyx doesn't copy sources' permissions: everything the account can see becomes "
                         "searchable by every caller.")
    p.add_argument("type", choices=list(SOURCE_TYPES))
    p.add_argument("name", help="a name for the source in Onyx, e.g. wiki")
    p.add_argument("--refresh-hours", type=float,
                   help=f"how often Onyx re-indexes it (default: ask, or {DEFAULT_REFRESH_HOURS:g} with --from-stdin)")
    p.add_argument("--from-stdin", action="store_true",
                   help="read the settings as KEY=VALUE lines from stdin instead of asking (keys: see the prompts)")
    p.add_argument("--yes", action="store_true", help=no_confirm)
    p = sub.add_parser("errors", help="show a source's latest indexing attempts and open errors")
    p.add_argument("name", nargs="?", help=pick_source)
    p.add_argument("--trace", action="store_true", help="include failed attempts' full tracebacks")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("index", help="index a source now instead of waiting for its next refresh")
    p.add_argument("name", nargs="?", help=pick_source)
    p.add_argument("--full", action="store_true",
                   help="refetch everything, not just what changed since the last run (only changed documents "
                        "are re-embedded)")
    p = sub.add_parser("edit", help="change a source's settings, asking for each with its current value")
    p.add_argument("name", nargs="?", help=pick_source)
    p.add_argument("--refresh-hours", type=float,
                   help="how often Onyx re-indexes it (default: ask, or unchanged with --from-stdin)")
    p.add_argument("--from-stdin", action="store_true",
                   help="read the settings to change as KEY=VALUE lines from stdin instead of asking")
    p.add_argument("--yes", action="store_true", help=no_confirm)
    p = sub.add_parser("delete", help="delete a source, its documents in the index, and its credential")
    p.add_argument("name", nargs="?", help=pick_source)
    p.add_argument("--yes", action="store_true", help=no_confirm)
    p = sub.add_parser("cleanup", help="delete connectors and credentials no source uses (left by a failed add)")
    p.add_argument("--yes", action="store_true", help=no_confirm)
    return parser


def run_agent(args: argparse.Namespace, agents: Agents) -> int:
    command = args.command
    if command == "servers":
        servers = agents.mcp_servers()
        if args.json:
            print(json.dumps(servers, indent=2))
        else:
            unusable = agents.unusable_mcp_servers()
            print("\n".join(f"{s}  (unusable: check its credential)" if s in unusable else s for s in servers)
                  or "(no MCP servers configured)")
        return 0
    if command == "levels":
        levels = agents.github_levels()
        print(json.dumps(levels, indent=2) if args.json else "\n".join(levels) or "(no GitHub access levels configured)")
        return 0
    if command == "list":
        listed = agents.list()
        if args.json:
            print(json.dumps([_agent_json(a) for a in listed], indent=2))
        else:
            print("\n".join(_describe(a) for a in listed) or "(no agents)")
        return 0
    if command == "add":
        slack = _read_stdin(SLACK_VARS) if args.from_stdin else _prompt_slack(False) if args.slack else None
        agent = agents.create(args.name, _split(args.mcp), slack, _split(args.github), args.budget)
        _print_warnings(agents)
        print(_describe(agent))
        print(START_HINT.format(name=agent.name))
        return 0
    if command == "compose":
        name = validate_name(args.name)
        if not 1 <= args.board_port <= 65535:
            raise AgentError(f"invalid port {args.board_port}")
        fields = {"name": name, "var": name.upper().replace("-", "_"), "port": args.board_port}
        print(COMPOSE_SERVICE.format(**fields), end="")
        print(COMPOSE_STEPS.format(**fields), file=sys.stderr)
        return 0

    if command == "edit":
        mcp = [] if args.no_mcp else None if args.mcp is None else _split(args.mcp)
        github = [] if args.no_github else None if args.github is None else _split(args.github)
        if mcp is None and github is None and args.budget is None:
            raise AgentError("nothing to change: give --mcp/--no-mcp, --github/--no-github, and/or --budget")
    if command == "board":
        mcp = [] if args.no_mcp else None if args.mcp is None else _split(args.mcp)
        if args.revoke and (mcp is not None or args.budget is not None):
            raise AgentError("--revoke can't be combined with --mcp, --no-mcp, or --budget")
    name = _name(args, "agent", lambda: [a.name for a in agents.list()])
    if name is None:
        return 1
    if command == "show":
        agent = agents.get(name)
        print(json.dumps(_agent_json(agent), indent=2) if args.json else _describe(agent))
    elif command == "edit":
        agent = agents.grant(name, mcp, github, args.budget)
        _print_warnings(agents)
        print(_describe(agent))
        print(EDIT_HINT)
    elif command == "board":
        if args.revoke:
            if not args.yes and not _confirm(f"Revoke {name}'s board page key? Its chat stops working. [y/N]"):
                print(CANCELLED)
                return 1
            agents.revoke_board(name)
            print(BOARD_REVOKED.format(name=name))
            return 0
        had_page = bool(agents.get(name).board_key_hashes)
        agent = agents.board(name, mcp, args.budget)
        _print_warnings(agents)
        print(_describe(agent))
        print((BOARD_EDIT_HINT if had_page else BOARD_HINT).format(name=name))
    elif command == "slack":
        values = _read_stdin(SLACK_VARS) if args.from_stdin else _prompt_slack(True)
        if not values:
            print("Nothing changed.")
            return 0
        agents.set_slack(name, values)
        print(SLACK_HINT.format(name=name))
    elif command == "rotate":
        if not args.yes and not _confirm(f"Issue new keys for {name} and revoke the old ones? {ROTATE_WARNING} [y/N]"):
            print(CANCELLED)
            return 1
        agents.rotate(name)
        print(ROTATE_DONE.format(name=name))
        print(ROTATE_HINT.format(name=name))
    elif command == "delete":
        if not args.yes:
            prompt = (f"Delete {name} and ALL its data? Type the agent name to confirm:" if args.purge
                      else f"Revoke {name}'s keys and remove its secrets? [y/N]")
            if not _confirm(prompt, name if args.purge else "y"):
                print(CANCELLED)
                return 1
        agents.delete(name, purge=args.purge)
        print((PURGED if args.purge else DELETED).format(name=name))
    return 0


def run_onyx(args: argparse.Namespace, onyx: Onyx) -> int:
    command = args.command
    if command == "setup":
        print("\n".join(onyx.setup(args.budget, args.rotate_key)))
    elif command == "token":
        token = onyx.token()
        print("Put this in secrets/litellm.env, then restart LiteLLM (docker compose restart litellm).",
              file=sys.stderr)
        print("The previous token, if any, is revoked: search fails until LiteLLM restarts.", file=sys.stderr)
        print(f"ONYX_SEARCH_PAT={token}")
    elif command == "list":
        sources = onyx.sources()
        if args.json:
            print(json.dumps(sources, indent=2))
        else:
            print("\n".join(f"{s['name']} ({s['source']}): {s['status'] or 'unknown'}, {s['documents'] or 0} "
                            f"documents, last index {s['last_index'] or 'none'}, {_refresh_text(s['refresh_hours'])}"
                            for s in sources) or "(no sources)")
    elif command == "add":
        source_type = SOURCE_TYPES[args.type]
        values = _read_stdin([f.key for f in source_type.fields]) if args.from_stdin else _prompt_source(source_type)
        refresh_hours = args.refresh_hours
        if refresh_hours is None:
            refresh_hours = DEFAULT_REFRESH_HOURS if args.from_stdin else _prompt_refresh_hours(DEFAULT_REFRESH_HOURS)
        source_settings(args.type, args.name, values, refresh_hours)
        if not args.yes:
            print(f"Add {args.name} ({args.type}), re-indexed every {refresh_hours:g} hours:")
            _print_source(source_type, values)
            if not _confirm("Everything this account can see becomes searchable by every caller. Add it? [y/N]"):
                print(CANCELLED)
                return 1
        print(onyx.add(args.type, args.name, values, refresh_hours))
    elif command == "cleanup":
        connectors, credentials = onyx.unused()
        if not connectors and not credentials:
            print("Nothing to clean up.")
            return 0
        if not args.yes:
            print("Not used by any source:")
            for kind, items in (("connector", connectors), ("credential", credentials)):
                for item in items:
                    print(f"  {kind} {item['name']!r} ({item['source']})")
            if not _confirm("Delete them? [y/N]"):
                print(CANCELLED)
                return 1
        print("\n".join(onyx.cleanup(connectors, credentials)))
    else:
        name = _name(args, "source", lambda: [s["name"] for s in onyx.sources()])
        if name is None:
            return 1
        if command == "errors":
            report = onyx.errors(name)
            print(json.dumps(report, indent=2) if args.json else format_errors(report, args.trace))
        elif command == "index":
            if not onyx.index(name, args.full):
                print(INDEX_BUSY.format(name=name))
                return 1
            print(INDEX_QUEUED.format(what="a full re-index" if args.full else "indexing", name=name),
                  f"`onyx errors {name}` shows how it went.")
        elif command == "edit":
            kind, current, refresh_hours = onyx.source(name)
            source_type = SOURCE_TYPES[kind]
            values = {**current, **(_read_stdin([f.key for f in source_type.fields]) if args.from_stdin
                                    else _prompt_source(source_type, current))}
            if args.refresh_hours is not None:
                refresh_hours = args.refresh_hours
            elif not args.from_stdin:
                refresh_hours = _prompt_refresh_hours(refresh_hours)
            source_settings(kind, name, values, refresh_hours, keep_secrets=True)
            if not args.yes:
                print(f"Update {name} ({kind}), re-indexed every {refresh_hours:g} hours:")
                _print_source(source_type, values, blank_secret="(unchanged)")
                if not _confirm("Update it? [y/N]"):
                    print(CANCELLED)
                    return 1
            print(onyx.edit(name, values, refresh_hours))
        elif command == "delete":
            if not args.yes and not _confirm(f"Delete {name} and remove its documents from the index? [y/N]"):
                print(CANCELLED)
                return 1
            print(f"Deleting {name}; Onyx can take a few minutes to remove its documents...", flush=True)
            print(onyx.delete(name))
    return 0


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


def _prompt_refresh_hours(current: float) -> float:
    """Asks how often Onyx should re-index a source; Enter keeps `current`."""
    answer = input(f"Re-index every how many hours [{current:g}]: ").strip()
    if not answer:
        return current
    try:
        return float(answer)
    except ValueError:
        raise OnyxError(f"expected a number of hours, not {answer!r}") from None


def _refresh_text(hours: float | None) -> str:
    return f"re-indexed every {hours:g} hours" if hours else "never re-indexed on its own"


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


def _print_warnings(agents: Agents) -> None:
    for warning in agents.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        agents = Agents(Settings.from_env())
        if args.group == "onyx":
            return run_onyx(args, Onyx(OnyxSettings.from_env(), agents.api))
        if args.group is None:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                parser.print_help()
                return 2
            from .tui import AdminApp

            AdminApp(agents, Onyx(OnyxSettings.from_env(), agents.api)).run()
            return 0
        return run_agent(args, agents)
    except (AgentError, LiteLLMError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
