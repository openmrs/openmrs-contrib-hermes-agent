"""Static checks that compose.yaml keeps secrets and networks separated and every service hardened,
used by tests/run.sh.

Usage:
  python3 tests/compose_isolation.py write-secrets DIR/secrets
  docker compose --project-directory DIR -f compose.yaml config --format json | python3 tests/compose_isolation.py

Compose 2.x merges env files into `environment` and drops `env_file` from `config` output, so
secret files can't be checked by path. `write-secrets` writes placeholder secret files that each set
one marker variable, and the checks look for markers in each service's environment. Run `config` in
a directory holding only those placeholders, so any other required env file makes it fail.

Every `agent-*` and `workspace-*` service is checked, so newly added agents are covered.
"""

import json
import os
import sys

AGENT_ENV_ALLOWED = {"HERMES_UID", "HERMES_GID", "HERMES_WRITE_SAFE_ROOT"}
WORKSPACE_ENV_ALLOWED = {"WORKSPACE_IDLE_MINUTES", "WORKSPACE_KEEP_FILES", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                         "GIT_COMMITTER_EMAIL"}
# Secret files and the only services allowed to receive them.
SECRET_FILES = {
    "litellm.env": {"litellm"},
    "master.env": {"litellm", "admin"},
    "onyx.env": {"onyx-api", "onyx-background", "onyx-mcp"},
    "onyx-db.env": {"onyx-db", "onyx-api", "onyx-background", "onyx-mcp"},
    "onyx-admin.env": {"admin"},
    "postgres.env": {"postgres"},
    "github-app.env": {"github-token"},
    "atlassian.env": {"mcp-atlassian"},
}
# mcp-atlassian takes each caller's key per request; any credential variable here (e.g.
# JIRA_API_TOKEN, ATLASSIAN_OAUTH_ACCESS_TOKEN, ALLOW_GLOBAL_CRED_FALLBACK) would let callers act
# without one.
MCP_ATLASSIAN_ENV_ALLOWED = {"ATLASSIAN_OAUTH_ENABLE", "TOOLSETS", "ENABLED_TOOLS"}
# Read-only binds an agent has beyond the four every agent has (target: source in the repository).
AGENT_EXTRA_BINDS = {
    "agent-reviewer": {
        "/etc/hermes/config.yaml": "hermes/reviewer/config.yaml",
        "/opt/data/plugins/github-review": "hermes/plugins/github-review",
        "/opt/data/skills": "hermes/reviewer/skills",
    },
}
# Named volumes an agent mounts read-only, and its workspace mounts read-write, beyond the above
# (target: volume): the supervisor's answer to a reset request, which the agent reads directly.
AGENT_EXTRA_VOLUMES = {"agent-reviewer": {"/opt/workspace-reset": "reviewer-reset"}}
WORKSPACE_EXTRA_VOLUMES = {"workspace-reviewer": {"/reset-status": "reviewer-reset"}}
# A workspace's cache volume, if not the shared workspace-cache. The reviewer builds untrusted PRs,
# so it gets its own: a poisoned cache can't reach other agents.
WORKSPACE_CACHE = {"workspace-reviewer": "reviewer-cache"}
# Services that only start with a profile: the reviewer needs its agent's data and webhook secret,
# which a host without a reviewer doesn't have.
SERVICE_PROFILES = {"agent-reviewer": ["reviewer"], "workspace-reviewer": ["reviewer"]}


def marker(secret):
    """The variable the placeholder for secrets/`secret` sets, e.g. SECRET_FILE_GITHUB_APP_ENV."""
    return "SECRET_FILE_" + secret.upper().replace(".", "_").replace("-", "_")


if sys.argv[1:2] == ["write-secrets"]:
    os.makedirs(sys.argv[2], exist_ok=True)
    for secret in SECRET_FILES:
        with open(os.path.join(sys.argv[2], secret), "w") as f:
            f.write(f"{marker(secret)}=1\n")
    sys.exit(0)

