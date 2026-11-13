"""Interactive interface for hermes-admin, built on Textual.

Every change goes through a dialog that shows what will happen and must be confirmed. LiteLLM and
filesystem work runs in worker threads so the interface stays responsive.
"""

from __future__ import annotations

from typing import Any, Callable

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, SelectionList, Static

from .agents import (DEFAULT_BUDGET, SLACK_REQUIRED_VARS, SLACK_TOKEN_VARS, SLACK_VARS, Agent, AgentError, Agents,
                     validate_budget, validate_name)
from .litellm import LiteLLMError
from .onyx import DEFAULT_REFRESH_HOURS, SOURCE_TYPES, Onyx, OnyxError, source_settings

DIALOG_CSS = """
ModalScreen { align: center middle; }
#dialog { width: 72; height: auto; max-height: 90%; overflow-y: auto; border: thick $accent; background: $surface; padding: 1 2; }
#dialog Label.title { text-style: bold; margin-bottom: 1; }
#dialog SelectionList { height: auto; max-height: 12; margin-bottom: 1; }
#dialog Input { margin-bottom: 1; }
#dialog .buttons { height: auto; align-horizontal: right; }
#dialog .buttons Button { margin-left: 1; }
#dialog .error { color: $error; }
"""


class _Dialog(ModalScreen):
    """Base for dialogs: Escape cancels (dismisses with None)."""

    DEFAULT_CSS = DIALOG_CSS
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _buttons(self, confirm: str, variant: str = "primary") -> Horizontal:
        return Horizontal(Button("Cancel", id="cancel"), Button(confirm, id="confirm", variant=variant), classes="buttons")

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def _budget(self) -> float | None:
        """The #budget input as USD, or None (with the error shown) if it isn't a positive amount."""
        try:
            return validate_budget(float(self.query_one("#budget", Input).value.strip().lstrip("$")))
        except (ValueError, AgentError):
            self.query_one("#error", Static).update("Budget must be a positive amount in USD.")
            return None


