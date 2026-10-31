#!/usr/bin/env bash
#
# Checks this stack's security properties and the parts that depend on LiteLLM and Hermes
# internals. Re-run after changing compose.yaml, any image or Dockerfile, litellm/, hermes/,
# admin/, github-token/, or workspace/.
#
# Usage: tests/run.sh
#
# Uses fake credentials, mock models, and fake Jev, Rovo, Anthropic, and GitHub endpoints, so
# nothing reaches a real provider. Runs as its own Compose project ("hermes-test") and removes its
# containers and volumes on exit, so a running stack is untouched. Needs Docker Compose 2.24.4+
# (for !override) and python3, openssl, shasum, base64, and git on the host.
#
# What it checks:
#   1. compose.yaml separates secrets, volumes, and networks; every service has no-new-privileges
#      and only the capabilities it needs (static).
#   2. Admin tool: create/grant/rotate/slack/delete; key lockdown (no management API, no provider
#      passthroughs, agent models only; workspace keys only /key/info); monthly budgets that survive
#      rotation; per-key MCP access with the right credential; root-only workspace configuration;
#      the interactive interface.
#   3. Every Hermes side task, including background review and curator, uses LiteLLM's `fast`.
#   4. `auto` classifier chain: Jev decides; if Jev fails, the LLM classifier does.
#   5. Hermes end to end: `auto` context length is 768000; chat goes through the router; titles
#      come from `fast`; the read-only SOUL.md reaches the model; MCP reaches only granted servers;
#      the terminal, file tools, and execute_code run in the workspace; the agent can't change its
#      .env but sees admin's changes after a restart; it holds no other secrets and can't reach
#      Postgres.
#   6. tier_classifier.py internals: refuses router classifier models, follows Router rebuilds,
#      declines re-entry.
#   7. Classifier fallbacks: both fail -> heuristic; classifier model `auto` refused without
#      recursing; ROUTER_CLASSIFIERS=llm and a missing TypeSafe key skip Jev. Admin refuses an MCP
#      server with an empty credential and only warns about one that's down.
#   8. Each tier model's requests mark the system prompt and latest message for prompt caching.
#   9. gh in the workspace (and git through it) gets GitHub App tokens only for granted levels;
#      tokens carry each level's permissions and repository limits, are reused, and need a valid,
#      unrevoked key; the App key never reaches the agent or workspace; commits are authored as
#      the bot.
#  10. Workspaces: only the agent's key logs in, and the agent refuses a different host key; commands
#      run as an unprivileged user on a read-only root filesystem, with each JDK; a branch and a pull
#      request clone from a local git server; the workspace can't read its configuration or reach
#      LiteLLM or Postgres, and holds no agent secrets; going idle kills its processes and deletes
#      its files except the caches, or with WORKSPACE_KEEP_FILES keeps /work, also across restarts.
#      Then purging the agents removes all of their data.
#
# Environment:
#   COMPOSE            Compose command (default "docker compose"; e.g. "docker-compose")
#   TEST_LITELLM_PORT  Host port the test LiteLLM publishes (default 14000; not used by the tests)

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
compose_cmd="${COMPOSE:-docker compose}"
export TEST_LITELLM_PORT="${TEST_LITELLM_PORT:-14000}"
# Exported to override the repository's .env, which Compose also reads.
export GIT_BOT_NAME="test-app[bot]" GIT_BOT_EMAIL="1+test-app[bot]@users.noreply.github.com"
master_key="sk-test-master"
expected_context=768000
# LiteLLM's log line when a classifier plugin call goes wrong (as opposed to declining).
plugin_error_pattern='classifier plugin (failed|timed out|returned)'

TEST_WORK="$(mktemp -d)"
export TEST_WORK
failures=0

compose() {
  # shellcheck disable=SC2086 # compose_cmd may be two words ("docker compose")
  $compose_cmd --progress quiet -p hermes-test --project-directory "$root" \
    -f "$root/compose.yaml" -f "$root/tests/compose.test.yaml" "$@"
}

cleanup() {
  compose --profile admin down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$TEST_WORK"
}
trap cleanup EXIT

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; failures=$((failures + 1)); }

expect() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then pass "$name"; else fail "$name (expected '$expected', got '$actual')"; fi
}

expect_ok() {
  local name="$1"
  shift
  if "$@" >"$TEST_WORK/out" 2>&1; then pass "$name"; else fail "$name ($(tail -n 3 "$TEST_WORK/out"))"; fi
}

expect_refused() {
  local name="$1"
  shift
  if "$@" >"$TEST_WORK/out" 2>&1; then fail "$name (command succeeded)"; else pass "$name"; fi
}

# Logs go to a file first: grep on a pipe can exit early, and under pipefail the writer's SIGPIPE
# would fail the pipeline.
log_count() {
  local service="$1" pattern="$2"
  compose logs --no-log-prefix "$service" > "$TEST_WORK/$service.log" 2>&1 || true
  grep -cE "$pattern" "$TEST_WORK/$service.log" || true
}

log_count_fixed() {
  local service="$1" text="$2"
  compose logs --no-log-prefix "$service" > "$TEST_WORK/$service.log" 2>&1 || true
  grep -cF -- "$text" "$TEST_WORK/$service.log" || true
}

set_jev_mode() {
  compose exec -T fake-jev sh -c "echo $1 > /tmp/mode"
}

jev_calls() {
  log_count fake-jev "jev request"
}

admin() {
  compose run --rm -T admin "$@"
}

# Runs admin with $1 on stdin, expanding escapes like \n.
admin_stdin() {
  local input="$1"
  shift
  printf '%b' "$input" | compose run --rm -T admin "$@"
}

# Runs a shell command in the admin container, which has all of agents-data at /agents.
admin_sh() {
  compose run --rm -T --entrypoint sh admin -c "$1"
}

agent_key() {
  admin_sh "sed -n 's/^LITELLM_API_KEY=//p' /agents/$1/.env"
}

# Requests LiteLLM; prints the status and leaves the body in $TEST_WORK/body.
request() {
  local method="$1" path="$2" key="$3" data="${4:-}"
  [ -n "$data" ] || data='{}'
  compose exec -T litellm python3 /tests/http_request.py "$method" "$path" "$key" "$data" > "$TEST_WORK/response"
  tail -n +2 "$TEST_WORK/response" > "$TEST_WORK/body"
  head -n 1 "$TEST_WORK/response"
}

# Prints `auto`'s reply, which names the tier model that answered.
ask_auto() {
  local key="$1" prompt="$2"
  request POST /v1/chat/completions "$key" \
    "{\"model\": \"auto\", \"messages\": [{\"role\": \"user\", \"content\": \"$prompt\"}]}" >/dev/null
  python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(d["choices"][0]["message"]["content"] if "choices" in d else d)' \
    "$TEST_WORK/body"
}

# Asks `auto` and checks both the answering tier model and how many times Jev was called.
expect_auto() {
  local name="$1" prompt="$2" expected_reply="$3" expected_jev_calls="$4"
  local before reply
  before="$(jev_calls)"
  reply="$(ask_auto "$reader_key" "$prompt")"
  expect "$name" "$expected_reply" "$reply"
  expect "$name: Jev calls" "$expected_jev_calls" "$(($(jev_calls) - before))"
}

