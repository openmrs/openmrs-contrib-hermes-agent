"""Follow-up instructions shown after a change, worded the same in the CLI and the interactive
interface. Each takes the agent's or source's name via `.format(name=...)`."""

START_HINT = ("Start it with: docker compose up -d agent-{name}. If compose.yaml has no agent-{name} service "
              "yet, see: docker compose run --rm admin agent compose {name}")
EDIT_HINT = "MCP and budget changes take effect immediately, GitHub changes within a minute."
SLACK_HINT = "Restart the agent to apply: docker compose restart agent-{name}"
ROTATE_DONE = "Issued new keys for {name} and its workspace and revoked the old ones."
ROTATE_HINT = ("Restart the agent and its workspace to apply: docker compose restart workspace-{name} agent-{name} "
               "(and its board page, if it has one: docker compose --profile boards restart board-{name})")
ROTATE_WARNING = "The agent stops working until it and its workspace are restarted."
BOARD_HINT = ("Start its board page with: docker compose --profile boards up -d board-{name}. If compose.yaml "
              "has no board-{name} service yet, see: docker compose run --rm admin agent compose {name}")
BOARD_EDIT_HINT = "MCP and budget changes take effect immediately."
BOARD_REVOKED = ("Revoked {name}'s board page key; its boards and chats are kept. Stop the page with: "
                 "docker compose --profile boards stop board-{name}")
DELETED = "Deleted {name} (data kept)."
PURGED = "Deleted {name} and its data."
INDEX_QUEUED = "Queued {what} of {name}."
INDEX_BUSY = "{name} is already being indexed; try again once that run finishes."