class CreateDialog(_Dialog):
    """Name, MCP and GitHub grants, monthly budget, and optional Slack settings for a new agent."""

    def __init__(self, servers: list[str], levels: list[str]) -> None:
        super().__init__()
        self._servers = servers
        self._levels = levels

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Create agent", classes="title")
            yield Input(placeholder="agent name (lowercase letters, digits, dashes)", id="name")
            yield Label("MCP servers (space to toggle):")
            yield SelectionList[str](*[(s, s) for s in self._servers], id="mcp")
            yield Label("GitHub access levels:")
            yield SelectionList[str](*[(level, level) for level in self._levels], id="github")
            yield Label("Monthly budget (USD):")
            yield Input(value=f"{DEFAULT_BUDGET:g}", id="budget")
            yield Label("Slack (optional; can be set later):")
            yield Input(placeholder="SLACK_BOT_TOKEN", password=True, id="SLACK_BOT_TOKEN")
            yield Input(placeholder="SLACK_APP_TOKEN", password=True, id="SLACK_APP_TOKEN")
            yield Input(placeholder="SLACK_ALLOWED_USERS (comma-separated member IDs)", id="SLACK_ALLOWED_USERS")
            yield Input(placeholder="SLACK_HOME_CHANNEL (channel ID)", id="SLACK_HOME_CHANNEL")
            yield Static("", classes="error", id="error")
            yield self._buttons("Create")

    @on(Button.Pressed, "#confirm")
    def _on_confirm(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        try:
            validate_name(name)
        except AgentError as e:
            self.query_one("#error", Static).update(str(e))
            return
        budget = self._budget()
        if budget is None:
            return
        slack = {var: self.query_one(f"#{var}", Input).value.strip() for var in SLACK_VARS}
        self.dismiss({
            "name": name,
            "budget": budget,
            "mcp": list(self.query_one("#mcp", SelectionList).selected),
            "github": list(self.query_one("#github", SelectionList).selected),
            "slack": {k: v for k, v in slack.items() if v},
        })


class GrantDialog(_Dialog):
    def __init__(self, agent: Agent, servers: list[str], levels: list[str]) -> None:
        super().__init__()
        self._agent = agent
        self._servers = servers
        self._levels = levels

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Access for {self._agent.name}", classes="title")
            yield Label("MCP servers (space to toggle; takes effect immediately):")
            yield SelectionList[str](*[(s, s, s in self._agent.mcp_servers) for s in self._servers], id="mcp")
            yield Label("GitHub access levels (takes effect within a minute):")
            yield SelectionList[str](
                *[(level, level, level in self._agent.github_levels) for level in self._levels], id="github")
            yield Label(f"Monthly budget (USD; currently {self._agent.describe_budget()}):")
            yield Input(value=f"{self._agent.budget if self._agent.budget is not None else DEFAULT_BUDGET:g}", id="budget")
            yield Static("", classes="error", id="error")
            yield self._buttons("Save")

    @on(Button.Pressed, "#confirm")
    def _on_confirm(self) -> None:
        budget = self._budget()
        if budget is None:
            return
        self.dismiss({
            "budget": budget,
            "mcp": list(self.query_one("#mcp", SelectionList).selected),
            "github": list(self.query_one("#github", SelectionList).selected),
        })


class SlackDialog(_Dialog):
    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self._agent = agent

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Slack settings for {self._agent.name}", classes="title")
            yield Label("Blank fields keep their current value. The agent must be restarted afterwards.")
            for var in SLACK_VARS:
                state = "set" if self._agent.env_set.get(var) else "not set"
                yield Input(placeholder=f"{var} ({state})", password=var in SLACK_TOKEN_VARS, id=var)
            yield self._buttons("Save")

    @on(Button.Pressed, "#confirm")
    def _on_confirm(self) -> None:
        values = {var: self.query_one(f"#{var}", Input).value.strip() for var in SLACK_VARS}
        self.dismiss({k: v for k, v in values.items() if v})


class ConfirmDialog(_Dialog):
    """Yes/no confirmation; with `type_to_confirm`, the user must type that text first."""

    def __init__(self, title: str, message: str, confirm: str, type_to_confirm: str | None = None) -> None:
        super().__init__()
        self._title, self._message, self._confirm, self._expected = title, message, confirm, type_to_confirm

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, classes="title")
            yield Label(self._message)
            if self._expected:
                yield Input(placeholder=f"type {self._expected} to confirm", id="typed")
            yield self._buttons(self._confirm, variant="error")

    def on_mount(self) -> None:
        self.query_one("#confirm", Button).disabled = bool(self._expected)

    @on(Input.Changed, "#typed")
    def _on_typed(self, event: Input.Changed) -> None:
        self.query_one("#confirm", Button).disabled = event.value != self._expected

    @on(Button.Pressed, "#confirm")
    def _on_confirm(self) -> None:
        self.dismiss(True)


class SourceTypeDialog(_Dialog):
    """Chooses the kind of Onyx source to add; dismisses with its SOURCE_TYPES key."""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add an Onyx source", classes="title")
            for kind, source_type in SOURCE_TYPES.items():
                yield Button(f"{kind}: {source_type.description}", id=f"type-{kind}")
            yield Horizontal(Button("Cancel", id="cancel"), classes="buttons")

    @on(Button.Pressed)
    def _on_type(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("type-"):
            self.dismiss(event.button.id.removeprefix("type-"))


class SourceDialog(_Dialog):
    """An Onyx source's name, settings, and refresh interval; dismisses with them once
    source_settings accepts them. With `name` and `current` (from Onyx.source), edits that
    source: fields start at their current values and blank tokens keep the stored ones."""

    def __init__(self, kind: str, name: str | None = None, current: dict[str, Any] | None = None,
                 refresh_hours: float = DEFAULT_REFRESH_HOURS) -> None:
        super().__init__()
        self._kind, self._name, self._current, self._refresh_hours = kind, name, current or {}, refresh_hours
        self._type = SOURCE_TYPES[kind]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Edit {self._name} ({self._kind})" if self._name else f"Add a {self._kind} source",
                        classes="title")
            yield Label("Everything its account can see becomes searchable by every caller: use an account "
                        "that sees only public content.")
            if not self._name:
                yield Input(placeholder="name in Onyx, e.g. wiki", id="name")
            for f in self._type.fields:
                if f.kind == "bool":
                    yield Checkbox(f.label, value=self._current.get(f.key, f.default), id=f.key)
                elif f.secret and self._name:
                    yield Input(placeholder=f"{f.label} (blank keeps the current one)", password=True, id=f.key)
                else:
                    yield Input(value=str(self._current.get(f.key, "")), placeholder=f.label, password=f.secret,
                                id=f.key)
            yield Label("Re-index every (hours):")
            yield Input(value=f"{self._refresh_hours:g}", id="refresh-hours")
            yield Static("", classes="error", id="error")
            yield self._buttons("Save" if self._name else "Add")

    @on(Button.Pressed, "#confirm")
    def _on_confirm(self) -> None:
        values = {f.key: self.query_one(f"#{f.key}", Checkbox).value if f.kind == "bool"
                  else self.query_one(f"#{f.key}", Input).value for f in self._type.fields}
        name = self._name or self.query_one("#name", Input).value.strip()
        try:
            refresh_hours = float(self.query_one("#refresh-hours", Input).value.strip())
            source_settings(self._kind, name, values, refresh_hours, keep_secrets=bool(self._name))
        except ValueError:
            self.query_one("#error", Static).update("Re-index interval must be a number of hours.")
            return
        except OnyxError as e:
            self.query_one("#error", Static).update(str(e))
            return
        self.dismiss({"name": name, "values": values, "refresh_hours": refresh_hours})