expect_no_plugin_errors() {
  expect "$1: no classifier plugin errors" "0" "$(log_count litellm "$plugin_error_pattern")"
}

restart_litellm() {
  compose up -d --wait --wait-timeout 180 --force-recreate --no-deps litellm >/dev/null
}

expect_route_denied() {
  local name="$1" key="$2" method="$3" path="$4" status
  status="$(request "$method" "$path" "$key")"
  if grep -q "not allowed to call this route" "$TEST_WORK/body"; then
    pass "$name: $method $path denied"
  else
    fail "$name: $method $path denied (HTTP $status: $(head -c 200 "$TEST_WORK/body"))"
  fi
}

# Calls fake Rovo's `whoami` on one MCP server; prints the account LiteLLM used
# (read/jira-write/confluence-write/none), "denied", or "error <status>: <body>".
rovo_whoami() {
  local key="$1" server_id="$2" status
  status="$(request POST /mcp-rest/tools/call "$key" \
    "{\"server_id\": \"$server_id\", \"name\": \"whoami\", \"arguments\": {}}")"
  python3 - "$TEST_WORK/body" "$status" <<'EOF'
import json, sys
body, status = open(sys.argv[1]).read(), sys.argv[2]
try:
    data = json.loads(body)
except ValueError:
    data = {}
if isinstance(data, dict) and data.get("content"):
    print(data["content"][0].get("text"))
elif "access_denied" in body or "not allowed to access" in body:
    print("denied")
else:
    print(f"error {status}: {body[:150]}")
EOF
}

# Prints a field of one agent from `admin show --json`.
agent_field() {
  admin show "$1" --json | python3 -c 'import json, sys; v = json.load(sys.stdin)[sys.argv[1]]; print(",".join(v) if isinstance(v, list) else v)' "$2"
}

echo "== Setup"
compose --profile admin down -v --remove-orphans >/dev/null 2>&1 || true
python3 "$root/tests/make_litellm_config.py" "$root/litellm/config.yaml" "$TEST_WORK/litellm.yaml"
# Throwaway GitHub App key pair: github-token signs, fake-github verifies.
openssl genrsa -out "$TEST_WORK/github-app.pem" 2048 2>/dev/null
openssl rsa -in "$TEST_WORK/github-app.pem" -pubout -out "$TEST_WORK/github-app.pub.pem" 2>/dev/null
# All levels share the test App. The test-only github_test_write checks that write permissions
# and repository limits reach GitHub and that grants are enforced per level.
github_key_base64="$(base64 < "$TEST_WORK/github-app.pem" | tr -d '\n')"
for prefix in GITHUB_READ GITHUB_PR GITHUB_TEST_WRITE; do
  echo "${prefix}_APP_ID=12345"
  echo "${prefix}_INSTALLATION_ID=67890"
  echo "${prefix}_PRIVATE_KEY_BASE64=$github_key_base64"
done > "$TEST_WORK/github-app.env"
python3 -c 'import json, sys
levels = json.load(open(sys.argv[1]))
levels["github_test_write"] = {"permissions": {"contents": "write", "metadata": "read", "pull_requests": "write"},
                               "repositories": ["openmrs-test-repo"]}
json.dump(levels, open(sys.argv[2], "w"), indent=2)' "$root/github-token/levels.json" "$TEST_WORK/github-levels.json"
# The workspace's test repository: `main` and a pull request (refs/pull/7/head) that differ in one
# file, served by fake-git, which allows partial clones as GitHub does.
git -c init.defaultBranch=main init -q "$TEST_WORK/repo"
git_test() { git -C "$TEST_WORK/repo" -c user.name=test -c user.email=test@example.org "$@"; }
echo main > "$TEST_WORK/repo/marker" && git_test add marker && git_test commit -qm main
git_test checkout -qb pr && echo pr > "$TEST_WORK/repo/marker" && git_test commit -qam pr
mkdir -p "$TEST_WORK/git"
git clone -q --bare "$TEST_WORK/repo" "$TEST_WORK/git/test-repo"
git -C "$TEST_WORK/git/test-repo" update-ref refs/pull/7/head "$(git_test rev-parse pr)"
git -C "$TEST_WORK/git/test-repo" config uploadpack.allowFilter true
mkdir -p "$TEST_WORK/check-home"
cp "$root/hermes/config.yaml" "$TEST_WORK/check-home/config.yaml"

echo "== 1. compose.yaml isolation (static)"
# Resolved against placeholder secrets (see tests/compose_isolation.py), so real secrets/*.env
# never reach the output.
python3 "$root/tests/compose_isolation.py" write-secrets "$TEST_WORK/compose-check/secrets"
# shellcheck disable=SC2086
if $compose_cmd --profile '*' --project-directory "$TEST_WORK/compose-check" -f "$root/compose.yaml" \
  config --format json 2>/dev/null > "$TEST_WORK/compose.json" &&
  python3 "$root/tests/compose_isolation.py" < "$TEST_WORK/compose.json"; then
  pass "compose.yaml isolation"
else
  fail "compose.yaml isolation (see output above)"
fi

echo "== Starting LiteLLM"
compose --profile admin build admin agent-default workspace-default github-token >/dev/null
compose up -d --wait --wait-timeout 180 postgres litellm github-token fake-jev fake-rovo fake-anthropic \
  fake-github fake-git >/dev/null

echo "== 2. Admin tool"
expect "servers lists the configured MCP servers" '["rovo_confluence_write", "rovo_jira_write", "rovo_read"]' "$(admin servers --json)"
expect "levels lists the GitHub access levels" '["github_pr", "github_read", "github_test_write"]' "$(admin levels --json)"
expect_ok "create default (rovo_read, github_read, with Slack)" admin_stdin \
  'SLACK_BOT_TOKEN=xoxb-test\nSLACK_APP_TOKEN=xapp-test\nSLACK_ALLOWED_USERS=U1\n' \
  create default --mcp rovo_read --github github_read --slack-from-stdin
expect_ok "create writer (rovo_jira_write, \$50 budget)" admin create writer --mcp rovo_jira_write --budget 50
expect_ok "create nobody (no MCP)" admin create nobody
expect_refused "create refuses an existing agent" admin create default --mcp rovo_read
expect_refused "create refuses an unknown MCP server" admin create typo --mcp rovo_raed
expect "unknown MCP server: no agent created" "" "$(admin list --json | python3 -c 'import json, sys; print(",".join(a["name"] for a in json.load(sys.stdin) if a["name"] == "typo"))')"
expect_refused "create refuses an unknown GitHub level" admin create typo --github github_raed
expect_refused "create refuses an invalid name" admin create Bad_Name
expect_refused "create refuses a zero budget" admin create typo --budget 0
expect_refused "create refuses a negative budget" admin create typo --budget -5
expect "list shows the agents" "default nobody writer" \
  "$(admin list --json | python3 -c 'import json, sys; print(" ".join(a["name"] for a in json.load(sys.stdin)))')"