config = json.load(sys.stdin)
services = config["services"]
networks = config.get("networks", {})
failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}: {name}{'' if ok else f' ({detail})'}")
    if not ok:
        failures.append(name)


def secrets(service):
    """The secret files whose markers are in the service's environment, sorted."""
    environment = service.get("environment") or {}
    return sorted(secret for secret in SECRET_FILES if marker(secret) in environment)


def network_names(service):
    return set((service.get("networks") or {}).keys())


def mounts(service):
    return [v if isinstance(v, dict) else {"source": str(v).split(":")[0]} for v in service.get("volumes") or []]


agents = {name: s for name, s in services.items() if name.startswith("agent-")}
check("at least one agent service", bool(agents), "no agent-* services found")

subpaths = {}
for name, service in sorted(agents.items()):
    expected_subpath = name[len("agent-"):]
    # Also catches env files, whose variables are merged into the environment.
    extra = set((service.get("environment") or {}).keys()) - AGENT_ENV_ALLOWED
    check(f"{name}: no extra environment variables", not extra, sorted(extra))
    check(f"{name}: only on the agents and workspaces networks", network_names(service) == {"agents", "workspaces"},
          sorted(network_names(service)))
    check(f"{name}: has a workspace-{expected_subpath} service", f"workspace-{expected_subpath}" in services,
          sorted(services))
    service_mounts = mounts(service)
    agents_data = [m for m in service_mounts if m.get("source") == "agents-data"]
    data = [m for m in agents_data if m.get("target") == "/opt/data"]
    envs = [m for m in agents_data if m.get("target") == "/opt/data/.env"]
    souls = [m for m in service_mounts if m.get("target") == "/opt/data/SOUL.md"]
    configs = [m for m in service_mounts if m.get("target") == "/opt/data/config.yaml"]
    extra_binds = AGENT_EXTRA_BINDS.get(name, {})
    extras = [m for m in service_mounts if m.get("target") in extra_binds]
    extra_volumes = AGENT_EXTRA_VOLUMES.get(name, {})
    check(f"{name}: mounts only its own agents-data subdirectory, .env, SOUL.md, and config.yaml"
          + (f", and {', '.join(sorted([*extra_binds, *extra_volumes]))}" if extra_binds or extra_volumes else ""),
          len(service_mounts) == 4 + len(extra_binds) + len(extra_volumes) and len(agents_data) == 2 and len(data) == 1
          and len(envs) == 1 and len(souls) == 1 and len(configs) == 1 and len(extras) == len(extra_binds),
          service_mounts)
    for target, volume in sorted(extra_volumes.items()):
        found = [m for m in service_mounts if m.get("target") == target]
        check(f"{name}: {target} is the {volume} volume, read-only",
              len(found) == 1 and found[0].get("type") == "volume" and found[0].get("source") == volume
              and found[0].get("read_only") is True, found)
    for target, source in sorted(extra_binds.items()):
        found = [m for m in extras if m.get("target") == target]
        check(f"{name}: {target} is {source}, read-only",
              len(found) == 1 and found[0].get("type") == "bind" and found[0].get("read_only") is True
              and str(found[0].get("source", "")).replace("\\", "/").endswith(f"/{source}"),
              found)
    check(f"{name}: agents-data subdirectory is {expected_subpath!r}",
          len(data) == 1 and data[0].get("type") == "volume"
          and (data[0].get("volume") or {}).get("subpath") == expected_subpath,
          data)
    check(f"{name}: .env is {expected_subpath}/.env in agents-data, read-only",
          len(envs) == 1 and envs[0].get("type") == "volume" and envs[0].get("read_only") is True
          and (envs[0].get("volume") or {}).get("subpath") == f"{expected_subpath}/.env",
          envs)
    check(f"{name}: SOUL.md is hermes/souls/{expected_subpath}.md, read-only",
          len(souls) == 1 and souls[0].get("type") == "bind" and souls[0].get("read_only") is True
          and str(souls[0].get("source", "")).replace("\\", "/").endswith(f"/hermes/souls/{expected_subpath}.md"),
          souls)
    check(f"{name}: config.yaml is hermes/config.yaml, read-only",
          len(configs) == 1 and configs[0].get("type") == "bind" and configs[0].get("read_only") is True
          and str(configs[0].get("source", "")).replace("\\", "/").endswith("/hermes/config.yaml"),
          configs)
    for m in agents_data:
        subpath = (m.get("volume") or {}).get("subpath")
        check(f"{name}: agents-data subpath not shared", subpath not in subpaths,
              f"{subpath!r} also used by {subpaths.get(subpath)}")
        subpaths[subpath] = name

