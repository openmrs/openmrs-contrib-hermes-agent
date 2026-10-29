#!/usr/bin/env bash
#
# Repeatable checks for this stack's security properties and for the parts that depend on LiteLLM
# and Hermes internals. Re-run after changing any image in compose.yaml or a Dockerfile,
# compose.yaml itself, litellm/config.yaml, litellm/tier_classifier.py, hermes/config.yaml,
# hermes/image/, admin/, or github-token/ (including levels.json).
#
# Usage: tests/run.sh
#
# Uses fake credentials, mock models, and fake Jev, Rovo MCP, Anthropic, and GitHub endpoints, so
# nothing reaches a real provider. Runs as a separate Compose project ("hermes-test") with its own
# volumes, and removes its containers and volumes on exit, so it does not touch a running stack.
# Requests to LiteLLM are made from inside its container, so no host port is needed. Needs Docker
# Compose 2.24.4 or later (for the !override tag in tests/compose.test.yaml), and python3, openssl,
# shasum, base64, and git on the host.
#
# What it checks:
#   1. compose.yaml keeps secrets, volumes, and networks separated, and every service runs with
#      no-new-privileges and only the capabilities it needs (static).
#   2. The admin tool: create / grant / rotate / slack / delete, key lockdown (no management API,
#      no provider passthroughs, agent models only), per-agent monthly budgets that survive key
#      rotation, per-key MCP access with the right injected credential, and the interactive
#      interface.
#   3. Every Hermes side task resolves to LiteLLM's `fast` model, including tasks that resolve a
#      full runtime (background review, curator).
#   4. The `auto` router's classifier chain: Jev decides; Jev fails -> LLM classifier decides.
#   5. Hermes end to end: context length is 512000 for `auto`, chat goes through the router,
#      session titles come from `fast`, the read-only SOUL.md reaches the model, MCP reaches only
#      the granted server, and the agent holds no other secrets and can't reach Postgres.
#   6. tier_classifier.py internals: refuses router classifier models, follows Router rebuilds,
#      declines re-entry.
#   7. Classifier fallbacks: both fail -> heuristic; classifier model `auto` is refused without
#      recursing; ROUTER_CLASSIFIERS=llm skips Jev; no TypeSafe key skips Jev. The admin tool
#      refuses an MCP server with an empty credential, and only warns about one that's down.
#   8. Requests to each tier model carry prompt-cache markers on the system prompt and the
#      latest message.
#   9. The agent's gh gets GitHub App tokens from github-token only for the levels its key was
#      granted; tokens carry each level's permissions and repository limits, are reused, and need
#      a valid, unrevoked key; the App key never reaches the agent.
#  10. The sandbox builds a branch or pull request of a repository (from a local git server) with
#      the chosen JDK, only for keys granted it; runs builds as an unprivileged user on a
#      read-only root filesystem; leaves no processes or files for the next job except its caches;
#      times jobs out, runs one at a time, and holds no secrets. Then purging the agents removes
#      all of their data.
#
# Environment:
#   COMPOSE            Compose command (default "docker compose"; e.g. "docker-compose")
#   TEST_LITELLM_PORT  Host port the test LiteLLM publishes (default 14000; not used by the tests)

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
compose_cmd="${COMPOSE:-docker compose}"
export TEST_LITELLM_PORT="${TEST_LITELLM_PORT:-14000}"
master_key="sk-test-master"
expected_context=512000
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

# Logs are written to a file before searching: grep -q on a pipe can exit early, and under
# pipefail the writer's SIGPIPE would turn a match into a failed (non-matching) pipeline.
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

# Runs the admin tool, as an operator would.
admin() {
  compose run --rm -T admin "$@"
}

# Runs the admin tool with the given text on stdin (for --slack-from-stdin / --from-stdin).
admin_stdin() {
  local input="$1"
  shift
  printf '%b' "$input" | compose run --rm -T admin "$@"
}