expect "default's grants" "rovo_read" "$(agent_field default mcp_servers)"
expect "nobody's grants" "" "$(agent_field nobody mcp_servers)"
expect "default's GitHub grants" "github_read" "$(agent_field default github_levels)"
expect "nobody's GitHub grants" "" "$(agent_field nobody github_levels)"
expect "create sets the default monthly budget" "1000.0" "$(agent_field default budget)"
expect "create sets the given monthly budget" "50.0" "$(agent_field writer budget)"
expect "budgets are on the agent's LiteLLM user, monthly" "50.0 1mo" \
  "$(request GET /user/info?user_id=hermes-agent-writer "$master_key" >/dev/null
     python3 -c 'import json, sys; u = json.load(open(sys.argv[1]))["user_info"]; print(u["max_budget"], u["budget_duration"])' \
     "$TEST_WORK/body")"
expect "default's .env: mode 600, owned by the Hermes user" "600 10000" "$(admin_sh 'stat -c "%a %u" /agents/default/.env')"
expect "default's .env has key and Slack settings" "LITELLM_API_KEY SLACK_ALLOWED_USERS SLACK_APP_TOKEN SLACK_BOT_TOKEN" \
  "$(admin_sh 'cut -d= -f1 /agents/default/.env | sort | tr "\n" " "' | sed 's/ $//')"
expect "create gives the agent a workspace key, not a second agent key" "1 1 True" \
  "$(agent_field default keys) $(agent_field default workspace_keys) $(agent_field default has_workspace)"
expect "default's SSH key: mode 600, owned by the Hermes user; known_hosts pins workspace-default" \
  "600 10000 [workspace-default]:2222 ssh-ed25519" \
  "$(admin_sh 'stat -c "%a %u" /agents/default/ssh/id_ed25519; cut -d" " -f1,2 /agents/default/ssh/known_hosts' | tr '\n' ' ' | sed 's/ $//')"
expect "workspace configuration is root-only" "700 0 600 0 600 0 600 0" \
  "$(admin_sh 'cd /workspaces/default && stat -c "%a %u" . authorized_keys ssh_host_ed25519_key litellm_key' | tr '\n' ' ' | sed 's/ $//')"
workspace_key() {
  admin_sh "cat /workspaces/$1/litellm_key"
}
default_workspace_key="$(workspace_key default)"
expect "workspace key can read its own key info" "200" "$(request GET /key/info "$default_workspace_key")"
for route in "POST /v1/chat/completions" "GET /v1/models" "POST /mcp-rest/tools/call" "POST /key/generate" \
  "POST /anthropic/v1/messages"; do
  # shellcheck disable=SC2086 # split "METHOD PATH"
  expect_route_denied "workspace key" "$default_workspace_key" $route
done

# A key the tool didn't create must be invisible to it and untouched.
request POST /key/generate "$master_key" '{"key_alias": "someone-else", "metadata": {"hermes_agent": "default"}}' >/dev/null
unmanaged_key="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["key"])' "$TEST_WORK/body")"
expect "keys without the managed_by marker are ignored" "1" "$(agent_field default keys)"

reader_key="$(agent_key default)"
writer_key="$(agent_key writer)"
nobody_key="$(agent_key nobody)"

# Agent keys may call /key/info (github-token reads grants with it), but only for their own keys:
# each agent has its own user_id.
expect "agent key can read its own key info" "200" "$(request GET /key/info "$writer_key")"
expect "agent key can't read another agent's key info" "403" \
  "$(request GET "/key/info?key=$(printf '%s' "$reader_key" | shasum -a 256 | cut -d' ' -f1)" "$writer_key")"
expect "agent key can't read a non-agent key's info" "403" \
  "$(request GET "/key/info?key=$(printf '%s' "$unmanaged_key" | shasum -a 256 | cut -d' ' -f1)" "$writer_key")"
for agent in reader writer nobody; do
  key_var="${agent}_key"
  key="${!key_var}"
  for route in "POST /key/generate" "POST /key/update" "GET /key/list" "POST /model/new" \
    "GET /v1/mcp/server" "POST /v1/mcp/server" "POST /anthropic/v1/messages" "POST /v1/messages" \
    "POST /v1/responses" "POST /v1/embeddings" "POST /v1/systemone" "POST /v1/decisions"; do
    # shellcheck disable=SC2086 # split "METHOD PATH"
    expect_route_denied "$agent" "$key" $route
  done
done
expect "agent key lists only the agent models" "auto balanced fast strong" \
  "$(request GET /v1/models "$reader_key" >/dev/null; python3 -c 'import json, sys; print(" ".join(sorted(m["id"] for m in json.load(open(sys.argv[1]))["data"])))' "$TEST_WORK/body")"
status="$(request POST /v1/chat/completions "$reader_key" '{"model": "classifier-mock", "messages": [{"role": "user", "content": "hi"}]}')"
if [ "$status" = 200 ]; then
  fail "agent key can't use models outside the agent list (got HTTP 200)"
else
  pass "agent key can't use models outside the agent list (HTTP $status)"
fi

request GET /v1/mcp/server "$master_key" >/dev/null
rovo_read_id="$(python3 -c 'import json, sys; print(next(s["server_id"] for s in json.load(open(sys.argv[1])) if s["server_name"] == "rovo_read"))' "$TEST_WORK/body")"
request GET /v1/mcp/server "$master_key" >/dev/null
rovo_jira_write_id="$(python3 -c 'import json, sys; print(next(s["server_id"] for s in json.load(open(sys.argv[1])) if s["server_name"] == "rovo_jira_write"))' "$TEST_WORK/body")"
rovo_confluence_write_id="$(python3 -c 'import json, sys; print(next(s["server_id"] for s in json.load(open(sys.argv[1])) if s["server_name"] == "rovo_confluence_write"))' "$TEST_WORK/body")"
expect "reader -> rovo_read uses the read account" "read" "$(rovo_whoami "$reader_key" "$rovo_read_id")"
expect "reader -> rovo_jira_write denied" "denied" "$(rovo_whoami "$reader_key" "$rovo_jira_write_id")"
expect "writer -> rovo_jira_write uses the Jira write account" "jira-write" "$(rovo_whoami "$writer_key" "$rovo_jira_write_id")"
expect "writer -> rovo_read denied" "denied" "$(rovo_whoami "$writer_key" "$rovo_read_id")"
expect "writer -> rovo_confluence_write denied" "denied" "$(rovo_whoami "$writer_key" "$rovo_confluence_write_id")"
expect "nobody -> rovo_read denied" "denied" "$(rovo_whoami "$nobody_key" "$rovo_read_id")"
expect "nobody -> rovo_jira_write denied" "denied" "$(rovo_whoami "$nobody_key" "$rovo_jira_write_id")"

expect_ok "grant writer both servers" admin grant writer --mcp rovo_read,rovo_jira_write
expect "grant takes effect without a new key" "read" "$(rovo_whoami "$writer_key" "$rovo_read_id")"
expect_ok "grant writer no servers" admin grant writer --no-mcp
expect "--no-mcp removes access" "denied" "$(rovo_whoami "$writer_key" "$rovo_jira_write_id")"
expect_ok "grant writer rovo_confluence_write" admin grant writer --mcp rovo_confluence_write
expect "writer -> rovo_confluence_write uses the Confluence write account" "confluence-write" \
  "$(rovo_whoami "$writer_key" "$rovo_confluence_write_id")"