workspaces = {name: s for name, s in services.items() if name.startswith("workspace-")}
check("one workspace per agent", sorted(workspaces) == sorted(f"workspace-{a[len('agent-'):]}" for a in agents),
      sorted(workspaces))
config_subpaths = {}
for name, service in sorted(workspaces.items()):
    expected_subpath = name[len("workspace-"):]
    extra = set((service.get("environment") or {}).keys()) - WORKSPACE_ENV_ALLOWED
    check(f"{name}: no extra environment variables", not extra, sorted(extra))
    check(f"{name}: only on the workspaces and workspace-egress networks",
          network_names(service) == {"workspaces", "workspace-egress"}, sorted(network_names(service)))
    check(f"{name}: no published ports", not service.get("ports"), service.get("ports"))
    check(f"{name}: read-only root filesystem", service.get("read_only") is True, service.get("read_only"))
    # The workspace user can write /run (reset requests), so it must not fill the host's memory.
    run_tmpfs = [t for t in service.get("tmpfs") or [] if str(t).split(":")[0] == "/run"]
    check(f"{name}: /run tmpfs has a size limit", len(run_tmpfs) == 1 and "size=" in str(run_tmpfs[0]), run_tmpfs)
    service_mounts = mounts(service)
    configs = [m for m in service_mounts if m.get("source") == "workspace-config"]
    cache_volume = WORKSPACE_CACHE.get(name, "workspace-cache")
    caches = [m for m in service_mounts if m.get("source") == cache_volume and m.get("target") == "/cache"]
    work = [m for m in service_mounts if m.get("target") == "/work" and not m.get("source")]
    extra_volumes = WORKSPACE_EXTRA_VOLUMES.get(name, {})
    check(f"{name}: mounts only its own workspace-config subdirectory, the {cache_volume} cache, and an anonymous /work"
          + (f", and {', '.join(sorted(extra_volumes))}" if extra_volumes else ""),
          len(service_mounts) == 3 + len(extra_volumes) and len(configs) == 1 and len(caches) == 1 and len(work) == 1,
          service_mounts)
    for target, volume in sorted(extra_volumes.items()):
        found = [m for m in service_mounts if m.get("target") == target]
        check(f"{name}: {target} is the {volume} volume",
              len(found) == 1 and found[0].get("type") == "volume" and found[0].get("source") == volume, found)
    check(f"{name}: workspace-config subdirectory is {expected_subpath!r}, read-only",
          len(configs) == 1 and configs[0].get("type") == "volume" and configs[0].get("read_only") is True
          and configs[0].get("target") == "/config"
          and (configs[0].get("volume") or {}).get("subpath") == expected_subpath, configs)
    for m in configs:
        subpath = (m.get("volume") or {}).get("subpath")
        check(f"{name}: workspace-config subpath not shared", subpath not in config_subpaths,
              f"{subpath!r} also used by {config_subpaths.get(subpath)}")
        config_subpaths[subpath] = name

for name, profiles in sorted(SERVICE_PROFILES.items()):
    check(f"{name}: not started by default (profile {', '.join(profiles)})",
          (services.get(name) or {}).get("profiles") == profiles, (services.get(name) or {}).get("profiles"))

