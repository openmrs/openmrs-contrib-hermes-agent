"""Headless checks of the hermes-admin interactive interface, used by tests/run.sh.

Runs in the admin container against the test LiteLLM, driving the Textual app with its test pilot.
Cleans up the agent it creates. The tests don't run Onyx, so its screen talks to FakeOnyxClient.
"""

import asyncio
import sys

from hermes_admin.agents import Agents, Settings
from hermes_admin.onyx import Onyx, OnyxSettings
from hermes_admin.tui import AdminApp, GrantDialog, OnyxScreen, SourceDialog
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


class FakeOnyxClient:
    """Answers the Onyx API calls the Onyx screen makes, recording the changes."""

    def __init__(self):
        self.sources = [{"name": "o3-docs", "source": "web"}]
        self.calls = []

    def call(self, method, path, body=None, form=None, ok=(200, 201, 204)):
        if method == "GET" and path == "/manage/admin/connector/status":
            return [{"name": s["name"], "cc_pair_id": i, "connector": {"source": s["source"]}}
                    for i, s in enumerate(self.sources)]
        if method == "GET" and path.startswith("/manage/admin/cc-pair/"):
            return {"status": "ACTIVE", "num_docs_indexed": 3, "last_index_attempt_status": "success"}
        self.calls.append((method, path, body))
        if path == "/manage/admin/connector":
            self.sources.append({"name": body["name"], "source": body["source"]})
        return {"id": len(self.calls)}


def onyx_cleanup_checks(agents):
    client = FakeOnyxClient()
    responses = {
        "/manage/connector": [{"id": 1, "name": "o3-docs", "source": "web", "credential_ids": [1]},
                              {"id": 2, "name": "wiki", "source": "confluence", "credential_ids": []}],
        "/manage/admin/credential": [{"id": 0, "name": "", "source": "ingestion_api"},
                                     {"id": 1, "name": "o3-docs", "source": "web"},
                                     {"id": 2, "name": "wiki", "source": "confluence"}],
    }
    call = client.call
    client.call = lambda method, path, *a, **k: responses[path] if method == "GET" and path in responses \
        else call(method, path, *a, **k)
    onyx = Onyx(OnyxSettings.from_env(), agents.api, client)
    connectors, credentials = onyx.unused()
    check("onyx cleanup finds only the unused connector and credential",
          [c["id"] for c in connectors] == [2] and [c["id"] for c in credentials] == [2], (connectors, credentials))
    onyx.cleanup(connectors, credentials)
    check("onyx cleanup deletes them", client.calls == [("DELETE", "/manage/admin/connector/2", None),
                                                        ("DELETE", "/manage/admin/credential/2", None)], client.calls)


async def onyx_checks(agents):
    client = FakeOnyxClient()
    app = AdminApp(agents, Onyx(OnyxSettings.from_env(), agents.api, client))
    async with app.run_test(size=(120, 50)) as pilot:
        await settle(app, pilot)
        await pilot.press("o")
        await settle(app, pilot)
        table = app.screen.query_one("#sources", DataTable)
        check("onyx screen lists Onyx's sources", isinstance(app.screen, OnyxScreen) and table.row_count == 1,
              app.screen)
        await pilot.press("n")
        await pilot.pause()
        check("agent shortcuts are off on the onyx screen", isinstance(app.screen, OnyxScreen), app.screen)

        await pilot.press("a")
        await pilot.pause()
        await pilot.click("#type-github")
        await pilot.pause()
        dialog = app.screen
        dialog.query_one("#name", Input).value = "github"
        dialog.query_one("#repo_owner", Input).value = "openmrs"
        dialog.query_one("#repositories", Input).value = "openmrs-core, openmrs-esm-core"
        dialog.query_one("#confirm").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        check("source dialog refuses a missing token", isinstance(app.screen, SourceDialog) and not client.calls,
              client.calls)
        dialog.query_one("#github_access_token", Input).value = "github-test-token"
        dialog.query_one("#confirm").scroll_visible(animate=False)
        await pilot.pause(0.3)
        await pilot.click("#confirm")
        await settle(app, pilot)
        bodies = {path: body for _, path, body in client.calls}
        credential, connector = bodies.get("/manage/credential") or {}, bodies.get("/manage/admin/connector") or {}
        check("source dialog creates the credential with the token",
              credential.get("credential_json") == {"github_access_token": "github-test-token"}, credential)
        check("source dialog creates a polled connector with the chosen scope",
              connector.get("input_type") == "poll" and connector.get("connector_specific_config") == {
                  "repo_owner": "openmrs", "repositories": "openmrs-core, openmrs-esm-core", "include_files": True,
                  "include_prs": True, "include_issues": False}, connector)
        check("onyx screen shows the new source", app.screen.query_one("#sources", DataTable).row_count == 2)


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
        app.screen.query_one("#mcp", SelectionList).select("atlassian_read")
        app.screen.query_one("#github", SelectionList).select("github_read")
        app.screen.query_one("#budget", Input).value = "25"
        # The dialog is taller than the test terminal, so this also checks it scrolls to its buttons.
        app.screen.query_one("#confirm").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#confirm")
        await settle(app, pilot)
        created = {a.name: a for a in agents.list()}.get(NAME)
        check("create dialog issues a key with the chosen grants",
              created is not None and created.provisioned and created.mcp_servers == ["atlassian_read"]
              and created.github_levels == ["github_read"] and created.budget == 25,
              created)
        check("table shows the new agent", NAME in table_names(app), table_names(app))

        select_row(app, NAME)
        await pilot.press("g")
        await pilot.pause()
        selection = app.screen.query_one("#mcp", SelectionList)
        selection.deselect("atlassian_read")
        selection.select("atlassian_jira_write")
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
              granted.mcp_servers == ["atlassian_jira_write"] and granted.github_levels == [] and granted.budget == 30,
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

    await onyx_checks(agents)
    onyx_cleanup_checks(agents)


try:
    asyncio.run(main())
finally:
    agents = Agents(Settings.from_env())
    if NAME in {a.name for a in agents.list()}:
        agents.delete(NAME, purge=True)
sys.exit(1 if failures else 0)