# Runs a shell command in the admin container, which mounts the whole agents-data volume at /agents.
admin_sh() {
  compose run --rm -T --entrypoint sh admin -c "$1"
}

agent_key() {
  admin_sh "sed -n 's/^LITELLM_API_KEY=//p' /agents/$1/.env"
}

# Sends an HTTP request to LiteLLM; prints the status code and leaves the body in $TEST_WORK/body.
request() {
  local method="$1" path="$2" key="$3" data="${4:-}"
  [ -n "$data" ] || data='{}'
  compose exec -T litellm python3 /tests/http_request.py "$method" "$path" "$key" "$data" > "$TEST_WORK/response"
  tail -n +2 "$TEST_WORK/response" > "$TEST_WORK/body"
  head -n 1 "$TEST_WORK/response"
}

# Prints the reply text from the tier model the `auto` router picked.
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

# Calls the fake Rovo `whoami` tool on one MCP server; prints the account LiteLLM authenticated
# as ("read"/"jira-write"/"confluence-write"/"none"), "denied", or "error <status>: <body>".
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
# A throwaway GitHub App key pair: github-token signs with the private key, fake-github verifies
# with the public one.
openssl genrsa -out "$TEST_WORK/github-app.pem" 2048 2>/dev/null
openssl rsa -in "$TEST_WORK/github-app.pem" -pubout -out "$TEST_WORK/github-app.pub.pem" 2>/dev/null
# Both levels share the test App; github_test_write exists only in the tests, to check that write
# permissions and repository limits reach GitHub and that grants are enforced per level.
github_key_base64="$(base64 < "$TEST_WORK/github-app.pem" | tr -d '\n')"
for prefix in GITHUB_READ GITHUB_TEST_WRITE; do
  echo "${prefix}_APP_ID=12345"
  echo "${prefix}_INSTALLATION_ID=67890"
  echo "${prefix}_PRIVATE_KEY_BASE64=$github_key_base64"
done > "$TEST_WORK/github-app.env"
python3 -c 'import json, sys
levels = json.load(open(sys.argv[1]))
levels["github_test_write"] = {"permissions": {"contents": "write", "metadata": "read", "pull_requests": "write"},
                               "repositories": ["openmrs-test-repo"]}
json.dump(levels, open(sys.argv[2], "w"), indent=2)' "$root/github-token/levels.json" "$TEST_WORK/github-levels.json"
# The sandbox's test repository: `main` and a pull request (refs/pull/7/head) that differ in one
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
# The static checks resolve env files against placeholder secrets in their own project directory
# (see tests/compose_isolation.py), so real secrets/*.env never reach the output.
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
compose --profile admin build admin agent-default github-token sandbox >/dev/null
compose up -d --wait --wait-timeout 180 postgres litellm github-token sandbox fake-jev fake-rovo fake-anthropic \
  fake-github fake-git >/dev/null

echo "== 2. Admin tool"
expect "servers lists the configured MCP servers" '["rovo_confluence_write", "rovo_jira_write", "rovo_read", "sandbox"]' "$(admin servers --json)"
expect "levels lists the GitHub access levels" '["github_read", "github_test_write"]' "$(admin levels --json)"
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

# A key the tool didn't create must be invisible to it and untouched.
request POST /key/generate "$master_key" '{"key_alias": "someone-else", "metadata": {"hermes_agent": "default"}}' >/dev/null
unmanaged_key="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["key"])' "$TEST_WORK/body")"
expect "keys without the managed_by marker are ignored" "1" "$(agent_field default keys)"

reader_key="$(agent_key default)"
writer_key="$(agent_key writer)"
nobody_key="$(agent_key nobody)"

# Agent keys may call /key/info (github-token uses it to read their grants), but only about
# themselves: each agent's keys have their own user_id.
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
# Agents created before budgets existed have a key but no LiteLLM user; their next grant gives
# them one.
request POST /key/generate "$master_key" '{"user_id": "hermes-agent-legacy",
  "metadata": {"managed_by": "hermes-admin", "hermes_agent": "legacy", "github_levels": []}}' >/dev/null