for name, service in services.items():
    check(f"{name}: no-new-privileges", "no-new-privileges:true" in (service.get("security_opt") or []),
          service.get("security_opt"))
    check(f"{name}: drops all capabilities", "ALL" in (service.get("cap_drop") or []), service.get("cap_drop"))
    check(f"{name}: not privileged", not service.get("privileged"))
    for m in mounts(service):
        if m.get("source") == "agents-data" and not (m.get("volume") or {}).get("subpath"):
            check(f"{name}: whole agents-data volume mounted only by admin", name == "admin", m)
        if m.get("source") == "workspace-config" and not (m.get("volume") or {}).get("subpath"):
            check(f"{name}: whole workspace-config volume mounted only by admin", name == "admin", m)
        if m.get("source") == "reviewer-reset":
            check(f"{name}: reviewer-reset mounted only by agent-reviewer and workspace-reviewer",
                  name in ("agent-reviewer", "workspace-reviewer"), m)
        if str(m.get("source", "")).endswith("docker.sock"):
            check(f"{name}: no Docker socket", False, m)
    for secret in secrets(service):
        check(f"{name}: may receive secrets/{secret}", name in SECRET_FILES[secret])

# GitHub must reach the reviewer's webhook, so its port is the one published on all addresses.
for name, service in sorted(services.items()):
    for port in service.get("ports") or []:
        if port.get("host_ip") != "127.0.0.1":
            check(f"{name}: a port on all addresses is only agent-reviewer's 8644",
                  name == "agent-reviewer" and port.get("target") == 8644 and str(port.get("published")) == "8644", port)
reviewer_ports = services.get("agent-reviewer", {}).get("ports") or []
check("agent-reviewer: publishes only 8644, on all addresses",
      [(p.get("host_ip") or "0.0.0.0", p.get("target"), str(p.get("published"))) for p in reviewer_ports]
      == [("0.0.0.0", 8644, "8644")], reviewer_ports)

admin, litellm, postgres = services.get("admin", {}), services["litellm"], services["postgres"]
github_token = services.get("github-token", {})
check("admin service exists", bool(admin))
check("admin: only secrets/master.env and secrets/onyx-admin.env", secrets(admin) == ["master.env", "onyx-admin.env"],
      secrets(admin))
check("admin: only on the admin and onyx networks", network_names(admin) == {"admin", "onyx"},
      sorted(network_names(admin)))
check("admin: not started by default (profile)", bool(admin.get("profiles")), admin.get("profiles"))
check("admin network is internal", networks.get("admin", {}).get("internal") is True, networks.get("admin"))
check("litellm: secrets/litellm.env and secrets/master.env",
      secrets(litellm) == ["litellm.env", "master.env"], secrets(litellm))
check("litellm: host port bound to 127.0.0.1 only",
      all(p.get("host_ip") == "127.0.0.1" for p in litellm.get("ports") or []), litellm.get("ports"))
check("postgres: only on the litellm-db network", network_names(postgres) == {"litellm-db"}, sorted(network_names(postgres)))
check("postgres: no published ports", not postgres.get("ports"), postgres.get("ports"))
check("github-token service exists", bool(github_token))
check("github-token: only secrets/github-app.env",
      secrets(github_token) == ["github-app.env"], secrets(github_token))
check("github-token: only on the agents and workspaces networks", network_names(github_token) == {"agents", "workspaces"},
      sorted(network_names(github_token)))
check("github-token: no published ports", not github_token.get("ports"), github_token.get("ports"))
github_mounts = mounts(github_token)
check("github-token: only mounts github-token/levels.json, read-only",
      len(github_mounts) == 1 and github_mounts[0].get("read_only") is True
      and str(github_mounts[0].get("source", "")).replace("\\", "/").endswith("/github-token/levels.json"),
      github_mounts)