class OnyxScreen(Screen):
    """Onyx's sources and their indexing state; adds and edits sources through SourceTypeDialog and
    SourceDialog, and deletes the selected one."""

    BINDINGS = [
        Binding("a", "add", "Add source"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("f5", "refresh", "Refresh"),
        Binding("escape", "app.pop_screen", "Back"),
    ]
    COLUMNS = ("Source", "Type", "Status", "Documents", "Last index")

    def __init__(self, onyx: Onyx) -> None:
        super().__init__()
        self.onyx = onyx

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="sources", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#sources", DataTable).add_columns(*self.COLUMNS)
        self.action_refresh()

    @work(thread=True, exclusive=True, group="onyx-refresh")
    def action_refresh(self) -> None:
        try:
            sources = self.onyx.sources()
        except (AgentError, LiteLLMError) as e:
            self.app.call_from_thread(self.notify, str(e), title="Onyx refresh failed", severity="error")
            return
        self.app.call_from_thread(self._show, sources)

    def _show(self, sources: list[dict]) -> None:
        table = self.query_one("#sources", DataTable)
        # Keeps the selection across refreshes, so e and d act on the source the user picked.
        selected = self._selected()
        table.clear()
        for s in sources:
            table.add_row(s["name"], s["source"], s["status"] or "", str(s["documents"] or 0), s["last_index"] or "none",
                          key=s["name"])
        if selected in {s["name"] for s in sources}:
            table.move_cursor(row=table.get_row_index(selected))

    def action_add(self) -> None:
        def chosen(kind: str | None) -> None:
            if kind:
                self.app.push_screen(SourceDialog(kind), lambda result: added(kind, result))

        def added(kind: str, result: dict | None) -> None:
            if result:
                self.run_worker(lambda: self._add(kind, result), thread=True, group="onyx-change")

        self.app.push_screen(SourceTypeDialog(), chosen)

    def _add(self, kind: str, result: dict) -> None:
        self._change("Add source", lambda: self.onyx.add(kind, result["name"], result["values"],
                                                         result["refresh_hours"]))

    def _change(self, label: str, change: Callable[[], str]) -> None:
        """Runs `change` (in a worker thread), then reports its result and refreshes the table."""
        try:
            done = change()
        except (AgentError, LiteLLMError) as e:
            self.app.call_from_thread(self.notify, str(e), title=f"{label} failed", severity="error", timeout=10)
            return
        self.app.call_from_thread(self.notify, done, title=label, timeout=8)
        self.app.call_from_thread(self.action_refresh)

    def _selected(self) -> str | None:
        table = self.query_one("#sources", DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def action_edit(self) -> None:
        name = self._selected()
        if name:
            self.run_worker(lambda: self._load_for_edit(name), thread=True, group="onyx-change")

    def _load_for_edit(self, name: str) -> None:
        try:
            kind, current, refresh_hours = self.onyx.source(name)
        except (AgentError, LiteLLMError) as e:
            self.app.call_from_thread(self.notify, str(e), title="Edit source failed", severity="error", timeout=10)
            return

        def edited(result: dict | None) -> None:
            if result:
                self.run_worker(lambda: self._change("Edit source", lambda: self.onyx.edit(
                    name, result["values"], result["refresh_hours"])), thread=True, group="onyx-change")

        self.app.call_from_thread(self.app.push_screen, SourceDialog(kind, name, current, refresh_hours), edited)

    def action_delete(self) -> None:
        name = self._selected()
        if not name:
            return

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.notify("Onyx can take a few minutes to remove its documents.", title=f"Deleting {name}")
                self.run_worker(lambda: self._change("Delete source", lambda: self.onyx.delete(name)),
                                thread=True, group="onyx-change")

        self.app.push_screen(ConfirmDialog(f"Delete {name}?", "Removes its documents from the index; agents can't "
                                           "find them until it's added again.", "Delete"), confirmed)


class AdminApp(App):
    TITLE = "Hermes admin"
    BINDINGS = [
        Binding("n", "create", "New"),
        Binding("g", "grant", "Access"),
        Binding("s", "slack", "Slack"),
        Binding("r", "rotate", "Rotate key"),
        Binding("d", "delete", "Delete"),
        Binding("o", "onyx", "Onyx sources"),
        Binding("f5", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]
    COLUMNS = ("Agent", "Status", "MCP servers", "GitHub", "Budget", "Slack", "Data")
    AGENT_ACTIONS = {"create", "grant", "slack", "rotate", "delete", "refresh", "onyx"}

    def __init__(self, agents: Agents, onyx: Onyx | None = None) -> None:
        super().__init__()
        self.agents = agents
        self.onyx = onyx
        self._listed: dict[str, Agent] = {}
        self._servers: list[str] = []
        self._levels: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="agents", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#agents", DataTable).add_columns(*self.COLUMNS)
        self.action_refresh()

    # -- data ----------------------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="refresh")
    def action_refresh(self) -> None:
        try:
            listed, servers, levels = self.agents.list(), self.agents.mcp_servers(), self.agents.github_levels()
        except (AgentError, LiteLLMError) as e:
            self.call_from_thread(self.notify, str(e), title="Refresh failed", severity="error")
            return
        self.call_from_thread(self._show, listed, servers, levels)

    def _show(self, listed: list[Agent], servers: list[str], levels: list[str]) -> None:
        self._listed = {a.name: a for a in listed}
        self._servers = servers
        self._levels = levels
        table = self.query_one("#agents", DataTable)
        table.clear()
        for a in listed:
            required_set = [a.env_set.get(var) for var in SLACK_REQUIRED_VARS]
            any_set = any(a.env_set.get(var) for var in SLACK_VARS)
            table.add_row(
                a.name,
                "active" if len(a.key_hashes) == 1 else f"{len(a.key_hashes)} keys" if a.key_hashes else "no key",
                ", ".join(a.mcp_servers) or "none",
                ", ".join(a.github_levels) or "none",
                f"${a.spend:,.2f} / ${a.budget:,.0f}" if a.budget is not None else "none",
                "set" if all(required_set) else "partial" if any_set else "not set",
                "present" if a.has_data else "none",
                key=a.name,
            )

    def _selected(self) -> Agent | None:
        table = self.query_one("#agents", DataTable)
        if not table.row_count:
            self.notify("No agent selected.", severity="warning")
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return self._listed.get(key)

    def _run(self, label: str, change: Callable[[], object], done: str) -> None:
        """Run a change in a worker thread, then report and refresh."""

        def job() -> None:
            self.agents.warnings = []
            try:
                change()
            except (AgentError, LiteLLMError) as e:
                self.call_from_thread(self.notify, str(e), title=f"{label} failed", severity="error", timeout=10)
                return
            self.call_from_thread(self.notify, done, title=label, timeout=8)
            for warning in self.agents.warnings:
                self.call_from_thread(self.notify, warning, title=label, severity="warning", timeout=10)
            self.call_from_thread(self.action_refresh)

        self.run_worker(job, thread=True, group="change")

    # -- actions -------------------------------------------------------------------------------

    def action_create(self) -> None:
        def created(result: dict | None) -> None:
            if result:
                self._run("Create", lambda: self.agents.create(
                    result["name"], result["mcp"], result["slack"] or None, result["github"], result["budget"]),
                          f"Created {result['name']}. Start it with: docker compose up -d agent-{result['name']}")

        self.push_screen(CreateDialog(self._servers, self._levels), created)

    def action_grant(self) -> None:
        agent = self._selected()
        if not agent:
            return

        def granted(result: dict | None) -> None:
            if result is not None:
                self._run("Access", lambda: self.agents.grant(
                    agent.name, result["mcp"], result["github"], result["budget"]),
                          f"{agent.name}: MCP {', '.join(sorted(result['mcp'])) or 'none'}; "
                          f"GitHub {', '.join(sorted(result['github'])) or 'none'}; "
                          f"budget ${result['budget']:,.2f}/month.")

        self.push_screen(GrantDialog(agent, self._servers, self._levels), granted)

    def action_slack(self) -> None:
        agent = self._selected()
        if not agent:
            return

        def saved(values: dict | None) -> None:
            if values:
                self._run("Slack", lambda: self.agents.set_slack(agent.name, values),
                          f"Saved. Restart it with: docker compose restart agent-{agent.name}")

        self.push_screen(SlackDialog(agent), saved)

    def action_rotate(self) -> None:
        agent = self._selected()
        if not agent:
            return

        def confirmed(ok: bool | None) -> None:
            if ok:
                self._run("Rotate", lambda: self.agents.rotate(agent.name),
                          f"New keys issued. Restart with: docker compose restart workspace-{agent.name} agent-{agent.name}")

        self.push_screen(ConfirmDialog(
            f"Rotate {agent.name}'s key?",
            "New keys are issued for the agent and its workspace, and the old ones revoked. The agent stops "
            "working until both are restarted.",
            "Rotate"), confirmed)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # The agent actions act on the agent table, which isn't shown under the Onyx screen.
        if action in self.AGENT_ACTIONS and any(isinstance(screen, OnyxScreen) for screen in self.screen_stack):
            return False
        return True

    def action_onyx(self) -> None:
        if self.onyx is None:
            self.notify("Onyx isn't configured.", severity="warning")
            return
        self.push_screen(OnyxScreen(self.onyx))

    def action_delete(self) -> None:
        agent = self._selected()
        if not agent:
            return

        def keep_data(ok: bool | None) -> None:
            if ok:
                self._run("Delete", lambda: self.agents.delete(agent.name), f"Deleted {agent.name} (data kept).")

        def purge(ok: bool | None) -> None:
            if ok:
                self._run("Delete", lambda: self.agents.delete(agent.name, purge=True),
                          f"Deleted {agent.name} and its data.")

        def chosen(choice: str | None) -> None:
            if choice == "keep":
                self.push_screen(ConfirmDialog(
                    f"Delete {agent.name}?",
                    "Revokes its key and removes its secrets. Sessions, memories, and skills are kept.",
                    "Delete"), keep_data)
            elif choice == "purge":
                self.push_screen(ConfirmDialog(
                    f"Delete {agent.name} and all its data?",
                    "Revokes its key and permanently deletes its sessions, memories, and skills.",
                    "Delete everything", type_to_confirm=agent.name), purge)

        self.push_screen(DeleteChoiceDialog(agent), chosen)


class DeleteChoiceDialog(_Dialog):
    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self._agent = agent

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Delete {self._agent.name}", classes="title")
            yield Label("Keep the agent's data (sessions, memories, skills) so it can be re-created later?")
            yield Horizontal(
                Button("Cancel", id="cancel"),
                Button("Delete, keep data", id="keep", variant="warning"),
                Button("Delete everything", id="purge", variant="error"),
                classes="buttons",
            )

    @on(Button.Pressed, "#keep")
    def _keep(self) -> None:
        self.dismiss("keep")

    @on(Button.Pressed, "#purge")
    def _purge(self) -> None:
        self.dismiss("purge")