expect "an agent without a LiteLLM user has no budget" "None" "$(agent_field legacy budget)"
expect_ok "grant legacy without a budget" admin grant legacy --no-mcp
expect "grant gives an agent without a budget the default" "1000.0" "$(agent_field legacy budget)"
expect_ok "delete legacy --purge" admin delete legacy --purge --yes

expect_ok "grant nobody github_read" admin grant nobody --github github_read
expect_ok "rotate nobody" admin rotate nobody
new_nobody_key="$(agent_key nobody)"
expect "rotate revokes the old key" "401" "$(request GET /v1/models "$nobody_key")"
expect "rotate's new key works" "200" "$(request GET /v1/models "$new_nobody_key")"
expect "rotate leaves one key" "1" "$(agent_field nobody keys)"
expect "rotate keeps GitHub grants" "github_read" "$(agent_field nobody github_levels)"

# The budget is on the agent's LiteLLM user, so it covers all of the agent's keys and a rotated key
# can't reset the month's spend. LiteLLM refuses requests (422) once spend reaches the budget.
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
expect_ok "re-create a deleted agent, reusing its data" admin create nobody
expect_ok "delete nobody --purge" admin delete nobody --purge --yes
expect "purge removes the agent's data" "absent" "$(admin_sh 'test -e /agents/nobody && echo present || echo absent')"
expect "purge removes the agent's LiteLLM user" "404" "$(request GET /user/info?user_id=hermes-agent-nobody "$master_key")"
expect "unmanaged key untouched" "200" "$(request GET /v1/models "$unmanaged_key")"

# `admin compose` must print exactly the agent-default block in compose.yaml, and a block for a new
# agent must pass the static isolation checks once added to compose.yaml.
admin compose default > "$TEST_WORK/compose-default.yaml" 2>/dev/null || true
if [ -s "$TEST_WORK/compose-default.yaml" ] &&
  python3 -c 'import sys; sys.exit(open(sys.argv[2]).read() not in open(sys.argv[1]).read())' \
    "$root/compose.yaml" "$TEST_WORK/compose-default.yaml"; then
  pass "compose prints the agent-default block in compose.yaml"
else
  fail "compose prints the agent-default block in compose.yaml (got: $(cat "$TEST_WORK/compose-default.yaml"))"
fi
# The block relies on compose.yaml's x-hermes-agent anchor, so it's inserted into a copy of
# compose.yaml (before the top-level networks: key) rather than passed as an override file.
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
if compose run --rm --no-deps -T -e HERMES_HOME=/check-home -e LITELLM_API_KEY=sk-placeholder \
  -v "$TEST_WORK/check-home:/check-home" -v "$root/tests/hermes_side_tasks.py:/check.py:ro" \
  --entrypoint /opt/hermes/.venv/bin/python agent-default /check.py; then
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
# The LLM classifier's own call is charged to the agent whose request it routes, so it counts
# against the agent's budget. Spend logs are written asynchronously, so this polls.
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
compose up -d --no-deps agent-default >/dev/null
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
# The agent's SOUL.md is the repo file, mounted read-only: Hermes neither replaces it at boot nor
# can write to it, and its text reaches the model. The test config (tests/make_litellm_config.py)
# has LiteLLM store prompts in its spend logs, written asynchronously, so the prompt check polls.
soul_file="$root/hermes/souls/default.md"
expect "SOUL.md in the agent is hermes/souls/default.md" "$(shasum -a 256 < "$soul_file" | cut -d' ' -f1)" \
  "$(compose exec -T agent-default cat /opt/data/SOUL.md | shasum -a 256 | cut -d' ' -f1)"
