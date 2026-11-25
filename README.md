# Hermes agent stack

Docker Compose stack that runs [Hermes Agent](https://github.com/NousResearch/hermes-agent) behind a
local [LiteLLM](https://github.com/BerriAI/litellm) gateway. LiteLLM holds every real credential (LLM
providers and MCP integrations such as Jira and Confluence); each agent holds only a LiteLLM virtual key
and its own Slack tokens. Everything an agent runs (its terminal, file tools, and code execution)
runs in its own workspace container, which holds none of those. A `github-token` service hands out
short-lived GitHub App tokens, and an on-demand admin container manages agents. A `reviewer` agent
reviews pull requests when GitHub's webhook starts it (see [PR reviewer](#pr-reviewer)).

```
                    agents network (internet egress)
 ┌───────────────┐   LLM: /v1/*            ┌───────────┐ ──▶ LLM providers
 │ agent-<name>  │ ──────────────────────▶ │  litellm  │
 │ LiteLLM key   │   MCP: /mcp/<server>    │ all real  │ ──▶ MCP servers (credentials
 │ Slack tokens  │ ──────────────────────▶ │  secrets  │     injected here)
 └───────┬───────┘                         └──┬─────┬──┘ ── atlassian network ──▶ mcp-atlassian
         │                                    │     │      (no credentials) ──▶ Jira, Confluence
         │ SSH (workspaces       litellm-db   │     │ admin network
         │ network, internal)    (internal)   │     │ (internal)
         ▼                              ┌─────┴───┐ ┌┴─────────────────┐
 ┌──────────────────┐                   │postgres │ │ admin (on demand)│
 │ workspace-<name> │                   └─────────┘ │ master key       │
 │ workspace key    │ ── gh ──▶ github-token (App keys) ──▶ api.github.com
 │ (GitHub only)    │           checks grants via litellm /key/info
 └──────────────────┘ ── workspace-egress ──▶ github.com, Maven Central, npm, ...

 GitHub ── webhook, HTTP :8644 (signed) ──▶ agent-reviewer ──▶ workspace-reviewer (browser)
                                            └─ github_review token ─▶ github-token ─▶ one PR review
```

## Security model

- **Credentials live only where they're used:**
  - `secrets/litellm.env` (provider, MCP, TypeSafe keys) → `litellm`.
  - `secrets/postgres.env` → `postgres`.
  - `secrets/master.env` (LiteLLM master key) → `litellm` and `admin`.
  - `secrets/github-app.env` (GitHub App private keys) → `github-token`.
  - `secrets/atlassian.env` (the Atlassian site) → `mcp-atlassian`, which holds no credentials:
    LiteLLM sends the granted server's Atlassian key with each request.
  - An agent's LiteLLM key, Slack tokens, and (reviewer only) webhook secret → its `.env` in its own `agents-data` subdirectory,
    which only it (and `admin`) mounts.
- **Agents run nothing themselves.** Hermes' `ssh` terminal backend sends the terminal, file tools,
  and `execute_code` to the agent's workspace, so building a pull request (running its author's
  code) can't read the agent's tokens. See [Workspaces](#workspaces).
- **Agents can't reach the database or admin.** Each is on an internal network shared only with
  `litellm`. Admin has no internet access and no Docker socket.
- **Agents can't change their instructions or configuration.** `SOUL.md` and `config.yaml` are
  read-only mounts of repo files; Hermes doesn't protect them itself, and `config.yaml` decides where
  requests go.
- **Runs are capped at 50 model calls** (`agent.max_turns`; 150 for the reviewer), so one runaway
  task can't spend the monthly budget.
- **Agent keys are narrowly scoped:** `allowed_routes` is chat completions, model listing, MCP
  inference, and `/key/info`; `models` is `auto`, `fast`, `balanced`, `strong`. So no management
  API (an agent can't mint a broader key) and no raw provider passthroughs (`/anthropic/*`, ...),
  which would bypass `auto`. Don't use LiteLLM's `llm_api_routes` group: it includes them.
- **Monthly budgets** ($1000 default) are on each agent's LiteLLM user, not its key, so `agent rotate`
  doesn't reset spend. Every request with the agent's key counts, including the router's classifier
  calls. Over budget, LiteLLM refuses requests until the 1st (UTC).
- **GitHub access is per level, with short-lived tokens.** `github-token/levels.json` defines each
  level's permissions and optional repository limit: `github_read`, `github_pr` (push branches,
  open pull requests), and `github_review` (post pull request reviews; agent keys only). Each level should have its own GitHub App, installed only where needed.
  - `gh` in the workspace asks `github-token` for a token on every run, sending the workspace's key.
    `github-token` checks the key with LiteLLM and issues a token only for a level in its
    `github_levels` metadata. Revoked keys get nothing; grant changes apply within a minute.
  - Tokens expire within an hour and aren't stored. To revoke a level outright, suspend its App.
  - Keys may call `/key/info`, but LiteLLM only answers about keys with the same `user_id`, and
    each agent has its own, so agents can't read each other's grants.
  - **`github_pr` is limited by GitHub, not this stack.** Two org rulesets (see [Setup](#setup))
    keep the App to `apollo/*` branches and away from tags, which also rules out merging. They don't
    cover the rest of Contents and Pull requests write: a prompt-injected agent, or code it runs,
    could still edit Releases, send `repository_dispatch`, and comment on, review, retitle, or close
    any pull request. Pushes to `apollo/*` run workflows that trigger on every branch, so check none
    pass secrets to build steps. Never give the App the Workflows permission.
  - **`github_review` is limited to one review by the reviewer's plugin, not by GitHub.** GitHub has
    no comment-only permission, so the App (Pull requests write, three repositories, no Contents)
    can also edit or close pull requests there. Only the `github-review` plugin limits the reviewer to
    one COMMENT review on the run's bound PR and head commit. `github-token` issues the level only to
    agent keys (never workspace keys).
- **MCP access is per server.** Each server in `litellm/config.yaml` has its own credential, and
  each key lists the servers it may use. Name servers `<integration>_<level>` with a separately
  scoped key each (e.g. `atlassian_read`, `atlassian_jira_write`, `atlassian_confluence_write`). 
- **Jira and Confluence writes need approval.** The Atlassian write servers are `trust: untrusted` in
  `hermes/config.yaml`, so Hermes asks in Slack before any tool not annotated read-only. Hermes
  enforces this, not LiteLLM, which is why workspaces don't get the agent's key.
- **Minimal privileges.** Every service has `no-new-privileges` and drops all capabilities, adding
  back only what startup needs. `github-token`, `admin`, and workspaces have read-only root
  filesystems. On SELinux hosts `admin` runs with `label=disable` so it can purge files agents copied
  from their image, which keep the agent's label.

### Workspaces

Each agent `<name>` has a `workspace-<name>` container (`workspace/`), which Hermes logs in to over
SSH as an unprivileged user. It has JDKs 8, 11, 17, and 21, Maven, Node with Yarn and pnpm, git,
and `gh`.

- **No agent secrets.** It holds its own SSH host key and a *workspace key*: a LiteLLM key that
  may only call `/key/info` (no models, no MCP servers) and carries the agent's GitHub grants.
- **Pinned and private.** The agent trusts only the host key admin wrote to its `ssh/known_hosts`,
  and the workspace accepts only the agent's key. The workspace's config is root-only.
- **Networks.** `workspaces` (internal: agents, workspaces, `github-token`) and `workspace-egress`
  (unrestricted internet). Workspaces can't reach LiteLLM or Postgres.
- **Reset when idle.** After `WORKSPACE_IDLE_MINUTES` (default 180, set per agent in
  `compose.yaml`) with no SSH session, the workspace kills all its user's processes and deletes
  their files. Only logged-in sessions count as activity, so a leftover process can't keep it
  alive. The Maven, npm, Yarn, and pnpm caches are shared by all workspaces and kept.
- **Permanent files.** With `WORKSPACE_KEEP_FILES: "true"` (per agent), a reset still kills
  processes but keeps `/work` (home, clones), including across restarts, though not when the
  container is recreated. Anything a malicious build planted there, such as a git hook, stays until
  removed. Tell that agent in its `SOUL.md` that its workspace isn't wiped.
- **Accepted risks.** Until the next reset, a malicious build's processes and files can tamper with
  later work in that workspace. Hermes copies skills to the workspace and back, so code there could
  plant a skill that appears in later prompts. The reviewer's skills are a read-only mount, so this
  can't happen to it (tested: a changed skill file and a new file next to it don't come back).

### PR reviewer

The `reviewer` agent reviews pull requests in `openmrs-esm-patient-chart`,
`openmrs-esm-patient-management`, and `openmrs-core`. GitHub's `pull_request` webhook (opened or
ready for review; not drafts or bots) hits a signed route on the agent, which starts a run. The run
checks the PR out in `workspace-reviewer`, builds and tests it and opens its UI in a browser if the
diff looks safe to build, and posts one COMMENT review.

- **Config and tools.** Its managed config (`hermes/reviewer/config.yaml`, read-only) turns off tool
  search (plugin tools are listed directly), memory, background review, the curator, and all MCP
  servers. Its skills (`hermes/reviewer/skills`) are a read-only mount, so `hermes skills list`
  fails for it. The `github-review` plugin gives it `prepare_workspace` and `post_pr_review`, and
  blocks a review's other tool calls until `prepare_workspace` has given it the workspace, and after
  another review has taken the workspace from it.
- **Profile.** `agent-reviewer` and `workspace-reviewer` are in the `reviewer` Compose profile, so a
  plain `docker compose up -d` doesn't start them. Commands that should include them, such as the
  update steps in [Upgrading](#upgrading), need `--profile reviewer` or `COMPOSE_PROFILES=reviewer`.
- **One review at a time.** `prepare_workspace` resets the workspace and takes a lock. A review waits
  up to 2 hours for it; a holder with no activity for 45 minutes loses it, and none keeps it over 3
  hours. A failed run doesn't fire `on_session_end`, so its lock is freed only once it goes stale
  and another review is waiting. `prepare_workspace` has its own tool deadline of about 2 hours.
  It refuses a PR and commit already reviewed.
- **Browser.** Only `openmrsinfra/hermes-workspace-reviewer` (the `reviewer` stage of
  `workspace/Dockerfile`) has Chromium and `agent-browser`. Chromium is pinned to a build from a
  `debian-security` snapshot; bump the pin periodically (see the Dockerfile) so it isn't behind
  security fixes.
- **Accepted risks.**
  - Webhook events arrive over plain HTTP, so anyone on the network path can read them (public PR
    data). The secret is never sent, only a signature over the body. The signature doesn't cover the
    delivery ID, so a captured body can be resent; each resend can start a run. If the first review
    of that PR and commit is done, `prepare_workspace` refuses the resend at once. If it still runs,
    the resend waits in `prepare_workspace` (up to 2 hours) and is refused when it gets the
    workspace. Hermes limits each route to 30 requests a minute.
  - The Hermes webhook server on 8644 is reachable from the internet and from every workspace on
    the `workspaces` network. Every route needs a valid signature.
  - The reviewer's Maven and npm cache (`reviewer-cache`) survives resets, so a PR build can poison
    it for later reviews.
  - Files a PR build writes next to the cache files Hermes syncs to the workspace can come back into
    the agent's `/opt/data/cache` and go to the next review's workspace. They are data there;
    nothing runs them.
  - Workspace egress is open.
  - `github_review` can also edit or close PRs in the three repositories (see
    [Security model](#security-model)).
- **Repository list.** It is in three places: `github-token/levels.json`, `ALLOWED_REPOS` in
  `hermes/plugins/github-review/__init__.py`, and the route filter in `hermes/reviewer/config.yaml`.
  Change all three together.

### Data retention

- **LiteLLM** keeps per-request metadata (key, model, tokens, cost) for 120 days
  (`maximum_spend_logs_retention_period`) and daily spend totals indefinitely. No prompts or
  replies.
- **Hermes** deletes a session (every message and tool call) 30 days after its last activity, but
  only once the session has ended (`sessions` in `hermes/config.yaml`). Slack sessions don't end
  when idle or when the agent restarts, so most are kept indefinitely.
- **Container logs** are capped at 30 MB per container.

## Prerequisites

- Docker Engine 26.0+ with Compose 2.24.4+ (volume subpaths, optional env files, the tests'
  `!override` tag), or Podman with Compose (tested on Podman 6 with `docker-compose` as provider).
- For the tests: `python3`, `openssl`, `shasum`, `base64`, and `git`.

Commands below use `docker compose`; with Podman, use `podman compose`. The `bin/` scripts pick
whichever is available.

## Setup

1. **Atlassian keys:** create an Atlassian service account with access to Jira and Confluence, and
   one API key on it per level (see [Atlassian keys](#atlassian-keys) for the scopes). Give the
   account only the Jira and Confluence permissions agents should have: keys can't be narrower than
   it in practice, and the account, not the key, decides which projects and spaces it can see and
   edit.
2. **Secrets:**
   ```sh
   cp secrets/litellm.env.example secrets/litellm.env
   cp secrets/postgres.env.example secrets/postgres.env
   cp secrets/master.env.example secrets/master.env
   cp secrets/atlassian.env.example secrets/atlassian.env
   chmod 600 secrets/*.env
   # Fill in all four. The Postgres password must match in DATABASE_URL and POSTGRES_PASSWORD.
   ```
   `TYPESAFE_API_KEY` is optional. Each `ATLASSIAN_*_API_KEY` is only needed if an agent will be
   granted that server; admin refuses to grant one without it.
3. **Models:** set `LLM_MODEL_FAST`, `LLM_MODEL_BALANCED`, `LLM_MODEL_STRONG`, and the provider's
   key in `secrets/litellm.env`. Agents use `auto`, which picks a tier per request:

   | Tier | Model |
   |---|---|
   | SIMPLE | `fast` |
   | MEDIUM, COMPLEX | `balanced` |
   | REASONING | `strong` |

   The classifiers in `ROUTER_CLASSIFIERS` are tried in order (default Jev, then an LLM call to
   `ROUTER_CLASSIFIER_MODEL`); if none answers, LiteLLM's heuristic decides. Jev is called as the
   `jev-router` model, which agent keys can't use, and is skipped without a TypeSafe key. Hermes'
   side tasks (titles, compression, ...) always use `fast`. Prompt caches are per model, so a turn
   routed to a different tier starts cold.
4. **GitHub (optional):** for `github_read`, create a GitHub App (no webhook) with these Repository
   permissions, all Read-only: Actions, Artifact metadata, Checks, Code quality, Commit statuses,
   Contents, Deployments, Pull requests. Install it on the `openmrs` org and fill in
   `secrets/github-app.env` from the example. Permissions in `github-token/levels.json` must match
   the App's. Private keys go in as one line of base64 (remove the example's empty line first):
   ```sh
   printf 'GITHUB_READ_PRIVATE_KEY_BASE64=%s\n' "$(base64 < your-app.private-key.pem | tr -d '\n')" \
     >> secrets/github-app.env
   ```

   For `github_pr` (optional), an org owner first creates two org rulesets on all repositories,
   with org admins, the Admin, Maintain, and Write roles, and any release deploy keys as bypass
   actors, but not the App:
   - branches: all except `apollo/**`; Restrict creations, updates, and deletions;
   - tags: all; the same three rules.

   Then create a second App with Read and write Contents and Pull requests (never Workflows),
   install it on `openmrs`, and add its `GITHUB_PR_*` values. Check with a scratch repository that
   its token can push to `apollo/test` but not `main`, and can't create a tag.

   Commits are authored as the App's bot, set in the [host settings](#host-settings):
   `GIT_BOT_NAME=<app-slug>[bot]` and `GIT_BOT_EMAIL=<user-id>+<app-slug>[bot]@users.noreply.github.com`,
   with the ID from `gh api 'users/<app-slug>[bot]' --jq .id`.
5. **Create the agent:**
   ```sh
   bin/admin agent add default --mcp atlassian_jira_write,atlassian_confluence_write --github github_read
   ```
   This starts LiteLLM and Postgres if needed. Add `github_pr` to `--github` to let it open pull
   requests.
6. **Slack:** each agent needs its own Slack app. Generate its manifest:
   ```sh
   docker compose run --rm --no-deps -T -e HERMES_HOME=/tmp/hermes \
     --entrypoint /opt/hermes/.venv/bin/hermes agent-default slack manifest --name Apollo > slack-manifest.json
   ```
   (This skips the image's startup output and uses a throwaway Hermes home; run as root against
   `/opt/data`, Hermes would leave files the agent can't write.)

   At https://api.slack.com/apps, choose Create New App → From an app manifest. Before installing,
   remove scopes and events the agent shouldn't have, e.g. private channels (`groups:*`,
   `message.groups`) or group DMs (`mpim:*`, `message.mpim`). Install it, copy the Bot User OAuth
   Token (`xoxb-...`), create an app-level token with `connections:write` (`xapp-...`), then:
   ```sh
   bin/admin agent slack default
   ```
   `SLACK_ALLOWED_USERS` is a comma-separated list of Slack member IDs, or `*` for everyone. Empty
   means no one.
7. **Start:** `docker compose up -d`

LiteLLM's admin UI is at http://127.0.0.1:4000/ui (host-only); log in with the master key.

### PR reviewer setup

Optional; needs the `github_read` App from step 4 above.

1. **GitHub App:** create "Apollo Review" with Repository permissions Pull requests Read and write
   (GitHub adds Metadata read) and no Contents: the reviewer reads code with `github_read`.
   Webhook URL `http://<server address>:8644/webhooks/github-pr`, secret from step 3, event
   "Pull request". Install it only on the three repositories.
2. **Secrets:** add the App's `GITHUB_REVIEW_*` values to `secrets/github-app.env` (see the example).
3. **Create the agent and secret:**
   ```sh
   bin/admin agent add reviewer --github github_read,github_review --budget <USD>
   bin/admin agent webhook reviewer     # prints the webhook secret once
   ```
   Paste the secret into the App.
4. **Start:** open port 8644 in the server's firewall, then
   `docker compose --profile reviewer up -d agent-reviewer workspace-reviewer`. Later commands that
   should include the reviewer (such as the update in [Upgrading](#upgrading)) need
   `--profile reviewer`, or set `COMPOSE_PROFILES=reviewer` in `./.env`.
5. **Test:** open a PR in one of the three repositories and watch
   `docker compose logs -f agent-reviewer`.

### Atlassian keys

Keys are scoped when created and can't be changed, so create each with all of its scopes. An
operation accepts either its classic scope or every one of its granular scopes; `mcp-atlassian` uses
both kinds (Confluence's v2 API takes only granular scopes).

Each write server only has its own product's tools (an agent that needs both products' writes gets
both write servers), so each write key only needs its product's scopes:

| Scope set | Scopes |
|---|---|
| Jira read | Classic: `read:jira-work`, `read:jira-user` |
| Confluence read | Classic: `read:confluence-content.all`, `read:confluence-content.summary`, `read:confluence-space.summary`, `read:confluence-props`, `read:confluence-user`, `search:confluence`. Granular: `read:space:confluence`, `read:page:confluence`, `read:hierarchical-content:confluence`, `read:comment:confluence`, `read:attachment:confluence` |
| Jira write | Granular: `write:issue:jira`, `write:issue.property:jira`, `write:comment:jira`, `write:comment.property:jira`, `write:attachment:jira`, `write:issue-link:jira`, `write:issue.remote-link:jira`, `read:issue.remote-link:jira`, `read:issue:jira`, `read:comment:jira`, `read:comment.property:jira`, `read:group:jira`, `read:project:jira`, `read:project-role:jira`, `read:user:jira`, `read:avatar:jira` |
| Confluence write | Granular: `write:page:confluence`, `write:comment:confluence`. Classic: `write:confluence-content`, `write:confluence-props` |

| Key | Scope sets |
|---|---|
| `ATLASSIAN_READ_API_KEY` | Jira read, Confluence read |
| `ATLASSIAN_JIRA_WRITE_API_KEY` | Jira read, Jira write |
| `ATLASSIAN_CONFLUENCE_WRITE_API_KEY` | Confluence read, Confluence write |

- **Never `write:jira-work`.** That classic scope also allows deleting issues; the granular Jira
  write scopes above don't, so the Jira write key can't delete whatever the account may do.
- **Never `delete:*` or `manage:*` scopes**, or any `*:agent-interface` scope (those are for
  Atlassian's own Rovo MCP server, which this stack doesn't use).
- **Check a key** with the public endpoints: `myself` should name the service account, not answer
  401, e.g.
  `curl -H "Authorization: Bearer $KEY" https://api.atlassian.com/ex/jira/<cloud ID>/rest/api/3/myself`.
  An invalid key reads public content as an anonymous user, so a successful read proves nothing.

### Host settings

Read from the environment or a `.env` file next to `compose.yaml` (git-ignored). None are secrets.

| Variable | Default | Purpose |
|---|---|---|
| `LITELLM_HOST_PORT` | `4000` | Host port for LiteLLM's admin UI, on 127.0.0.1 |
| `ONYX_UI_HOST_PORT` | `3000` | Host port for Onyx's web UI, on 127.0.0.1 |
| `HERMES_UID`, `HERMES_GID` | `10000` | User and group agents run as and own their files |
| `GIT_BOT_NAME`, `GIT_BOT_EMAIL` | (none) | Author of agents' commits (the `github_pr` App's bot). Commits fail until both are set |

## Managing agents

- `bin/admin [command ...]` runs the admin tool; with no arguments, its interactive interface.
- `bin/agent <name> [hermes arguments]` runs the Hermes CLI in `agent-<name>`, e.g.
  `bin/agent default config get memory`.

They use `docker compose`, else `podman compose`, else `docker-compose`; set `COMPOSE` to override.

```sh
bin/admin agent servers                         # grantable MCP servers, marking unusable ones
bin/admin agent levels                          # grantable GitHub levels
bin/admin agent list
bin/admin agent show [<name>]
bin/admin agent add <name> --mcp a,b [--github x,y] [--budget USD] [--slack]
bin/admin agent edit [<name>] --mcp a,b         # or --no-mcp; immediate
bin/admin agent edit [<name>] --github x,y      # or --no-github; within a minute
bin/admin agent edit [<name>] --budget USD      # immediate
bin/admin agent slack [<name>]                  # prompts; blank keeps current values
bin/admin agent rotate [<name>] [--yes]         # new agent, workspace, and SSH keys; old keys revoked
bin/admin agent delete [<name>] [--purge] [--yes]
bin/admin agent compose <name>                  # agent-<name> and workspace-<name> blocks for compose.yaml
bin/admin agent webhook [<name>] [--yes]        # new webhook secret, printed once (reviewer)

bin/admin onyx setup [--budget USD] [--rotate-key]  # point Onyx at LiteLLM with its own key
bin/admin onyx token                            # new ONYX_SEARCH_PAT for secrets/litellm.env
bin/admin onyx list                             # sources, their indexing state, and refresh intervals
bin/admin onyx errors [<name>] [--trace]        # a source's latest indexing attempts and open errors
bin/admin onyx index [<name>] [--full]          # index a source now; --full refetches everything
bin/admin onyx add <type> <name> [--refresh-hours H] [--yes]
bin/admin onyx edit [<name>] [--refresh-hours H] [--yes]
bin/admin onyx delete [<name>] [--yes]
bin/admin onyx cleanup [--yes]                  # connectors and credentials a failed add left behind
```

- **Names are optional** where shown in brackets: without one, admin lists the agents or sources and
  asks which.
- **Changes ask for confirmation** where `--yes` is shown; `--yes` skips it, e.g. in scripts.
- **`--json`** on `list`, `show`, `servers`, `levels`, and `onyx errors` prints machine-readable output.
- **Onyx's web UI** is at `http://127.0.0.1:3000` (`ONYX_UI_HOST_PORT`); log in with
  `ONYX_ADMIN_EMAIL` and `ONYX_ADMIN_PASSWORD` from `secrets/onyx-admin.env`. Use it to look into
  indexing beyond what `onyx errors` shows and to browse what's indexed, but manage sources and
  models with admin: the UI can also change them, which admin doesn't expect.
- **Indexing logs** are in `docker compose logs onyx-background` (fetching, embedding, indexing).

- **LiteLLM is the source of truth.** An agent is the set of keys tagged `managed_by: hermes-admin`;
  other keys are ignored. Budgets and spend are on the LiteLLM user `hermes-agent-<name>`.
- **Unusable MCP servers are refused** (e.g. an empty credential). A server that's only down
  upstream is granted with a warning.
- **Secrets never go on the command line.** Slack tokens come from masked prompts or, for scripts,
  `SLACK_*=...` lines on stdin (`agent add <name> --from-stdin`, `agent slack <name> --from-stdin`); Onyx
  source settings likewise (`onyx add ... --from-stdin`, `onyx edit <name> --from-stdin`).
- **Restarts:** Slack changes need `docker compose restart agent-<name>`; `agent rotate`
  needs `docker compose restart workspace-<name> agent-<name>`. Admin has no Docker socket to do it.
- **`agent delete`** revokes the keys and removes the agent's secrets (`.env` values including
  `WEBHOOK_SECRET`, SSH key, workspace config) but keeps its sessions, memories, skills, and spend, so `agent add` can bring it back.
  `--purge` also deletes its data and LiteLLM user.

To add an agent: add the output of `bin/admin agent compose <name>` under `services:` in `compose.yaml`,
write `hermes/souls/<name>.md`, run `bin/admin agent add <name> ...`, set up its Slack app (Setup
step 6), then `docker compose up -d agent-<name>`.

To remove one: delete its blocks from `compose.yaml`, run `docker compose up -d --remove-orphans`,
then `bin/admin agent delete <name> --purge` (or without `--purge` to keep its data), and delete its
`SOUL.md` and Slack app.

To add an MCP server: add it to `litellm/config.yaml` (credential in `secrets/litellm.env`) and add
a matching `http://litellm:4000/mcp/<server>` entry with `strict_redirect_headers: true` to
`hermes/config.yaml`, then restart the agents.

### Agent identity (SOUL.md)

`hermes/souls/<name>.md`, mounted read-only as Hermes' `SOUL.md`, is sent with every request, so
keep it short. Hermes reads it when a session starts: use `/new` in Slack, or restart the agent.

## Upgrading

Images are pinned by tag and digest, in `compose.yaml` and the Dockerfiles. Change both, using the
digest of the tag's multi-platform index (`docker buildx imagetools inspect <image>:<tag>`), run
`tests/run.sh`, then `docker compose build && docker compose up -d` (with the reviewer, add
`--profile reviewer` to both, or set `COMPOSE_PROFILES=reviewer`).

- **Hermes:** `HERMES_IMAGE` and `image:` under `x-hermes-agent`. Follow the checklist in
  `hermes/config.yaml`: new side tasks must be routed to `fast`, and `_config_version` must match
  the image, because agents can't write their `config.yaml`.
- **LiteLLM:** the `litellm` image. `litellm/tier_classifier.py` depends on its internals (tested).
- **mcp-atlassian:** the `mcp-atlassian` image.
- **Onyx:** `x-onyx-backend` and `onyx-web` together. Check that the web UI still logs in: it
  reaches `onyx-api` through a route Onyx calls development-only (see `onyx-web`).
- **Python:** the base image in the `admin/`, `github-token/`, and `workspace/` Dockerfiles.
- **Workspace toolchain:** the JDK, Node, Maven, and GitHub CLI pins in `workspace/Dockerfile`.
  The reviewer's Chromium pin (a `debian-security` snapshot) needs a periodic bump too.
- **Postgres:** a major version also needs `pg_upgrade` or a dump and restore; the volume is
  mounted at `/var/lib/postgresql` so `pg_upgrade --link` works.

Never change `LITELLM_SALT_KEY` after first start: it encrypts credentials LiteLLM stores.

## Images and releases

GitHub Actions publishes images for `linux/amd64` and `linux/arm64` to Docker Hub:
`openmrsinfra/hermes` (`hermes/image`), `openmrsinfra/hermes-admin`,
`openmrsinfra/hermes-github-token`, `openmrsinfra/hermes-workspace`, and
`openmrsinfra/hermes-workspace-reviewer`.

- **Pushes to `main`** publish `:nightly` and `:<short sha>` once `tests/run.sh` passes. Pull
  requests only run the tests.
- **Releases:** run the Release workflow from `main` with `patch`, `minor`, `major`, or an exact
  version (e.g. `1.2.0-beta.1`). It runs the tests, publishes `:<version>` (and `:latest` for the
  newest stable), tags `v<version>`, and creates a GitHub Release listing merged pull requests.

`compose.yaml` builds images locally; the published ones are for deployments that pull them.

## Checking the setup

```sh
# Only LITELLM_API_KEY and SLACK_* should be listed
docker compose exec agent-default sh -c 'cut -d= -f1 /opt/data/.env; env | cut -d= -f1' | grep -E "KEY|TOKEN|SECRET|PASS"
# Lists the server's tools if granted, fails otherwise
bin/agent default mcp test atlassian_read
# Should fail with "Virtual key is not allowed to call this route"
docker compose exec agent-default sh -c \
  '. /opt/data/.env; curl -s -X POST -H "Authorization: Bearer $LITELLM_API_KEY" http://litellm:4000/key/generate'
```

## Tests

`tests/run.sh` checks the stack's security properties and the parts that depend on LiteLLM and
Hermes internals; its header lists what it covers. Run it after changing any image, `compose.yaml`,
`litellm/`, `hermes/`, `admin/`, `github-token/`, or `workspace/`.

```sh
tests/run.sh                          # uses "docker compose"
COMPOSE=docker-compose tests/run.sh   # e.g. with Podman
```

It uses fake credentials and fake model, Jev, Atlassian, Anthropic, and GitHub endpoints, runs as a
separate Compose project that cleans up after itself, and takes about 5 minutes. CI runs it on every
pull request and push to `main` that changes more than `README.md`.