expect "writer -> rovo_jira_write denied after regrant" "denied" "$(rovo_whoami "$writer_key" "$rovo_jira_write_id")"
expect_ok "grant writer back to rovo_jira_write" admin grant writer --mcp rovo_jira_write
expect "restored grant works" "jira-write" "$(rovo_whoami "$writer_key" "$rovo_jira_write_id")"
expect_ok "grant writer github_test_write" admin grant writer --github github_test_write
expect "GitHub grant changes only GitHub levels" "github_test_write rovo_jira_write" \
  "$(agent_field writer github_levels) $(agent_field writer mcp_servers)"
expect_refused "grant needs something to change" admin grant writer
expect_ok "grant writer a \$75 budget" admin grant writer --budget 75
expect "grant changes the budget" "75.0" "$(agent_field writer budget)"
expect "budget grant leaves access unchanged" "github_test_write rovo_jira_write" \
  "$(agent_field writer github_levels) $(agent_field writer mcp_servers)"
expect_refused "grant refuses a zero budget" admin grant writer --budget 0
# Agents from before budgets have a key but no LiteLLM user; their next grant adds one.
request POST /key/generate "$master_key" '{"user_id": "hermes-agent-legacy",
  "metadata": {"managed_by": "hermes-admin", "hermes_agent": "legacy", "github_levels": []}}' >/dev/null
expect "an agent without a LiteLLM user has no budget" "None" "$(agent_field legacy budget)"
expect_ok "grant legacy without a budget" admin grant legacy --no-mcp
expect "grant gives an agent without a budget the default" "1000.0" "$(agent_field legacy budget)"
expect_ok "delete legacy --purge" admin delete legacy --purge --yes

expect_ok "grant nobody github_read" admin grant nobody --github github_read
old_nobody_workspace_key="$(workspace_key nobody)"
old_nobody_ssh_key="$(admin_sh 'cat /agents/nobody/ssh/id_ed25519' | shasum -a 256)"
expect_ok "rotate nobody" admin rotate nobody
expect "rotate revokes the old workspace key and leaves one" "401 1" \
  "$(request GET /key/info "$old_nobody_workspace_key") $(agent_field nobody workspace_keys)"
expect "rotate issues a new workspace key and SSH key" "new new" \
  "$([ "$(workspace_key nobody)" != "$old_nobody_workspace_key" ] && echo new || echo same) $(
    [ "$(admin_sh 'cat /agents/nobody/ssh/id_ed25519' | shasum -a 256)" != "$old_nobody_ssh_key" ] && echo new || echo same)"
new_nobody_key="$(agent_key nobody)"
expect "rotate revokes the old key" "401" "$(request GET /v1/models "$nobody_key")"
expect "rotate's new key works" "200" "$(request GET /v1/models "$new_nobody_key")"
expect "rotate leaves one key" "1" "$(agent_field nobody keys)"
expect "rotate keeps GitHub grants" "github_read" "$(agent_field nobody github_levels)"

# The budget is on the agent's LiteLLM user, so rotating keys can't reset spend. LiteLLM answers
# 422 once spend reaches it.
expect_ok "grant nobody a tiny budget" admin grant nobody --budget 0.000001
expect "an agent under budget is answered" "200" \
  "$(request POST /v1/chat/completions "$new_nobody_key" '{"model": "fast", "messages": [{"role": "user", "content": "hi"}]}')"
for _ in $(seq 1 30); do
  status="$(request POST /v1/chat/completions "$new_nobody_key" '{"model": "fast", "messages": [{"role": "user", "content": "hi"}]}')"
  [ "$status" = "422" ] && break
  sleep 2
done
expect "an agent over budget is refused" "422 budget_exceeded" \
  "$status $(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["error"]["type"])' "$TEST_WORK/body" 2>/dev/null)"
expect_ok "rotate nobody over budget" admin rotate nobody
over_budget_key="$(agent_key nobody)"
expect "rotate doesn't reset spend" "422" \
  "$(request POST /v1/chat/completions "$over_budget_key" '{"model": "fast", "messages": [{"role": "user", "content": "hi"}]}')"
expect_ok "raise nobody's budget" admin grant nobody --budget 1000
expect "raising the budget takes effect immediately" "200" \
  "$(request POST /v1/chat/completions "$over_budget_key" '{"model": "fast", "messages": [{"role": "user", "content": "hi"}]}')"
new_nobody_key="$over_budget_key"

expect_ok "slack updates one setting from stdin" admin_stdin 'SLACK_ALLOWED_USERS=U2\n' slack default --from-stdin
expect "slack keeps the other settings" "SLACK_ALLOWED_USERS=U2 SLACK_BOT_TOKEN=xoxb-test" \
  "$(admin_sh 'grep -E "^SLACK_(ALLOWED_USERS|BOT_TOKEN)=" /agents/default/.env | sort | tr "\n" " "' | sed 's/ $//')"
expect "slack keeps the agent's key" "$reader_key" "$(agent_key default)"

expect_refused "delete needs confirmation without --yes" admin delete nobody
expect_ok "delete nobody (keep data)" admin delete nobody --yes
expect "delete revokes the key" "401" "$(request GET /v1/models "$new_nobody_key")"
expect "delete keeps the agent's data" "False True" "$(agent_field nobody provisioned) $(agent_field nobody has_data)"
expect "delete keeps the agent's budget" "1000.0" "$(agent_field nobody budget)"
expect "delete removes secrets from .env" "" "$(admin_sh 'grep -E "^(LITELLM_API_KEY|SLACK_)" /agents/nobody/.env || true')"
expect "delete revokes the workspace key and removes the SSH key and workspace configuration" "0 absent absent" \
  "$(agent_field nobody workspace_keys) $(admin_sh 'for p in /agents/nobody/ssh /workspaces/nobody; do
    test -e $p && echo present || echo absent; done' | tr '\n' ' ' | sed 's/ $//')"
expect_ok "re-create a deleted agent, reusing its data" admin create nobody
expect_ok "delete nobody --purge" admin delete nobody --purge --yes
expect "purge removes the agent's data" "absent" "$(admin_sh 'test -e /agents/nobody && echo present || echo absent')"
expect "purge removes the agent's LiteLLM user" "404" "$(request GET /user/info?user_id=hermes-agent-nobody "$master_key")"
expect "unmanaged key untouched" "200" "$(request GET /v1/models "$unmanaged_key")"

# `admin compose` must match compose.yaml's agent-default block, and its block for a new agent
# must pass the isolation checks.
admin compose default > "$TEST_WORK/compose-default.yaml" 2>/dev/null || true
if [ -s "$TEST_WORK/compose-default.yaml" ] &&
  python3 -c 'import sys; sys.exit(open(sys.argv[2]).read() not in open(sys.argv[1]).read())' \
    "$root/compose.yaml" "$TEST_WORK/compose-default.yaml"; then
  pass "compose prints the agent-default block in compose.yaml"
else
  fail "compose prints the agent-default block in compose.yaml (got: $(cat "$TEST_WORK/compose-default.yaml"))"
