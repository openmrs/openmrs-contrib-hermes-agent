# Hermes agent stack

Docker Compose stack that runs [Hermes Agent](https://github.com/NousResearch/hermes-agent) behind a
local [LiteLLM](https://github.com/BerriAI/litellm) gateway. LiteLLM holds every real credential
(LLM providers and MCP integrations such as Atlassian Rovo); agents only hold a LiteLLM virtual key
and their own Slack tokens. A small `github-token` service gives agents short-lived GitHub App
tokens for the access levels they've been granted. A `sandbox` service builds and tests openmrs
repositories, including pull requests, away from agents' credentials. An on-demand admin container
manages agents' keys and access.

```
                    agents network (has internet egress)
 ┌───────────────┐   LLM: /v1/*            ┌───────────┐ ──▶ LLM providers
 │ agent-<name>  │ ──────────────────────▶ │  litellm  │
 │ LITELLM key   │   MCP: /mcp/<server>    │ all real  │ ──▶ MCP servers, e.g. mcp.atlassian.com
 │ Slack tokens  │ ──────────────────────▶ │  secrets  │     (credentials injected here)
 └───────┬───────┘                         └──┬─────┬──┘
         │ own subdirectory of     litellm-db │     │ admin network
         │ the agents-data volume  (internal) │     │ (internal)
         ▼                              ┌─────┴───┐ ┌┴────────────────┐
   agents-data volume ◀──────────────── │postgres │ │ admin (on demand)│
   (admin mounts all of it)             └─────────┘ │ master key       │
                                                    └──────────────────┘
 agent-<name> ── gh: GET /token/<level> ──▶ github-token (App keys) ──▶ api.github.com
                   (LiteLLM key)             checks grants via litellm /key/info
 agent-<name> ── MCP: /mcp/sandbox ──▶ litellm ── sandbox network ──▶ sandbox ──▶ github.com,
                                                                     (no secrets)  Maven Central, npm
```

## Security model

- **Credentials live only in the containers that use them:**
  - `secrets/litellm.env` (provider, MCP, and TypeSafe keys) → `litellm` only.
  - `secrets/postgres.env` → `postgres` only.
  - `secrets/master.env` (LiteLLM's master key) → `litellm` and the on-demand `admin` container.
  - `secrets/github-app.env` (GitHub App private keys) → `github-token` only.
  - Each agent's LiteLLM key and Slack tokens → that agent's own `.env`, in its own subdirectory
    of the `agents-data` volume. Each agent mounts only its subdirectory; only `admin` mounts the
    whole volume.
- **Agents can't reach the database or the admin container.** Postgres and admin are each on an
  internal network shared only with `litellm`. The admin container has no internet access and
  no Docker socket.
- **Agents can't change their own instructions or configuration.** Each agent's `SOUL.md` and
  `config.yaml` are read-only mounts of files in this repo. Hermes doesn't gate writes to either
  in its own home, so a writable copy could be rewritten by any Slack user, or by injected text in
  content the agent reads; `config.yaml` decides where the agent sends requests. Hermes commands
  that change config (e.g. `/model --global`) fail.
- **Each run is capped at 50 model calls** (`agent.max_turns` in `hermes/config.yaml`), so a
  runaway task can't use up the agent's monthly budget.
- **Agent keys are narrowly scoped.** The admin tool creates each virtual key with:
  - an explicit `allowed_routes` list: chat completions, model listing, LiteLLM's MCP
    inference routes, and `/key/info` (see GitHub access below);
  - `models` limited to `auto`, `fast`, `balanced`, and `strong`.

  So an agent can't call LiteLLM's management API (`/key/*`, `/model/*`, MCP server
  create/update/delete, ...), and so can't mint itself a broader key. It also can't use LiteLLM's
  raw provider passthroughs (`/anthropic/*`, `/openai/*`, ...), which would bypass `auto`.
  Avoid LiteLLM's `llm_api_routes` group here: it includes those passthroughs.
- **Each agent has a monthly budget** ($1000 unless set otherwise), on its LiteLLM user rather
  than its key, so `rotate` doesn't reset it. Every request made with the agent's key counts,
  including the `auto` router's classifier calls. Once the month's spend reaches the budget,
  LiteLLM refuses the agent's requests until spend resets on the 1st of the month (UTC).
- **GitHub access is granted per level, and tokens are short-lived.** Access levels are defined
  in `github-token/levels.json`: each names its token permissions and, optionally, the
  repositories it's limited to: `github_read` (read-only) and `github_pr` (push branches and open
  pull requests). Each level should have its own GitHub App, installed only where that level needs
  access; GitHub caps every token at its App's permissions.
  - **Tokens are per run.** An agent's `gh` sends its LiteLLM key to `github-token` on every run.
    `github-token` checks the key with LiteLLM and only issues a token for a level in the key's
    `github_levels` metadata, which the admin tool manages. Revoked keys get nothing, and grant
    changes take effect within a minute.
  - **Tokens expire within an hour**, and no GitHub credential is stored in the agent.
  - **Revoking a level** is suspending or uninstalling its App.
  - **Agent keys may call `/key/info`** so `github-token` can read their grants. LiteLLM only
    answers a non-admin key about itself or keys with the same `user_id`, and each agent's keys
    get their own `user_id`, so agents can't read each other's key info.
  - **`github_pr` pushes are limited by GitHub, not by this stack.** The agent gets an ordinary
    token with Contents and Pull requests write, and Hermes doesn't ask before using it. Two org
    rulesets (see [Setup](#setup)) stop the App from pushing anywhere but `apollo/*` branches, or
    creating, moving, or deleting tags; that includes merging, which updates the base branch.
    Rulesets don't cover the rest of what those permissions allow, so a prompt-injected agent
    could still edit or delete GitHub Releases, send `repository_dispatch` events, and comment on,
    review, retitle, or close any pull request. Pushes to `apollo/*` also run any workflow that
    triggers on pushes to every branch, with whatever secrets it exposes, so check that none hand
    secrets to build steps. The App must not have the Workflows permission, which keeps the agent
    from changing workflow files.
- **MCP access is granted per server.** Each MCP server in `litellm/config.yaml` holds its own
  credential, and each agent's key is granted an explicit list of servers (possibly none). Name
  servers `<integration>_<level>` and give each level its own, separately scoped service account,
  e.g. `rovo_read`, `rovo_jira_write`, and `rovo_confluence_write` (each write account can read
  both products but write only to its own). The provider-side scopes are the real boundary: an
  agent granted `rovo_read` can't write to Atlassian, and one granted `rovo_confluence_write`
  can't write to Jira, even if LiteLLM were misconfigured.
  `hermes/config.yaml` has one entry per server, at LiteLLM's `/mcp/<server>` path, and LiteLLM
  refuses the ones an agent's key wasn't granted.
- **Jira and Confluence writes need an administrator's approval.** The Rovo write servers are
  `trust: untrusted` in `hermes/config.yaml`, so Hermes asks in Slack before running any of their
  tools that isn't annotated read-only, and blocks it if no one approves. Text in an issue, page,
  or pull request therefore can't make the agent edit Jira or Confluence unseen.
- **Builds run in the sandbox, never in an agent.** Building a pull request runs its author's
  code; in an agent's container that code could read the agent's Slack and LiteLLM tokens. The
  `sandbox` service (granted per key as the `sandbox` MCP server) holds no secrets and mounts no
  agent data, so builds there run without approval. It is reachable only from LiteLLM, clones
  only repositories in the openmrs GitHub organization, and runs one job at a time as an
  unprivileged user on a read-only root filesystem. When a job ends, it kills the job's processes
  and deletes the files it left, except the shared Maven, npm, Yarn, and pnpm caches: a
  malicious build could poison those and skew later results, but there is nothing in them to
  steal. In the agents, `approvals.deny` in `hermes/config.yaml` blocks build and package-manager
  commands (`mvn`, `yarn`, `npm`, `npx`, `node`, ...) outright; a script file could still get
  round that, so `SOUL.md` also sends builds to the sandbox.
- **Containers run with minimal privileges.** Every service has `no-new-privileges` and drops all
  Linux capabilities, adding back only what its startup needs (e.g. to hand files to its own
  user). `github-token` and `admin` also have read-only root filesystems. On SELinux hosts,
  `admin` runs unconfined (`label=disable`): files an agent copies from its image keep that
  agent's own SELinux label, and admin must be able to read and purge them.

### Data retention

- **LiteLLM** keeps per-request metadata (key, model, tokens, cost) for 120 days
  (`maximum_spend_logs_retention_period` in `litellm/config.yaml`), and daily spend totals
  indefinitely. It doesn't store prompts or replies.
- **Hermes** keeps each agent's sessions (every message and tool call) until 30 days after they
  end (`sessions` in `hermes/config.yaml`). An agent's `MEMORY.md` notes are kept until removed;
  `SOUL.md` limits what goes in them.
- **Container logs** are capped at 30 MB per container.

## Prerequisites

- Docker Engine 26.0 or later with Docker Compose 2.24.4 or later (for volume subpaths, optional
  env files, and the tests' `!override` tag), or Podman with Compose. The stack has been run on
  Podman 6 with `docker-compose` as its Compose provider.
- For the tests only: `python3`, `openssl`, `shasum`, `base64`, and `git` on the host.

Commands below use `docker compose`; with Podman, use `podman compose`. The `bin/` scripts pick
whichever is available (see [Managing agents](#managing-agents)).

## Setup

1. **MCP service accounts:** for Rovo, an Atlassian org admin must enable API-token authentication
   for the Rovo MCP server (Atlassian Administration → Rovo → Rovo MCP server → Authentication).
   Then create one service account and API key per access level: read-only for both Jira and
   Confluence, read both plus write Jira, and read both plus write Confluence.
2. **Secrets:**
   ```sh
   cp secrets/litellm.env.example secrets/litellm.env
   cp secrets/postgres.env.example secrets/postgres.env
   cp secrets/master.env.example secrets/master.env
   chmod 600 secrets/*.env
   # Fill in all three. The Postgres password must match in DATABASE_URL and POSTGRES_PASSWORD.
   ```
   In `secrets/litellm.env`, `TYPESAFE_API_KEY` is optional, and each `ROVO_*_API_KEY` is only
   needed if an agent will be granted that server (the admin tool refuses to grant one without).
3. **Models:** set `LLM_MODEL_FAST`, `LLM_MODEL_BALANCED`, and `LLM_MODEL_STRONG` in
   `secrets/litellm.env`, along with that provider's key. Agents use `auto` (set in
   `hermes/config.yaml`), which picks a model for each request:

   | Tier | Model |
   |---|---|
   | SIMPLE | `fast` |
   | MEDIUM, COMPLEX | `balanced` |
   | REASONING | `strong` |

   The tier is chosen by the classifiers in `ROUTER_CLASSIFIERS`, tried in order (default Jev,
   then an LLM call to `ROUTER_CLASSIFIER_MODEL`). If none of them answers, LiteLLM's local
   heuristic scorer decides. Jev is called through LiteLLM's Decisions API, as the `jev-router`
   model in `litellm/config.yaml`, which agents' keys can't use. Jev is skipped when
   `TYPESAFE_API_KEY` is empty and `jev-router` sets no `api_key` of its own. See the security
   model above for what Jev sends to TypeSafe.

   Hermes' side tasks (titles, compression, vision, and so on) always use `fast` directly. Hermes
   treats `auto` as having a 512k-token context window.

   LiteLLM adds prompt-cache markers to requests for providers that need them (e.g. Anthropic).
   The cache is per model, so a turn the router sends to a different tier than the last one
   starts with a cold cache.
4. **GitHub (optional):** for the `github_read` level, create a GitHub App with no webhook and these
   Repository permissions, all Read-only: Actions, Artifact metadata, Checks, Code quality,
   Commit statuses, Contents, Deployments, Pull requests (Metadata is added automatically).
   Install it on the `openmrs` org, generate a private key, and fill in `secrets/github-app.env`
   from `secrets/github-app.env.example`. The permissions in `github-token/levels.json` must
   match the App's, or GitHub refuses the token. Without the App, everything but `gh` works.

   For the `github_pr` level (optional), an org owner first creates two org rulesets that apply
   to all repositories, each with org admins and the Admin, Maintain, and Write repository roles
   (and any deploy keys release tooling pushes with) as bypass actors, but not the App:
   - a branch ruleset targeting all branches except `apollo/**`, with Restrict creations,
     Restrict updates, and Restrict deletions;
   - a tag ruleset targeting all tags, with the same three rules.

   Then create a second App with Read and write Contents and Pull requests (never Workflows),
   install it on the `openmrs` org, and add its `GITHUB_PR_*` values to `secrets/github-app.env`.
   Before granting it, check with a scratch repository that the App's token can push to
   `apollo/test` but not to `main`, and can't create a tag.

   Commits are authored as the App's bot account, set in the [host settings](#host-settings):
   `GIT_BOT_NAME=<app-slug>[bot]` and
   `GIT_BOT_EMAIL=<user-id>+<app-slug>[bot]@users.noreply.github.com`, with the bot's user ID
   from `gh api 'users/<app-slug>[bot]' --jq .id`.
5. **Create the agent:**
   ```sh
   bin/admin create default --mcp rovo_jira_write,rovo_confluence_write,sandbox --github github_read
   ```
   This starts LiteLLM and Postgres if they aren't running. An agent can't start until `create`
   has made its directory in the `agents-data` volume. To let it open pull requests, add
   `github_pr` to `--github`.
6. **Slack:** give each agent its own Slack app. Generate the app's manifest from Hermes:
   ```sh
   docker compose run --rm --no-deps -T -e HERMES_HOME=/tmp/hermes \
     --entrypoint /opt/hermes/.venv/bin/hermes agent-default slack manifest --name Apollo > slack-manifest.json
   ```
   This skips the image's startup script, whose output would end up in the file, and uses a
   throwaway Hermes home. Don't point it at `/opt/data`: run as root there, Hermes leaves files the
   agent can't write, and the agent then fails to start.

   At https://api.slack.com/apps, choose Create New App → From an app manifest and paste it. The
   manifest uses Socket Mode, so the agent needs no public URL. It asks for Hermes' default
   scopes and events; before installing, remove those for anything this agent shouldn't see,
   e.g. private channels (`groups:*`, `message.groups`) or group DMs (`mpim:*`,
   `message.mpim`). Install the app, copy its Bot User OAuth Token (`xoxb-...`), and create an
   app-level token with the `connections:write` scope (`xapp-...`). Then:
   ```sh
   bin/admin slack default
   ```
   `SLACK_ALLOWED_USERS` is a comma-separated list of the Slack member IDs the agent answers
   (e.g. `U01ABC2DEF3`), or `*` for everyone in the workspace. If it's empty, the agent answers
   no one.
7. **Start everything:**
   ```sh
   docker compose up -d
   ```

The LiteLLM admin UI is at http://127.0.0.1:4000/ui (host-only); log in with the master key.

### Host settings

These are read from the environment or from a `.env` file next to `compose.yaml` (ignored by
git). None of them are secrets.

| Variable | Default | Purpose |
|---|---|---|
| `LITELLM_HOST_PORT` | `4000` | Host port for LiteLLM's admin UI, bound to 127.0.0.1 |
| `HERMES_UID`, `HERMES_GID` | `10000` | User and group agents run as, and that own their files in `agents-data` |
| `GIT_BOT_NAME`, `GIT_BOT_EMAIL` | (none) | Author and committer of agents' commits: the `github_pr` App's bot account. Commits fail until both are set |

## Managing agents

Two scripts wrap the Compose commands. They work with Docker or Podman, from any directory:
- `bin/admin [command ...]` runs the admin tool (`docker compose run --rm admin [command ...]`).
- `bin/agent <name> [hermes arguments]` runs the Hermes CLI in `agent-<name>`, e.g.
  `bin/agent default config get memory` or just `bin/agent default` for an interactive session.

They use `docker compose` if it works, otherwise `podman compose`, otherwise `docker-compose`; set
`COMPOSE` to override (e.g. `COMPOSE=docker-compose`).

Run `bin/admin` with no arguments for the interactive interface:
- **Keys:** `n` new agent, `g` MCP and GitHub access, `s` Slack, `r` rotate key, `d` delete, `F5` refresh.
- **Confirmation:** every change is confirmed in a dialog. Purging an agent's data requires typing
  its name.

The same operations are available as commands, for scripts:

```sh
bin/admin servers                         # MCP servers that can be granted, marking unusable ones
bin/admin levels                          # GitHub access levels that can be granted
bin/admin list                            # agents and their access
bin/admin show <name>
bin/admin create <name> --mcp a,b [--github x,y] [--budget USD] [--slack]
bin/admin grant <name> --mcp a,b          # or --no-mcp; takes effect immediately
bin/admin grant <name> --github x,y       # or --no-github; within a minute
bin/admin grant <name> --budget USD       # monthly budget; takes effect immediately
bin/admin slack <name>                    # prompts; blank keeps current values
bin/admin rotate <name>                   # new key; old one revoked
bin/admin delete <name> [--purge] [--yes]
bin/admin compose <name>                  # agent-<name> block for compose.yaml
```

- **Unusable MCP servers are refused.** `create` and `grant` check each requested server through
  LiteLLM and refuse one LiteLLM can't use, e.g. because its credential is empty. A server that's
  only down upstream is granted, with a warning.
- **Source of truth:** LiteLLM. An agent is the set of keys tagged `managed_by: hermes-admin`; the
  tool ignores every other key. Its budget and spend are on the LiteLLM user
  `hermes-agent-<name>`.
- **Budgets:** `show` and `list` give each agent's spend and budget for the month. An agent created
  before budgets were added gets the default $1000 on its next `grant`.
- **Secrets never go on the command line**, where they'd be visible in the host's process list.
  Slack tokens come from masked prompts (`--slack`, `slack <name>`) or, for scripts, as
  `SLACK_*=...` lines on stdin (`--slack-from-stdin`, `slack <name> --from-stdin`).
- **Restarts:** Slack changes and `rotate` take effect when the agent restarts
  (`docker compose restart agent-<name>`). The tool can't restart agents itself, because it has
  no Docker socket.
- **`delete`** revokes the key and removes the agent's secrets but keeps its sessions, memories,
  skills, and this month's spend, so `create` can bring it back. `--purge` also deletes its data
  and its LiteLLM user.

To add an agent:
1. Print the agent's service block with `bin/admin compose <name>` and add it under `services:` in
   `compose.yaml`.
2. Write its identity in `hermes/souls/<name>.md` (see below).
3. Run `bin/admin create <name> ...`.
4. Set up its Slack app as in [Setup](#setup), step 6, using `agent-<name>`.
5. Run `docker compose up -d agent-<name>`.

To remove an agent:
1. Delete its block from `compose.yaml` and run `docker compose up -d --remove-orphans`, which
   removes its container.
2. Run `bin/admin delete <name> --purge`, or leave out `--purge` to keep its data.
3. Delete `hermes/souls/<name>.md` and the agent's Slack app.

To add an MCP server, add it under `mcp_servers:` in `litellm/config.yaml` (with its credential
in `secrets/litellm.env`) and add a matching entry, at `http://litellm:4000/mcp/<server>`, to
`hermes/config.yaml`, with `strict_redirect_headers: true` like the others, then restart the
agents.

### Agent identity (SOUL.md)

Each agent's identity prompt is `hermes/souls/<name>.md`, mounted read-only as Hermes' `SOUL.md`.
Hermes includes it in every request, so keep it short. Changes are reviewed through git, and the
agent can't rewrite its own instructions; asked to, it drafts a proposal for a maintainer instead.

Hermes reads SOUL.md when a session starts. After editing, use `/new` in Slack for a fresh
session, or `docker compose restart agent-<name>` to apply it everywhere.

## Upgrading

Every image is pinned by tag and digest, in `compose.yaml` and the Dockerfiles. To upgrade one,
change both, using the digest of the tag's multi-platform index, e.g. from
`docker buildx imagetools inspect <image>:<tag>` or `regctl image digest <image>:<tag>`. Then
run `tests/run.sh`, and apply the change with `docker compose build` and `docker compose up -d`.

- **Hermes:** `HERMES_IMAGE` and the version in `image:`, both under `x-hermes-agent` in
  `compose.yaml`. Follow the upgrade checklist in `hermes/config.yaml`: new Hermes versions
  can add side tasks that would otherwise bypass `fast`, and a new config schema version needs
  `_config_version` updated, because agents can't write their read-only `config.yaml`.
- **LiteLLM:** the `litellm` service's `image`. `litellm/tier_classifier.py` depends on LiteLLM
  internals, which the tests cover.
- **GitHub CLI:** `GH_VERSION` and both checksums in `hermes/image/Dockerfile`.
- **Python:** the base image in `admin/`, `github-token/`, `hermes/image/`, and `sandbox/` Dockerfiles.
- **Sandbox toolchain:** the JDK, Node, and Maven pins at the top of `sandbox/Dockerfile`.
- **Postgres:** a minor version only needs the new image. A major version also needs its data
  upgraded (`pg_upgrade` or a dump and restore); the volume is mounted at `/var/lib/postgresql`
  so `pg_upgrade --link` can work across versions.

Never change `LITELLM_SALT_KEY` after the first start: it encrypts the credentials LiteLLM stores
in Postgres.

## Images and releases

GitHub Actions publishes the stack's images to Docker Hub for `linux/amd64` and `linux/arm64`:

| Image | Built from |
|---|---|
| `openmrsinfra/hermes` | `hermes/image` (Hermes plus `gh`, on the Hermes image pinned in `compose.yaml`) |
| `openmrsinfra/hermes-admin` | `admin/` |
| `openmrsinfra/hermes-github-token` | `github-token/` |
| `openmrsinfra/hermes-sandbox` | `sandbox/` |

- **Every push to `main`** publishes `:nightly` and `:<short commit sha>`, once `tests/run.sh`
  passes. Pull requests build the images without publishing them.
- **Releases** are cut by running the Release workflow (Actions → Release → Run workflow, from
  `main`). Choose `patch`, `minor`, or `major` to bump the latest `v<version>` tag, or give an
  exact version (e.g. `1.2.0-beta.1` for a pre-release). The workflow runs the tests, publishes
  the images as `:<version>` (and `:latest` for the newest stable release), then tags
  `v<version>` and creates a GitHub Release listing every pull request merged since the
  previous release.

`compose.yaml` still builds the images locally; the published ones are for deployments that pull
them instead.

## Checking the setup

```sh
# Only LITELLM_API_KEY and SLACK_* should be listed
docker compose exec agent-default sh -c 'cut -d= -f1 /opt/data/.env; env | cut -d= -f1' | grep -E "KEY|TOKEN|SECRET|PASS"
# Should list the server's tools for a granted server, and fail for any other
bin/agent default mcp test rovo_read
# Should fail with "Virtual key is not allowed to call this route"
docker compose exec agent-default sh -c \
  '. /opt/data/.env; curl -s -X POST -H "Authorization: Bearer $LITELLM_API_KEY" http://litellm:4000/key/generate'
```

## Tests

`tests/run.sh` checks the stack's security properties and the parts that depend on LiteLLM and
Hermes internals. Run it after changing any of these:

- any image (see [Upgrading](#upgrading))
- `compose.yaml`
- `litellm/config.yaml` or `litellm/tier_classifier.py`
- `hermes/config.yaml` or `hermes/image/`
- `admin/`, `github-token/` (including `levels.json`), or `sandbox/`

```sh
tests/run.sh                          # uses "docker compose"
COMPOSE=docker-compose tests/run.sh   # e.g. with Podman
```

It needs the [prerequisites](#prerequisites) above, including `python3`, `openssl`, `shasum`, and
`base64` on the host. It uses fake credentials, mock models, and fake Jev, Rovo MCP, Anthropic,
and GitHub endpoints, so it never calls a real provider. It runs as a separate Compose project
with its own volumes and cleans up after itself. It takes about 5 minutes. GitHub Actions runs it
on every pull request and push to `main` (`.github/workflows/build.yml`).

It checks:

- **Isolation (static):**
  - every `agent-*` service has no env files, is only on the `agents` network, and mounts only
    its own `agents-data` subdirectory, its own `hermes/souls/<name>.md`, and
    `hermes/config.yaml`, both read-only;
  - only `admin` mounts the whole volume;
  - each secret file reaches only the services listed above;
  - `github-token` is only on the `agents` network, with no ports, mounting only `levels.json`
    read-only;
  - Postgres and admin are on internal networks;
  - every service has `no-new-privileges`, drops all capabilities, and isn't privileged.
- **Admin tool:**
  - `compose` prints the `agent-default` block in `compose.yaml`, and its block for a new agent
    passes the isolation checks;
  - create, grant (MCP and GitHub separately), rotate (keeping grants), slack, and delete,
    including refusals (existing agent, unknown MCP server or GitHub level, bad name, bad budget,
    nothing to grant, delete without confirmation);
  - budgets: set on the agent's LiteLLM user (default or given), changed by `grant`, given to an
    agent without one on its next `grant`, kept by `delete` and removed by `--purge`; an agent
    over budget is refused, and `rotate` doesn't reset its spend;
  - an MCP server with an empty credential is marked by `servers` and refused by `grant`, while
    one that's down upstream is granted with a warning;
  - purging an agent that has run removes all of its data;
  - keys it didn't create are left alone;
  - agent keys can't reach management routes or provider passthroughs, and can only use the
    agent models;
  - each key reaches only its granted MCP servers, with the right service-account key injected;
  - an agent key can read its own key info but not another agent's or a non-agent key's;
  - the interactive interface (create, change access and budget, purge with typed confirmation),
    driven headlessly.
- **Side tasks:** every Hermes side task routes to `fast`, including background review and the
  curator, which use a separate resolver. This fails if a Hermes upgrade adds a task that isn't
  listed under `auxiliary:` in `hermes/config.yaml`.
- **Classifier chain:**
  - Jev's tier picks the model, with exactly one Jev call per request.
  - If Jev fails, the LLM classifier decides.
  - If both fail, the heuristic decides.
  - `ROUTER_CLASSIFIERS=llm`, or an empty `TYPESAFE_API_KEY`, means Jev is never called.
  - A classifier model that is itself a router (e.g. `auto`) is refused without recursing.
  - No classifier plugin errors in any scenario.
- **Prompt caching:** requests to each tier model reach the provider with cache markers on the
  system prompt and the latest message.
- **Classifier plugin internals:** refuses router models and aliases to them, skips Jev without
  `jev-router` or a TypeSafe key and otherwise calls it through the Router, rebuilds when LiteLLM
  replaces its router, and declines re-entry.
- **GitHub** (with a test-only write level alongside `github_read`):
  - the agent's `gh` is the pinned version and gets a `github_read` token, which is reused;
  - the agent's `git` gets its GitHub credentials through `gh`, only for granted levels, and
    commits as the bot in `GIT_BOT_NAME` and `GIT_BOT_EMAIL`;
  - `github-token` signs a valid App JWT (checked against the App's public key) and requests
    exactly each level's permissions and repository limits;
  - a level the key wasn't granted is refused (403), as are a missing, unknown, or revoked key
    (401) and an unknown level (404);
  - the App's private key never appears in the agent's environment or data.
- **Sandbox:**
  - only keys granted `sandbox` can use it;
  - it builds a branch or a pull request's head (from a local git server) with the chosen JDK;
  - builds run as the unprivileged build user on a read-only root filesystem;
  - a job leaves no files or processes behind for the next one, except its caches;
  - repositories outside the openmrs organization are refused, jobs run one at a time and are
    killed at their timeout, and the sandbox holds no secrets.
- **Hermes end to end:**
  - `auto` gets the 512k context length.
  - Chat goes through the router, and session titles come from `fast`.
  - The agent's `SOUL.md` is its repo file, can't be written, and reaches the model.
  - The agent's `config.yaml` is `hermes/config.yaml`, can't be written, and its
    `_config_version` matches the Hermes image, so startup has no migration to write.
  - Runs are capped at 50 model calls.
  - The agent's terminal blocks build and package-manager commands, in any part of a command.
  - Hermes reaches only its granted MCP server.
  - Hermes prunes sessions after 30 days.
  - Hermes needs no routes outside the key's allow-list.
  - The agent's environment and data hold no LiteLLM, master, or Postgres secrets, and it
    can't reach Postgres.
