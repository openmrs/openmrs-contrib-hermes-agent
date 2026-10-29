"""Interactive interface for hermes-admin, built on Textual.

Every change goes through a dialog that shows what will happen and must be confirmed. LiteLLM and
filesystem work runs in worker threads so the interface stays responsive.
"""

from __future__ import annotations

from typing import Callable

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, SelectionList, Static

from .agents import DEFAULT_BUDGET, SLACK_VARS, Agent, AgentError, Agents, validate_budget, validate_name
from .litellm import LiteLLMError

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
                yield Input(placeholder=f"{var} ({state})", password=var != "SLACK_ALLOWED_USERS", id=var)
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


class AdminApp(App):
    TITLE = "Hermes admin"
    BINDINGS = [
        Binding("n", "create", "New"),
        Binding("g", "grant", "Access"),
        Binding("s", "slack", "Slack"),
        Binding("r", "rotate", "Rotate key"),
        Binding("d", "delete", "Delete"),
        Binding("f5", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]
    COLUMNS = ("Agent", "Status", "MCP servers", "GitHub", "Budget", "Slack", "Data")

    def __init__(self, agents: Agents) -> None:
        super().__init__()
        self.agents = agents
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
            slack_set = [a.env_set.get(var) for var in SLACK_VARS]
            table.add_row(
                a.name,
                "active" if len(a.key_hashes) == 1 else f"{len(a.key_hashes)} keys" if a.key_hashes else "no key",
                ", ".join(a.mcp_servers) or "none",
                ", ".join(a.github_levels) or "none",
                f"${a.spend:,.2f} / ${a.budget:,.0f}" if a.budget is not None else "none",
                "set" if all(slack_set) else "partial" if any(slack_set) else "not set",
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
                          f"New key issued. Restart it with: docker compose restart agent-{agent.name}")

        self.push_screen(ConfirmDialog(
            f"Rotate {agent.name}'s key?",
            "A new key is issued and the old one revoked. The agent stops working until it is restarted.",
            "Rotate"), confirmed)

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