fi
# The block uses compose.yaml's x-hermes-agent anchor, so it goes into a copy of compose.yaml, not
# an override file.
admin compose extra 2>/dev/null > "$TEST_WORK/compose-extra-block.yaml" || true
python3 -c 'import sys; c = open(sys.argv[1]).read(); b = open(sys.argv[2]).read(); print(c.replace("\nnetworks:\n", "\n" + b + "\nnetworks:\n", 1), end="")' \
  "$root/compose.yaml" "$TEST_WORK/compose-extra-block.yaml" > "$TEST_WORK/compose-extra.yaml"
# shellcheck disable=SC2086
if $compose_cmd --profile '*' --project-directory "$TEST_WORK/compose-check" -f "$TEST_WORK/compose-extra.yaml" \
  config --format json 2>/dev/null > "$TEST_WORK/compose-extra.json" &&
  grep -q '"agent-extra"' "$TEST_WORK/compose-extra.json" &&
  python3 "$root/tests/compose_isolation.py" < "$TEST_WORK/compose-extra.json" >/dev/null; then
  pass "compose output for a new agent passes the isolation checks"
else
  fail "compose output for a new agent passes the isolation checks"
fi
expect_refused "compose refuses an invalid name" admin compose Bad_Name

if compose run --rm -T --entrypoint python admin /tests/admin_tui_checks.py; then
  pass "interactive interface"
else
  fail "interactive interface (see output above)"
fi

echo "== 3. Hermes side tasks use fast"
# Clears check-home from inside: with rootful Docker, Hermes' files there are root-owned on the
# host and cleanup couldn't remove them.
if compose run --rm --no-deps -T -e HERMES_HOME=/check-home -e LITELLM_API_KEY=sk-placeholder \
  -v "$TEST_WORK/check-home:/check-home" -v "$root/tests/hermes_side_tasks.py:/check.py:ro" \
  --entrypoint sh agent-default -c \
  '/opt/hermes/.venv/bin/python /check.py; status=$?; find /check-home -mindepth 1 -delete; exit $status'; then
  pass "all side tasks route to litellm/fast"
else
  fail "side task routing (see output above)"
fi

echo "== 4. Classifier chain"
set_jev_mode SIMPLE
expect_auto "Jev SIMPLE routes to fast" "What is the capital of France?" "fast-model-answered" 1
set_jev_mode MEDIUM
expect_auto "Jev MEDIUM routes to balanced" "Explain REST vs gRPC." "balanced-model-answered" 1
set_jev_mode COMPLEX
expect_auto "Jev COMPLEX routes to balanced" "Design a rate limiter." "balanced-model-answered" 1
set_jev_mode REASONING
expect_auto "Jev REASONING routes to strong" "Prove it." "strong-model-answered" 1
expect "Jev received the TypeSafe key on every call" "0" "$(log_count fake-jev "auth_ok=False")"
set_jev_mode fail
# The LLM classifier's call is charged to the agent whose request it routes. Spend logs are
# written asynchronously, so this polls.
classifier_query="select count(*) from \"LiteLLM_SpendLogs\" where model_group = 'classifier-mock'
  and \"user\" = 'hermes-agent-default';"
classifier_calls() {
  printf '%s\n' "$classifier_query" | compose exec -T postgres psql -U litellm -d litellm -At 2>/dev/null || echo 0
}
classifier_before="$(classifier_calls)"
expect_auto "Jev failure falls back to LLM classifier (REASONING -> strong)" "Design a rate limiter." \
  "strong-model-answered" 1
expect "LLM classifier ran after the Jev failure" "1" "$(log_count litellm "jev classifier did not decide")"
classifier_after="$classifier_before"
for _ in $(seq 1 30); do
  classifier_after="$(classifier_calls)"
  [ "${classifier_after:-0}" -gt "${classifier_before:-0}" ] && break
  sleep 3
done
expect "LLM classifier calls are charged to the agent" "1" "$((${classifier_after:-0} - ${classifier_before:-0}))"
expect_no_plugin_errors "classifier chain"

echo "== 5. Hermes end to end"
set_jev_mode MEDIUM
denied_before="$(log_count litellm "not allowed to call this route")"
compose up -d --wait --wait-timeout 180 workspace-default >/dev/null && compose up -d --no-deps agent-default >/dev/null
context=""
for _ in $(seq 1 60); do
  context="$(compose exec -T agent-default sh -c 'cat /opt/data/logs/gateway.log 2>/dev/null' |
    sed -n 's/.*Model context warmed: auto -> \([0-9]*\) tokens.*/\1/p' | tail -n 1 || true)"
  [ -n "$context" ] && break
  sleep 3
done
expect "Hermes context length for auto" "$expected_context" "$context"
chat="$(compose exec -T agent-default hermes chat -q "Explain REST vs gRPC briefly." 2>&1 || true)"
if printf '%s' "$chat" | grep -q "balanced-model-answered"; then
  pass "Hermes chat goes through the auto router"
else
  fail "Hermes chat goes through the auto router (output: $(printf '%s' "$chat" | tail -n 5))"
fi
expect "Hermes session title comes from fast" "fast-model-answered" \
  "$(printf '%s' "$chat" | sed -n 's/^Title:[[:space:]]*//p' | tail -n 1)"
# hermes/config.yaml lists every Rovo server; the key only grants rovo_read.
mcp="$(compose exec -T agent-default hermes mcp test rovo_read 2>&1 || true)"
if printf '%s' "$mcp" | grep -q "whoami"; then
  pass "Hermes MCP reaches the granted server"
else
  fail "Hermes MCP reaches the granted server (output: $(printf '%s' "$mcp" | tail -n 6))"
fi
mcp="$(compose exec -T agent-default hermes mcp test rovo_jira_write 2>&1 || true)"
if printf '%s' "$mcp" | grep -q "whoami"; then
  fail "Hermes MCP can't reach a server it wasn't granted (output: $(printf '%s' "$mcp" | tail -n 6))"
else
  pass "Hermes MCP can't reach a server it wasn't granted"
fi
expect "Hermes user profile memory is off" "false" \
  "$(compose exec -T agent-default hermes config get memory.user_profile_enabled 2>&1 | tr -d '[:space:]')"
expect "Hermes prunes sessions after 30 days" "true 30" \
  "$(compose exec -T agent-default sh -c 'echo $(hermes config get sessions.auto_prune) $(hermes config get sessions.retention_days)' 2>&1)"
# SOUL.md is the repo file, mounted read-only. The test LiteLLM config stores prompts in spend
# logs, written asynchronously, so the prompt check polls.
soul_file="$root/hermes/souls/default.md"
expect "SOUL.md in the agent is hermes/souls/default.md" "$(shasum -a 256 < "$soul_file" | cut -d' ' -f1)" \
  "$(compose exec -T agent-default cat /opt/data/SOUL.md | shasum -a 256 | cut -d' ' -f1)"
expect_refused "agent can't write its SOUL.md" compose exec -T agent-default sh -c 'echo changed >> /opt/data/SOUL.md'
# config.yaml too. Its _config_version must match the image's, or Hermes' startup migration would
# try to rewrite it.
expect "config.yaml in the agent is hermes/config.yaml" "$(shasum -a 256 < "$root/hermes/config.yaml" | cut -d' ' -f1)" \
  "$(compose exec -T agent-default cat /opt/data/config.yaml | shasum -a 256 | cut -d' ' -f1)"