expect_refused "agent can't write its SOUL.md" compose exec -T agent-default sh -c 'echo changed >> /opt/data/SOUL.md'
# config.yaml is likewise the repo file, read-only. Its _config_version must be the image's, or
# Hermes' startup migration would try to rewrite it.
expect "config.yaml in the agent is hermes/config.yaml" "$(shasum -a 256 < "$root/hermes/config.yaml" | cut -d' ' -f1)" \
  "$(compose exec -T agent-default cat /opt/data/config.yaml | shasum -a 256 | cut -d' ' -f1)"
expect_refused "agent can't write its config.yaml" compose exec -T agent-default sh -c 'echo changed >> /opt/data/config.yaml'
expect "hermes/config.yaml's _config_version is the Hermes image's" \
  "$(compose exec -T agent-default /opt/hermes/.venv/bin/python -c \
    'from hermes_cli.config_defaults import DEFAULT_CONFIG; print(DEFAULT_CONFIG["_config_version"])' 2>&1)" \
  "$(sed -n 's/^_config_version: *//p' "$root/hermes/config.yaml")"
expect "Hermes started without a config migration warning" "0" \
  "$(log_count_fixed agent-default "docker_config_migrate.py failed")"
# Builds belong in the sandbox: the agent's terminal blocks build and package-manager commands, in
# any part of a command, and still runs everything else.
expect "the agent's terminal blocks build tools (approvals.deny)" \
  "mvn * */mvnw* */mvnw* yarn * npx * node * npm * None None None" \
  "$(compose exec -T -u "${HERMES_UID:-10000}" -w /opt/hermes agent-default python -c '
from tools.approval_floors import _match_user_deny_rule
for command in ["mvn -B verify", "cd x && ./mvnw verify", "sh ./mvnw verify", "yarn install", "npx turbo build",
                "node build.js", "sh -c \"npm ci\"", "git status", "gh pr diff 7", "grep -rn yarn ."]:
    print(_match_user_deny_rule(command))' 2>/dev/null | tr '\n' ' ' | sed 's/ $//')"
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

# The agent's environment and its whole Hermes home must not contain any LiteLLM, master, or
# Postgres secret.
secrets="$(cat "$root/tests/fixtures/litellm.env" "$root/tests/fixtures/master.env" "$root/tests/fixtures/postgres.env" |
  grep -v -E '^(#|$|LLM_MODEL_)' | cut -d= -f2-)"
scan="$(printf '%s\n' "$secrets" | compose exec -T agent-default sh -c '
  env > /tmp/agent-env; checked=0; leaked=0
  while read -r s; do
    checked=$((checked + 1))
    if grep -rqF -- "$s" /tmp/agent-env /opt/data; then leaked=$((leaked + 1)); fi
  done
  rm /tmp/agent-env; echo "$checked $leaked"' || true)"
# Comparing the count of checked secrets keeps a failed exec from passing as "nothing leaked".
expect "agent environment and data hold no LiteLLM, master, or Postgres secrets (checked, leaked)" \
  "$(printf '%s\n' "$secrets" | wc -l | tr -d ' ') 0" "$scan"
if compose exec -T agent-default python3 -c 'import socket; socket.create_connection(("postgres", 5432), timeout=3)' \
  >/dev/null 2>&1; then
  fail "agent can't reach Postgres"
else
  pass "agent can't reach Postgres"
fi
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

# An MCP server with an empty credential still loads, but LiteLLM can't use it: the admin tool must
# refuse to grant it. A server that's only down upstream is granted, with a warning.
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
compose up -d --no-deps agent-default >/dev/null
expect "github-token has credentials for each level" "github_read: configured github_test_write: configured" \
  "$(compose exec -T github-token python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/healthz").read().decode())' 2>&1 |
    tr '\n' ' ' | sed 's/ *$//')"
expect "agent gh is the pinned version" "2.103.3" \
  "$(compose exec -T agent-default gh --version 2>&1 | sed -n 's/^gh version \([0-9.]*\).*/\1/p')"
