"""github-token: hands agents short-lived GitHub App installation tokens for the access levels they
have been granted.

Access levels are defined in levels.json (mounted at LEVELS_FILE). Each level names the token's
permissions and, optionally, the repositories it is limited to. GitHub caps a token at what its
App was granted, so each level normally has its own App (e.g. a read-only App installed org-wide,
and a write App installed only on the repositories agents may change). A level's App credentials
come from environment variables prefixed with the level's name in upper case:

  <LEVEL>_APP_ID                  the App's ID
  <LEVEL>_INSTALLATION_ID         the installation's ID (e.g. on the openmrs org)
  <LEVEL>_PRIVATE_KEY_BASE64      the App's private key (PEM), base64-encoded onto one line

Agents authenticate with their LiteLLM virtual key. The key is checked against LiteLLM's
/key/info, and a token is issued only for a level listed in the key's `github_levels` metadata,
which the admin tool manages. Keys LiteLLM rejects (revoked, expired, blocked) get nothing.

  GET /token/<level>   Authorization: Bearer <LiteLLM key>; returns the token as plain text.
                       401 bad key, 403 level not granted, 404 unknown level, 503 level has no
                       App credentials, 502 GitHub or LiteLLM failed.
  GET /healthz         each level and whether it has App credentials, without calling GitHub.

GitHub expires tokens after an hour; each level's token is reused until five minutes before
that. Key checks are cached for a minute, so revoking a grant takes effect within a minute.
JWTs are signed with the openssl CLI, keeping this service free of Python dependencies.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LEVELS_FILE = os.environ.get("LEVELS_FILE", "/app/levels.json")
GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
REFRESH_BEFORE_EXPIRY = 300
GRANT_CACHE_SECONDS = 60
LEVEL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class TokenError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Level:
    name: str
    permissions: dict[str, str]
    repositories: list[str] | None
    app_id: str
    installation_id: str
    private_key: bytes

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.installation_id and self.private_key)


def load_levels() -> dict[str, Level]:
    with open(LEVELS_FILE) as f:
        definitions = json.load(f)
    levels = {}
    for name, spec in definitions.items():
        if not LEVEL_NAME.match(name):
            raise SystemExit(f"{LEVELS_FILE}: invalid level name {name!r}")
        permissions = spec.get("permissions") or {}
        if not permissions or any(v not in ("read", "write") for v in permissions.values()):
            raise SystemExit(f"{LEVELS_FILE}: {name} needs permissions, each 'read' or 'write'")
        prefix = name.upper()
        levels[name] = Level(
            name=name,
            permissions=permissions,
            repositories=spec.get("repositories"),
            app_id=os.environ.get(f"{prefix}_APP_ID", "").strip(),
            installation_id=os.environ.get(f"{prefix}_INSTALLATION_ID", "").strip(),
            private_key=base64.b64decode(os.environ.get(f"{prefix}_PRIVATE_KEY_BASE64", "").strip() or b""),
        )
    return levels


LEVELS = load_levels()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(private_key: bytes, message: bytes) -> bytes:
    # The key reaches openssl through a pipe, so it's never written to disk.
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, private_key)
        os.close(write_fd)
        write_fd = None
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", f"/dev/fd/{read_fd}"],
            input=message, capture_output=True, check=False, pass_fds=(read_fd,),
        )
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
    if result.returncode != 0:
        raise TokenError(502, f"signing the App JWT failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def _app_jwt(level: Level) -> str:
    now = int(time.time())
    # iat is backdated to allow for clock drift; GitHub rejects expiries over 10 minutes.
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": level.app_id}).encode())
    signing_input = f"{header}.{payload}".encode()
    return f"{header}.{payload}.{_b64url(_sign(level.private_key, signing_input))}"


def _mint(level: Level) -> tuple[str, float]:
    body: dict = {"permissions": level.permissions}
    if level.repositories is not None:
        body["repositories"] = level.repositories
    request = urllib.request.Request(
        f"{GITHUB_API_URL}/app/installations/{level.installation_id}/access_tokens",
        method="POST",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {_app_jwt(level)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise TokenError(502, f"GitHub refused the {level.name} token request: HTTP {e.code} {detail}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise TokenError(502, f"could not reach GitHub: {e}")
    expires = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00")).timestamp()
    return result["token"], expires


def _granted_levels(litellm_key: str) -> list[str]:
    """The levels in the key's github_levels metadata, as reported by LiteLLM for the key itself."""
    request = urllib.request.Request(f"{LITELLM_URL}/key/info", headers={"Authorization": f"Bearer {litellm_key}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            info = json.load(response).get("info") or {}
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403, 404):
            raise TokenError(401, "LiteLLM did not accept this key")
        raise TokenError(502, f"LiteLLM key check failed: HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise TokenError(502, f"could not reach LiteLLM: {e}")
    if info.get("status", "active") != "active":
        raise TokenError(401, f"key is {info.get('status')}")
    return list((info.get("metadata") or {}).get("github_levels") or [])


_lock = threading.Lock()
_tokens: dict[str, tuple[str, float]] = {}
_grants: dict[str, tuple[list[str], float]] = {}


def _cached_grants(litellm_key: str) -> list[str]:
    digest = hashlib.sha256(litellm_key.encode()).hexdigest()
    with _lock:
        cached = _grants.get(digest)
        if cached and cached[1] > time.time():
            return cached[0]
    levels = _granted_levels(litellm_key)
    with _lock:
        _grants[digest] = (levels, time.time() + GRANT_CACHE_SECONDS)
    return levels


def token_for(level_name: str, litellm_key: str) -> str:
    level = LEVELS.get(level_name)
    if level is None:
        raise TokenError(404, f"unknown level {level_name!r}; levels: {', '.join(sorted(LEVELS)) or 'none'}")
    if not litellm_key:
        raise TokenError(401, "missing Authorization: Bearer <LiteLLM key>")
    if level_name not in _cached_grants(litellm_key):
        raise TokenError(403, f"this agent has not been granted {level_name}")
    if not level.configured:
        raise TokenError(503, f"{level_name} has no GitHub App credentials (see secrets/github-app.env)")
    with _lock:
        cached = _tokens.get(level_name)
        if cached is None or cached[1] - time.time() < REFRESH_BEFORE_EXPIRY:
            cached = _tokens[level_name] = _mint(level)
            print(f"minted {level_name} token, expires {datetime.fromtimestamp(cached[1]).isoformat()}", flush=True)
        return cached[0]


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, text: str) -> None:
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            self._reply(200, "".join(
                f"{name}: {'configured' if level.configured else 'not configured'}\n" for name, level in LEVELS.items()
            ))
            return
        if not self.path.startswith("/token/"):
            self._reply(404, "not found\n")
            return
        level_name = self.path[len("/token/"):]
        auth = self.headers.get("Authorization", "")
        try:
            self._reply(200, token_for(level_name, auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""))
        except TokenError as e:
            print(f"{level_name} token refused ({e.status}): {e}", flush=True)
            self._reply(e.status, f"{e}\n")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    for level in LEVELS.values():
        print(f"github-token: {level.name} {'configured' if level.configured else 'not configured'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