expect_refused "agent can't write its config.yaml" compose exec -T agent-default sh -c 'echo changed >> /opt/data/config.yaml'
expect_refused "agent can't write its .env" compose exec -T agent-default sh -c 'echo X=1 >> /opt/data/.env'
expect_refused "agent can't replace its .env" compose exec -T agent-default sh -c 'rm /opt/data/.env'
expect "hermes/config.yaml's _config_version is the Hermes image's" \
  "$(compose exec -T agent-default /opt/hermes/.venv/bin/python -c \
    'from hermes_cli.config_defaults import DEFAULT_CONFIG; print(DEFAULT_CONFIG["_config_version"])' 2>&1)" \
  "$(sed -n 's/^_config_version: *//p' "$root/hermes/config.yaml")"
expect "Hermes started without a config migration warning" "0" \
  "$(log_count_fixed agent-default "docker_config_migrate.py failed")"
# Hermes' own tools, called as the agent would, all run in the workspace and can't see the agent's
# files.
expect "Hermes' terminal, file tools, and execute_code run in the workspace" \
  "terminal=20000@$(compose exec -T workspace-default hostname) write_file=written agent_file_visible=False execute_code=20000" \
  "$(compose exec -T -u "${HERMES_UID:-10000}" -w /opt/hermes agent-default python - < "$root/tests/hermes_workspace_tools.py" 2>/dev/null |
    tail -n 1)"
expect "Hermes caps runs at 50 model calls, warning at 80%" "50 0.8" \
  "$(compose exec -T agent-default sh -c 'echo $(hermes config get agent.max_turns) $(hermes config get agent.budget_warning_ratio)' 2>&1)"
soul_marker="$(head -c 60 "$soul_file")"
soul_query="select count(*) from \"LiteLLM_SpendLogs\" where model_group = 'auto' and
  position(:'marker' in coalesce(messages::text, '') || coalesce(proxy_server_request::text, '')) > 0;"
soul_prompts=0
for _ in $(seq 1 30); do
  soul_prompts="$(printf '%s\n' "$soul_query" |
    compose exec -T postgres psql -U litellm -d litellm -At -v marker="$soul_marker" 2>/dev/null || echo 0)"
  [ "${soul_prompts:-0}" -gt 0 ] && break
  sleep 3
done
if [ "${soul_prompts:-0}" -gt 0 ]; then
  pass "Hermes sends SOUL.md to the model"
else
  fail "Hermes sends SOUL.md to the model (no auto request containing '$soul_marker')"
fi
expect "Hermes needed no routes outside the agent key's allow-list" "$denied_before" \
  "$(log_count litellm "not allowed to call this route")"
expect_no_plugin_errors "Hermes end to end"

secrets="$(cat "$root/tests/fixtures/litellm.env" "$root/tests/fixtures/master.env" "$root/tests/fixtures/postgres.env" |
  grep -v -E '^(#|$|LLM_MODEL_)' | cut -d= -f2-)"
scan="$(printf '%s\n' "$secrets" | compose exec -T agent-default sh -c '
  env > /tmp/agent-env; checked=0; leaked=0
  while read -r s; do
    checked=$((checked + 1))
    if grep -rqF -- "$s" /tmp/agent-env /opt/data; then leaked=$((leaked + 1)); fi
  done
  rm /tmp/agent-env; echo "$checked $leaked"' || true)"
# Comparing the checked count keeps a failed exec from passing as "nothing leaked".
expect "agent environment and data hold no LiteLLM, master, or Postgres secrets (checked, leaked)" \
  "$(printf '%s\n' "$secrets" | wc -l | tr -d ' ') 0" "$scan"
if compose exec -T agent-default python3 -c 'import socket; socket.create_connection(("postgres", 5432), timeout=3)' \
  >/dev/null 2>&1; then
  fail "agent can't reach Postgres"
else
  pass "agent can't reach Postgres"
fi
# admin replaces .env with a new file, which the agent's mount only picks up on restart.
expect_ok "slack sets the home channel" admin_stdin 'SLACK_HOME_CHANNEL=C0TEST\n' slack default --from-stdin
compose restart agent-default >/dev/null
expect "the agent sees admin's .env changes after a restart" "SLACK_HOME_CHANNEL=C0TEST" \
  "$(compose exec -T agent-default sh -c 'grep "^SLACK_HOME_CHANNEL=" /opt/data/.env' 2>&1)"
compose stop agent-default >/dev/null

echo "== 6. tier_classifier.py internals"
if compose exec -T litellm python3 /tests/tier_classifier_checks.py; then
  pass "tier_classifier.py internals"
else
  fail "tier_classifier.py internals (see output above)"
fi

echo "== 7. Classifier fallbacks (restarts LiteLLM with different settings)"
export TEST_ROUTER_CLASSIFIER_MODEL=classifier-mock-invalid
restart_litellm
set_jev_mode fail
expect_auto "Both classifiers fail: heuristic routes a greeting to fast" "hi" "fast-model-answered" 1
expect "both classifiers reported no decision" "2" "$(log_count litellm "classifier did not decide")"
expect_no_plugin_errors "both classifiers fail"

export TEST_ROUTER_CLASSIFIER_MODEL=auto TEST_ROUTER_CLASSIFIERS=llm
restart_litellm
started="$(date +%s)"
reply="$(ask_auto "$reader_key" "hi")"
elapsed=$(($(date +%s) - started))
expect "Classifier model auto: request still answered" "fast-model-answered" "$reply"
if [ "$elapsed" -lt 5 ]; then
  pass "Classifier model auto: no recursion (${elapsed}s)"
else
  fail "Classifier model auto: no recursion (took ${elapsed}s)"
fi
expect "Classifier model auto: refused with an error" "1" "$(log_count litellm "skipping llm classifier.*is a router")"
expect_no_plugin_errors "classifier model auto"
unset TEST_ROUTER_CLASSIFIER_MODEL TEST_ROUTER_CLASSIFIERS

export TEST_ROUTER_CLASSIFIERS=llm
restart_litellm
set_jev_mode SIMPLE
expect_auto "ROUTER_CLASSIFIERS=llm uses the LLM classifier" "What is the capital of France?" "strong-model-answered" 0
expect_no_plugin_errors "ROUTER_CLASSIFIERS=llm"
unset TEST_ROUTER_CLASSIFIERS

export TEST_TYPESAFE_API_KEY=""
restart_litellm
expect_auto "No TypeSafe key: LLM classifier decides" "What is the capital of France?" "strong-model-answered" 0
expect "No TypeSafe key: Jev skipped with a warning" "1" "$(log_count litellm "skipping jev classifier")"
expect_no_plugin_errors "no TypeSafe key"
unset TEST_TYPESAFE_API_KEY

# A server with an empty credential loads but is unusable, so admin must refuse it. One that's only
# down upstream is granted with a warning.
export TEST_ROVO_JIRA_WRITE_API_KEY=""
restart_litellm
expect "servers marks a server with an empty credential" "rovo_jira_write  (unusable: check its credential)" \
  "$(admin servers 2>&1 | grep rovo_jira_write)"
