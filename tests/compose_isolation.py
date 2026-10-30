"""Static checks that compose.yaml keeps secrets and networks separated and every service hardened,
used by tests/run.sh.

Usage:
  python3 tests/compose_isolation.py write-secrets DIR/secrets
  docker compose --project-directory DIR -f compose.yaml config --format json | python3 tests/compose_isolation.py

Compose 2.x always merges env files into `environment` and drops `env_file` from `config` output,
so secret files can't be checked by path. Instead, `write-secrets` writes placeholder secret files
that each set one marker variable, and the checks look for those markers in each service's
environment. Run `config` against a project directory holding only those placeholders: a required
env file that isn't one of them makes `config` fail.

Checks every `agent-*` service (so newly added agents are covered), plus admin, litellm, postgres,
github-token, and sandbox.
"""

import json
import os
import sys

AGENT_ENV_ALLOWED = {"HERMES_UID", "HERMES_GID", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                     "GIT_COMMITTER_EMAIL"}
# Secret files and the only services allowed to receive them.
SECRET_FILES = {
    "litellm.env": {"litellm"},
    "master.env": {"litellm", "admin"},
    "postgres.env": {"postgres"},
    "github-app.env": {"github-token"},
}


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
    check(f"{name}: only on the agents network", network_names(service) == {"agents"}, sorted(network_names(service)))
    service_mounts = mounts(service)
    data = [m for m in service_mounts if m.get("source") == "agents-data"]
    souls = [m for m in service_mounts if m.get("target") == "/opt/data/SOUL.md"]
    configs = [m for m in service_mounts if m.get("target") == "/opt/data/config.yaml"]
    check(f"{name}: mounts only its own agents-data subdirectory, SOUL.md, and config.yaml",
          len(service_mounts) == 3 and len(data) == 1 and len(souls) == 1 and len(configs) == 1, service_mounts)
    check(f"{name}: agents-data subdirectory is {expected_subpath!r}",
          len(data) == 1 and data[0].get("type") == "volume" and data[0].get("target") == "/opt/data"
          and (data[0].get("volume") or {}).get("subpath") == expected_subpath,
          data)
    check(f"{name}: SOUL.md is hermes/souls/{expected_subpath}.md, read-only",
          len(souls) == 1 and souls[0].get("type") == "bind" and souls[0].get("read_only") is True
          and str(souls[0].get("source", "")).replace("\\", "/").endswith(f"/hermes/souls/{expected_subpath}.md"),
          souls)
    check(f"{name}: config.yaml is hermes/config.yaml, read-only",
          len(configs) == 1 and configs[0].get("type") == "bind" and configs[0].get("read_only") is True
          and str(configs[0].get("source", "")).replace("\\", "/").endswith("/hermes/config.yaml"),
          configs)
    for m in data:
        subpath = (m.get("volume") or {}).get("subpath")
        check(f"{name}: agents-data subdirectory not shared", subpath not in subpaths,
              f"{subpath!r} also used by {subpaths.get(subpath)}")
        subpaths[subpath] = name

for name, service in services.items():
    check(f"{name}: no-new-privileges", "no-new-privileges:true" in (service.get("security_opt") or []),
          service.get("security_opt"))
    check(f"{name}: drops all capabilities", "ALL" in (service.get("cap_drop") or []), service.get("cap_drop"))
    check(f"{name}: not privileged", not service.get("privileged"))
    for m in mounts(service):
        if m.get("source") == "agents-data" and not (m.get("volume") or {}).get("subpath"):
            check(f"{name}: whole agents-data volume mounted only by admin", name == "admin", m)
        if str(m.get("source", "")).endswith("docker.sock"):
            check(f"{name}: no Docker socket", False, m)
    for secret in secrets(service):
        check(f"{name}: may receive secrets/{secret}", name in SECRET_FILES[secret])

admin, litellm, postgres = services.get("admin", {}), services["litellm"], services["postgres"]
github_token = services.get("github-token", {})
check("admin service exists", bool(admin))
check("admin: only secrets/master.env", secrets(admin) == ["master.env"], secrets(admin))
check("admin: only on the admin network", network_names(admin) == {"admin"}, sorted(network_names(admin)))
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
check("github-token: only on the agents network", network_names(github_token) == {"agents"},
      sorted(network_names(github_token)))
check("github-token: no published ports", not github_token.get("ports"), github_token.get("ports"))
github_mounts = mounts(github_token)
check("github-token: only mounts github-token/levels.json, read-only",
      len(github_mounts) == 1 and github_mounts[0].get("read_only") is True
      and str(github_mounts[0].get("source", "")).replace("\\", "/").endswith("/github-token/levels.json"),
      github_mounts)
sandbox = services.get("sandbox", {})
check("sandbox service exists", bool(sandbox))
check("sandbox: no environment or env files", not sandbox.get("environment"), sandbox.get("environment"))
check("sandbox: only on the sandbox network", network_names(sandbox) == {"sandbox"}, sorted(network_names(sandbox)))
check("sandbox: no published ports", not sandbox.get("ports"), sandbox.get("ports"))
check("sandbox: read-only root filesystem", sandbox.get("read_only") is True, sandbox.get("read_only"))
check("sandbox: only its own work and cache volumes",
      sorted(m.get("source") for m in mounts(sandbox)) == ["sandbox-cache", "sandbox-work"], mounts(sandbox))
check("sandbox network: only litellm and sandbox",
      sorted(name for name, s in services.items() if "sandbox" in network_names(s)) == ["litellm", "sandbox"])
check("litellm-db network is internal", networks.get("litellm-db", {}).get("internal") is True, networks.get("litellm-db"))

sys.exit(1 if failures else 0)