expect "agent gh gets a github_read token" "ghs_test_1" "$(compose exec -T agent-default gh auth token 2>&1)"
expect "github-token reuses an unexpired token" "ghs_test_1" "$(compose exec -T agent-default gh auth token 2>&1)"
expect "github_read token: valid App JWT, levels.json permissions, no repository limit" "1" \
  "$(log_count_fixed fake-github "mint path_ok=True jwt_ok=True permissions=$(python3 -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1]))["github_read"]["permissions"], sort_keys=True, separators=(",", ":")))' "$root/github-token/levels.json") repositories=null")"
if compose exec -T -e GH_LEVEL=github_test_write agent-default gh auth token >"$TEST_WORK/out" 2>&1; then
  fail "agent gh refused a level it wasn't granted (got a token)"
else
  expect "agent gh refused a level it wasn't granted" "1" "$(grep -c "not been granted github_test_write" "$TEST_WORK/out")"
fi

# Direct requests to github-token, from the agent's network, with other agents' keys.
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
expect "a key without the grant is refused (403)" "403" "$(token_request "$writer_key" github_read)"
expect "no key is refused (401)" "401" "$(token_request "" github_read)"
expect "an unknown key is refused (401)" "401" "$(token_request "sk-not-a-key" github_read)"
expect "a revoked key is refused even though it had the grant (401)" "401" "$(token_request "$new_nobody_key" github_read)"
expect "an unknown level is refused (404)" "404" "$(token_request "$reader_key" github_admin)"
expect "github-token asked GitHub for exactly two tokens" "2" "$(log_count fake-github "^mint ")"
# One line of the PEM and a stretch of its base64 form (as in github-app.env), both from the middle,
# where they're unique to this key.
github_key_markers="$(sed -n 5p "$TEST_WORK/github-app.pem")
$(printf '%s' "$github_key_base64" | cut -c 400-460)"
expect "agent environment and data hold no GitHub App key (markers, files)" "2 0" \
  "$(printf '%s\n' "$github_key_markers" | compose exec -T agent-default sh -c '
    cat > /tmp/markers; env > /tmp/agent-env
    echo "$(grep -c . /tmp/markers) $(grep -rlF -f /tmp/markers /tmp/agent-env /opt/data | wc -l | tr -d " ")"
    rm /tmp/markers /tmp/agent-env' || echo error)"
compose stop agent-default >/dev/null

echo "== 10. Sandbox"
expect_ok "create builder (sandbox)" admin create builder --mcp sandbox
builder_key="$(agent_key builder)"
request GET /v1/mcp/server "$master_key" >/dev/null
sandbox_id="$(python3 -c 'import json, sys; print(next(s["server_id"] for s in json.load(open(sys.argv[1])) if s["server_name"] == "sandbox"))' "$TEST_WORK/body")"

# Calls a sandbox tool through LiteLLM; prints the tool's text result, "denied", or
# "error <status>: <body>".
sandbox_tool() {
  local key="$1" tool="$2" arguments="$3" status
  status="$(request POST /mcp-rest/tools/call "$key" \
    "{\"server_id\": \"$sandbox_id\", \"name\": \"$tool\", \"arguments\": $arguments}")"
  python3 - "$TEST_WORK/body" "$status" <<'PYTHON'
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
    print(f"error {status}: {body[:300]}")
PYTHON
}

job_field() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]))' "$TEST_WORK/job" "$1" 2>/dev/null
}

# Waits for a sandbox job to end, leaving its final job_status in $TEST_WORK/job.
wait_for_job() {
  for _ in $(seq 1 "${2:-60}"); do
    sandbox_tool "$builder_key" job_status "{\"job_id\": \"$1\"}" > "$TEST_WORK/job"
    [ "$(job_field state)" != "running" ] && return
    sleep 2
  done
}