check("workspaces network is internal", networks.get("workspaces", {}).get("internal") is True,
      networks.get("workspaces"))
check("workspaces network: only agents, workspaces, and github-token",
      all(name.startswith(("agent-", "workspace-")) or name == "github-token"
          for name, s in services.items() if "workspaces" in network_names(s)))
check("workspace-egress network: only workspaces",
      all(name.startswith("workspace-") for name, s in services.items() if "workspace-egress" in network_names(s)))
mcp_atlassian = services.get("mcp-atlassian", {})
check("mcp-atlassian service exists", bool(mcp_atlassian))
check("mcp-atlassian: only secrets/atlassian.env", secrets(mcp_atlassian) == ["atlassian.env"], secrets(mcp_atlassian))
extra = set((mcp_atlassian.get("environment") or {}).keys()) - MCP_ATLASSIAN_ENV_ALLOWED - {marker("atlassian.env")}
check("mcp-atlassian: no credentials or other extra environment variables", not extra, sorted(extra))
check("mcp-atlassian: only on the atlassian network", network_names(mcp_atlassian) == {"atlassian"},
      sorted(network_names(mcp_atlassian)))
check("mcp-atlassian: no published ports", not mcp_atlassian.get("ports"), mcp_atlassian.get("ports"))
check("mcp-atlassian: read-only root filesystem", mcp_atlassian.get("read_only") is True, mcp_atlassian.get("read_only"))
check("mcp-atlassian: no mounts", not mounts(mcp_atlassian), mounts(mcp_atlassian))
check("atlassian network: only litellm and mcp-atlassian",
      sorted(name for name, s in services.items() if "atlassian" in network_names(s)) == ["litellm", "mcp-atlassian"],
      sorted(name for name, s in services.items() if "atlassian" in network_names(s)))


def members(network):
    return sorted(name for name, s in services.items() if network in network_names(s))


# onyx-web, the UI, is checked separately: it publishes a port and never reaches the datastores.
onyx_services = sorted(name for name in services if name.startswith("onyx-") and name != "onyx-web")
check("onyx services exist", {"onyx-api", "onyx-background", "onyx-mcp", "onyx-db", "onyx-opensearch"} <= set(onyx_services),
      onyx_services)
for name in onyx_services:
    check(f"{name}: no published ports", not services[name].get("ports"), services[name].get("ports"))
onyx_web = services.get("onyx-web", {})
check("onyx-web service exists", bool(onyx_web))
check("onyx-web: no secrets", not secrets(onyx_web), secrets(onyx_web))
check("onyx-web: only on the onyx-ui network", network_names(onyx_web) == {"onyx-ui"}, sorted(network_names(onyx_web)))
check("onyx-web: host port bound to 127.0.0.1 only",
      all(p.get("host_ip") == "127.0.0.1" for p in onyx_web.get("ports") or []), onyx_web.get("ports"))
check("onyx-web: no mounts", not mounts(onyx_web), mounts(onyx_web))
for network in ("onyx", "onyx-backend"):
    check(f"{network} network is internal", networks.get(network, {}).get("internal") is True, networks.get(network))
check("onyx network: only litellm, admin, and the Onyx services that call models, serve MCP, or take configuration",
      members("onyx") == ["admin", "litellm", "onyx-api", "onyx-background", "onyx-mcp"], members("onyx"))
check("onyx-backend network: only Onyx services", members("onyx-backend") == onyx_services, members("onyx-backend"))
check("onyx-egress network: only onyx-api and onyx-background",
      members("onyx-egress") == ["onyx-api", "onyx-background"], members("onyx-egress"))
check("onyx-ui network: only onyx-web and onyx-api", members("onyx-ui") == ["onyx-api", "onyx-web"], members("onyx-ui"))
check("litellm-db network is internal", networks.get("litellm-db", {}).get("internal") is True, networks.get("litellm-db"))

sys.exit(1 if failures else 0)