expect_refused "grant refuses a server with an empty credential" admin grant writer --mcp rovo_read,rovo_jira_write
expect "a refused grant changes nothing" "rovo_jira_write" "$(agent_field writer mcp_servers)"
compose stop fake-rovo >/dev/null
expect_ok "grant allows a server that's down upstream" admin grant writer --mcp rovo_read
expect "grant warns about a server it couldn't check" "1" \
  "$(grep -c "couldn't check MCP server 'rovo_read'" "$TEST_WORK/out")"
compose start fake-rovo >/dev/null
unset TEST_ROVO_JIRA_WRITE_API_KEY
restart_litellm
expect_ok "grant writer back to rovo_jira_write" admin grant writer --mcp rovo_jira_write

echo "== 8. Prompt-cache markers"
# cache-check-<tier> are unmocked copies of the tier entries, pointed at tests/fake_anthropic.py.
cache_request='{"model": "MODEL", "messages": [{"role": "system", "content": "You are a test."}, {"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}, {"role": "user", "content": "Again"}]}'
for tier in fast balanced strong; do
  before="$(log_count fake-anthropic "anthropic request")"
  status="$(request POST /v1/chat/completions "$master_key" "${cache_request/MODEL/cache-check-$tier}")"
  expect "$tier request succeeds" "200" "$status"
  log_count fake-anthropic "anthropic request" >/dev/null
  expect "$tier request marks the system prompt and latest message for caching" \
    "system_cached=True last_cached=True" \
    "$(grep "anthropic request" "$TEST_WORK/fake-anthropic.log" | tail -n +$((before + 1)) | sed -E 's/.*(system_cached=[A-Za-z]+ last_cached=[A-Za-z]+).*/\1/')"
done

echo "== 9. GitHub"
compose up -d --wait --wait-timeout 180 workspace-default >/dev/null && compose up -d --no-deps agent-default >/dev/null
# Runs a command in the agent's workspace as Hermes does: SSH from the agent container, as the
# Hermes user, with its key and the pinned host key.
ws() {
  compose exec -T -u "${HERMES_UID:-10000}" agent-default \
    ssh -p 2222 -i /opt/data/ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=10 workspace@workspace-default "$1"
}
expect "github-token has credentials for each level" "github_read: configured github_pr: configured github_test_write: configured" \
  "$(compose exec -T github-token python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/healthz").read().decode())' 2>&1 |
    tr '\n' ' ' | sed 's/ *$//')"
expect "workspace gh is the pinned version" "2.103.3" \
  "$(ws 'gh --version' 2>&1 | sed -n 's/^gh version \([0-9.]*\).*/\1/p')"
expect "workspace gh gets a github_read token" "ghs_test_1" "$(ws 'gh auth token' 2>&1)"
expect "github-token reuses an unexpired token" "ghs_test_1" "$(ws 'gh auth token' 2>&1)"
git_credential() {
  ws "printf 'protocol=https\nhost=github.com\n\n' | ${1:-} git credential fill" 2>&1 | sed -n 's/^password=//p'
}
expect "workspace git gets GitHub credentials through gh" "ghs_test_1" "$(git_credential)"
expect "workspace git gets no credentials for a level it wasn't granted" "" "$(git_credential GH_LEVEL=github_test_write)"
expect "workspace commits are authored and committed as the bot" \
  "$GIT_BOT_NAME <$GIT_BOT_EMAIL> $GIT_BOT_NAME <$GIT_BOT_EMAIL>" \
  "$(ws 'echo $(git var GIT_AUTHOR_IDENT | sed "s/>.*/>/") $(git var GIT_COMMITTER_IDENT | sed "s/>.*/>/")' 2>&1)"
expect "github_read token: valid App JWT, levels.json permissions, no repository limit" "1" \
  "$(log_count_fixed fake-github "mint path_ok=True jwt_ok=True permissions=$(python3 -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1]))["github_read"]["permissions"], sort_keys=True, separators=(",", ":")))' "$root/github-token/levels.json") repositories=null")"
if ws 'GH_LEVEL=github_test_write gh auth token' >"$TEST_WORK/out" 2>&1; then
  fail "workspace gh refused a level it wasn't granted (got a token)"
else
  expect "workspace gh refused a level it wasn't granted" "1" "$(grep -c "not been granted github_test_write" "$TEST_WORK/out")"
fi

# Requests github-token directly from the agent's network, with any key.
token_request() {
  compose exec -T -e KEY="$1" -e LEVEL="$2" agent-default sh -c '
    url="http://github-token:8080/token/$LEVEL"
    if [ -n "$KEY" ]; then
      curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $KEY" "$url"
    else
      curl -s -o /dev/null -w "%{http_code}" "$url"
    fi'

}
expect "a granted write level gets a token" "200" "$(token_request "$writer_key" github_test_write)"
expect "write token: write permissions, limited to the level's repositories" "1" \
  "$(log_count_fixed fake-github 'permissions={"contents":"write","metadata":"read","pull_requests":"write"} repositories=["openmrs-test-repo"]')"
expect "grant changes reach the workspace key" "200" "$(token_request "$(workspace_key writer)" github_test_write)"
expect "a key without the grant is refused (403)" "403" "$(token_request "$writer_key" github_read)"
expect "no key is refused (401)" "401" "$(token_request "" github_read)"
expect "an unknown key is refused (401)" "401" "$(token_request "sk-not-a-key" github_read)"
expect "a revoked key is refused even though it had the grant (401)" "401" "$(token_request "$new_nobody_key" github_read)"
expect "an unknown level is refused (404)" "404" "$(token_request "$reader_key" github_admin)"
expect "github-token asked GitHub for exactly two tokens" "2" "$(log_count fake-github "^mint ")"
# A line of the PEM and a stretch of its base64 form, from the middle where they're unique to
# this key.
github_key_markers="$(sed -n 5p "$TEST_WORK/github-app.pem")
$(printf '%s' "$github_key_base64" | cut -c 400-460)"
expect "agent environment and data hold no GitHub App key (markers, files)" "2 0" \
  "$(printf '%s\n' "$github_key_markers" | compose exec -T agent-default sh -c '
    cat > /tmp/markers; env > /tmp/agent-env
    echo "$(grep -c . /tmp/markers) $(grep -rlF -f /tmp/markers /tmp/agent-env /opt/data | wc -l | tr -d " ")"
    rm /tmp/markers /tmp/agent-env' || echo error)"
expect "workspace environment and files hold no GitHub App key (markers, files)" "2 0" \
  "$(printf '%s\n' "$github_key_markers" | compose exec -T workspace-default sh -c '
    cat > /tmp/markers; env > /tmp/workspace-env
    echo "$(grep -c . /tmp/markers) $(grep -rlF -f /tmp/markers /tmp/workspace-env /config /run /work | wc -l | tr -d " ")"
    rm /tmp/markers /tmp/workspace-env' || echo error)"

echo "== 10. Workspaces"
expect "workspace runs commands as the unprivileged user on a read-only root filesystem" "20000 read-only" \
  "$(ws 'id -u; touch /etc/x 2>/dev/null || echo read-only' 2>&1 | tr '\n' ' ' | sed 's/ $//')"