# Starts a job with the builder's key and waits for it; prints its final state.
sandbox_job() {
  local job
  job="$(sandbox_tool "$builder_key" run_build "$1" |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["job_id"])' 2>/dev/null)" || { echo "not started"; return; }
  wait_for_job "$job"
  job_field state
}

expect "a key without the sandbox grant is refused" "denied" "$(sandbox_tool "$reader_key" list_jobs '{}')"
expect "sandbox builds main with JDK 8" "succeeded" \
  "$(sandbox_job '{"repository": "test-repo", "ref": "main", "command": "cat marker; java -version", "java_version": 8}')"
expect "main build: the checked-out file and JDK" "1 1" \
  "$(job_field output_tail | grep -cx main) $(job_field output_tail | grep -c 'version "1.8.0')"
expect "sandbox builds a pull request's head with JDK 21" "succeeded" \
  "$(sandbox_job '{"repository": "test-repo", "ref": "pull/7", "command": "cat marker; java -version"}')"
expect "pull request build: the checked-out file, commit, and JDK" "1 $(git_test rev-parse pr) 1" \
  "$(job_field output_tail | grep -cx pr) $(job_field commit) $(job_field output_tail | grep -c 'version "21\.')"
# The first job leaves files, a background process, and a cache entry; the second checks what's left.
expect "a build with leftovers succeeds" "succeeded" \
  "$(sandbox_job '{"repository": "test-repo", "ref": "main", "command": "id -u; touch /etc/x 2>/dev/null || echo read-only; echo x > /tmp/left; echo x > /dev/shm/left; touch /cache/m2/kept; (setsid sleep 600 >/dev/null 2>&1 &)"}')"
expect "builds run as the unprivileged build user on a read-only root filesystem" "1 1" \
  "$(job_field output_tail | grep -cx 20000) $(job_field output_tail | grep -cx read-only)"
sandbox_job '{"repository": "test-repo", "ref": "main", "command": "for f in /tmp/left /dev/shm/left /cache/m2/kept; do [ -e $f ] && echo $f; done; for p in /proc/[0-9]*/cmdline; do [ \"$(head -c 6 $p 2>/dev/null | tr \"\\\\0\" \" \")\" = \"sleep \" ] && echo leftover-process; done; true"}' >/dev/null
expect "a job leaves no files or processes for the next, only its caches" "/cache/m2/kept" \
  "$(job_field output_tail | grep -E '^(/|leftover)' | tr '\n' ' ' | sed 's/ $//')"
expect "sandbox refuses a repository outside the organization" "1" \
  "$(sandbox_tool "$builder_key" run_build '{"repository": "../other", "ref": "main", "command": "true"}' | grep -c "must be a repository name")"
job="$(sandbox_tool "$builder_key" run_build '{"repository": "test-repo", "ref": "main", "command": "sleep 600", "timeout_minutes": 1}' |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["job_id"])')"
expect "sandbox runs one job at a time" "1" \
  "$(sandbox_tool "$builder_key" run_build '{"repository": "test-repo", "ref": "main", "command": "true"}' | grep -c "still running")"
wait_for_job "$job" 45
expect "sandbox kills a job at its timeout" "timed_out" "$(job_field state)"
# GPG_KEY, from the python base image, is the fingerprint of the public CPython release key.
expect "sandbox holds no secrets" "" \
  "$(compose exec -T sandbox sh -c 'env | cut -d= -f1' | grep -E 'KEY|TOKEN|SECRET|PASS' | grep -vx GPG_KEY || true)"
expect_ok "delete builder --purge" admin delete builder --purge --yes

# Files an agent copies from its image can carry that agent's own SELinux label; admin must still
# be able to remove them.
expect_ok "delete --purge an agent that has run" admin delete default --purge --yes
expect "purge removes all of a run agent's data" "absent" "$(admin_sh 'test -e /agents/default && echo present || echo absent')"

echo
if [ "$failures" -eq 0 ]; then
  echo "All checks passed."
else
  echo "$failures check(s) failed."
  exit 1
fi
