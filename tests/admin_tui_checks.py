"""Headless checks of the hermes-admin interactive interface, used by tests/run.sh.

Runs inside the admin container against the test LiteLLM. Drives the Textual app with its test
pilot: the agent table loads, an agent can be created through the dialog with MCP and GitHub
grants and a budget, its access and budget can be changed (and an invalid budget is refused), and it
can be deleted (with the type-to-confirm purge). Cleans up the agent it creates.
"""

import asyncio
import sys

from hermes_admin.agents import Agents, Settings
from hermes_admin.tui import AdminApp, GrantDialog
from textual.widgets import DataTable, Input, SelectionList

NAME = "tui-check"
failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}: {name}{'' if ok else f' ({detail})'}")
    if not ok:
        failures.append(name)


async def settle(app, pilot):
    # A finished change starts a refresh worker, so wait until no worker is left running.
    while True:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not any(worker.is_running for worker in app.workers):
            return


def table_names(app):
    table = app.query_one("#agents", DataTable)
    return [table.get_row_at(i)[0] for i in range(table.row_count)]


def select_row(app, name):
    table = app.query_one("#agents", DataTable)
    table.move_cursor(row=table.get_row_index(name))


async def main():
    agents = Agents(Settings.from_env())
    app = AdminApp(agents)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        names = table_names(app)
        check("agent table lists existing agents", "default" in names, names)

        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#name", Input).value = NAME
        app.screen.query_one("#mcp", SelectionList).select("rovo_read")
        app.screen.query_one("#github", SelectionList).select("github_read")
        app.screen.query_one("#budget", Input).value = "25"
        # The dialog is taller than the test terminal, so this also checks it scrolls to its buttons.
        app.screen.query_one("#confirm").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#confirm")
        await settle(app, pilot)
        created = {a.name: a for a in agents.list()}.get(NAME)
        check("create dialog issues a key with the chosen grants",
              created is not None and created.provisioned and created.mcp_servers == ["rovo_read"]
              and created.github_levels == ["github_read"] and created.budget == 25,
              created)
        check("table shows the new agent", NAME in table_names(app), table_names(app))

        select_row(app, NAME)
        await pilot.press("g")
        await pilot.pause()
        selection = app.screen.query_one("#mcp", SelectionList)
        selection.deselect("rovo_read")
        selection.select("rovo_jira_write")
        app.screen.query_one("#github", SelectionList).deselect("github_read")
        budget = app.screen.query_one("#budget", Input)
        check("grant dialog shows the current budget", budget.value == "25", budget.value)
        budget.value = "not money"
        app.screen.query_one("#confirm").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        check("grant dialog refuses an invalid budget", isinstance(app.screen, GrantDialog), app.screen)
        budget.value = "30"
        # The error message pushes the buttons down, out of view again. A Textual button also
        # ignores clicks during its 0.2s press animation.
        app.screen.query_one("#confirm").scroll_visible(animate=False)
        await pilot.pause(0.3)
        await pilot.click("#confirm")
        await settle(app, pilot)
        granted = agents.get(NAME)
        check("grant dialog replaces MCP and GitHub access and the budget",
              granted.mcp_servers == ["rovo_jira_write"] and granted.github_levels == [] and granted.budget == 30,
              (granted.mcp_servers, granted.github_levels, granted.budget))

        select_row(app, NAME)
        await pilot.press("d")
        await pilot.pause()
        await pilot.click("#purge")
        await pilot.pause()
        confirm = app.screen.query_one("#confirm")
        check("purge needs the agent name typed first", confirm.disabled)
        await pilot.click("#typed")
        await pilot.press(*NAME)
        await pilot.pause()
        check("typing the name enables purge", not confirm.disabled)
        await pilot.click("#confirm")
        await settle(app, pilot)
        check("purge removes the agent", NAME not in {a.name for a in agents.list()})


try:
    asyncio.run(main())
finally:
    agents = Agents(Settings.from_env())
    if NAME in {a.name for a in agents.list()}:
        agents.delete(NAME, purge=True)
sys.exit(1 if failures else 0)