expect "workspace has each JDK, 21 by default, and Maven and Node" "21.0 1.8 11.0 17.0 21.0 ok" \
  "$(ws 'java -version 2>&1 | head -n 1 | cut -d\" -f2 | cut -d. -f1-2
    for v in 8 11 17 21; do /opt/java/$v/bin/java -version 2>&1 | head -n 1 | cut -d\" -f2 | cut -d. -f1-2; done
    mvn -v >/dev/null && node --version >/dev/null && echo ok' 2>&1 | tr '\n' ' ' | sed 's/ $//')"
expect "workspace clones a branch and a pull request's head" "main pr $(git_test rev-parse pr)" \
  "$(ws 'git clone -q -b main --filter=blob:none git://fake-git:8005/test-repo repo && cd repo && cat marker &&
    git fetch -q origin pull/7/head && git -c advice.detachedHead=false checkout -q FETCH_HEAD && cat marker &&
    git rev-parse HEAD' 2>&1 | tr '\n' ' ' | sed 's/ $//')"
expect "workspace user can't read its configuration or host key" "denied denied" \
  "$(ws 'for f in /config/litellm_key /run/workspace/ssh_host_ed25519_key; do
    cat $f >/dev/null 2>&1 && echo readable || echo denied; done' 2>&1 | tr '\n' ' ' | sed 's/ $//')"
expect "workspace can't reach LiteLLM or Postgres" "unreachable unreachable" \
  "$(ws 'for target in litellm:4000 postgres:5432; do
    python3 -c "import socket, sys; h, p = sys.argv[1].split(\":\"); socket.create_connection((h, int(p)), timeout=3)" \
      $target 2>/dev/null && echo reachable || echo unreachable; done' 2>&1 | tr '\n' ' ' | sed 's/ $//')"
expect "workspace holds none of the agent's secrets" "0" \
  "$(printf '%s\n' "$reader_key" xoxb-test xapp-test | compose exec -T workspace-default sh -c '
    cat > /tmp/markers; env > /tmp/workspace-env
    grep -rlF -f /tmp/markers /tmp/workspace-env /config /run /work | wc -l | tr -d " "
    rm /tmp/markers /tmp/workspace-env' || echo error)"
expect_refused "workspace refuses any other SSH key" compose exec -T -u "${HERMES_UID:-10000}" agent-default sh -c '
  ssh-keygen -q -t ed25519 -N "" -f /tmp/other &&
    ssh -p 2222 -i /tmp/other -o IdentitiesOnly=yes -o BatchMode=yes workspace@workspace-default true
  status=$?; rm -f /tmp/other /tmp/other.pub; exit $status'
expect_refused "workspace refuses root logins" compose exec -T -u "${HERMES_UID:-10000}" agent-default \
  ssh -p 2222 -i /opt/data/ssh/id_ed25519 -o BatchMode=yes root@workspace-default true
# Hermes connects with StrictHostKeyChecking=accept-new, which must still refuse a changed key.
expect_refused "agent refuses a workspace whose host key isn't the pinned one" \
  compose exec -T -u "${HERMES_UID:-10000}" agent-default sh -c '
  ssh-keygen -q -t ed25519 -N "" -f /tmp/fake && echo "[workspace-default]:2222 $(cut -d" " -f1,2 /tmp/fake.pub)" > /tmp/kh &&
    ssh -p 2222 -i /opt/data/ssh/id_ed25519 -o BatchMode=yes -o GlobalKnownHostsFile=/tmp/kh \
      -o StrictHostKeyChecking=accept-new workspace@workspace-default true
  status=$?; rm -f /tmp/fake /tmp/fake.pub /tmp/kh; exit $status'
# Leaves files, a background process, and a cache entry, then lets the idle limit (15 seconds in
# tests/compose.test.yaml) pass with no one logged in. Counts resets only after the login, so a
# reset from earlier logins can't pass for this one.
ws 'echo x > ~/left; echo x > /tmp/left; echo x > /dev/shm/left; touch /cache/m2/kept
  (setsid sleep 600 >/dev/null 2>&1 &)' >/dev/null 2>&1
resets_before="$(log_count_fixed workspace-default "reset after going idle")"
compose stop agent-default >/dev/null
# Waits up to three minutes for a reset after $resets_before, and prints whether one happened. On a
# timeout it prints the workspace's log, which shows its logins.
wait_for_reset() {
  for _ in $(seq 1 60); do
    [ "$(log_count_fixed workspace-default "reset after going idle")" -gt "$resets_before" ] && { echo reset; return; }
    sleep 3
  done
  echo "no reset"
  compose logs -t workspace-default 2>&1 | grep -v "Connection closed by 127" | tail -n 20 | sed 's/^/    /' >&2
}
expect "workspace resets after going idle, despite a leftover process" "reset" "$(wait_for_reset)"
# Prints which of the leftover files still exist, and "leftover-process" if any process of the
# workspace user is still running.
leftovers() {
  compose exec -T workspace-default sh -c '
    for f in /work/home/left /tmp/left /dev/shm/left /cache/m2/kept; do [ -e $f ] && echo $f; done
    grep -l "^Uid:[[:space:]]*20000" /proc/[0-9]*/status 2>/dev/null | sed "s/.*/leftover-process/" | sort -u
    true' | tr '\n' ' ' | sed 's/ $//'
}
expect "a reset leaves no files or processes, only the caches" "/cache/m2/kept" "$(leftovers)"

# With WORKSPACE_KEEP_FILES, a reset still kills processes but keeps /work, also across restarts.
# The recreated workspace's only session lasts milliseconds, shorter than a poll, so this also
# checks that every login counts as use.
export TEST_WORKSPACE_KEEP_FILES=true
compose up -d --wait --wait-timeout 180 --force-recreate --no-deps workspace-default >/dev/null
compose up -d --no-deps agent-default >/dev/null
ws 'echo x > ~/left; echo x > /tmp/left; echo x > /dev/shm/left; touch /cache/m2/kept
  (setsid sleep 600 >/dev/null 2>&1 &)' >/dev/null 2>&1
resets_before="$(log_count_fixed workspace-default "reset after going idle")"
compose stop agent-default >/dev/null
expect "WORKSPACE_KEEP_FILES: workspace resets after going idle" "reset" "$(wait_for_reset)"
expect "WORKSPACE_KEEP_FILES: a reset kills processes but keeps /work" "/work/home/left /cache/m2/kept" \
  "$(leftovers)"
compose restart workspace-default >/dev/null
expect "WORKSPACE_KEEP_FILES: /work survives a restart" "/work/home/left /cache/m2/kept" "$(leftovers)"
unset TEST_WORKSPACE_KEEP_FILES

# Files an agent copies from its image can carry that agent's own SELinux label; admin must still
# be able to remove them.
expect_ok "delete --purge an agent that has run" admin delete default --purge --yes
expect "purge removes all of a run agent's data and its workspace configuration" "absent absent" \
  "$(admin_sh 'for p in /agents/default /workspaces/default; do test -e $p && echo present || echo absent; done' |
    tr '\n' ' ' | sed 's/ $//')"

echo
if [ "$failures" -eq 0 ]; then
  echo "All checks passed."
else
  echo "$failures check(s) failed."
  exit 1
fi
