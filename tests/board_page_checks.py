"""Checks a board page as a browser would, used by tests/run.sh.

Usage: python board_page_checks.py <page URL> <task title>

Runs in the fake-oidc container (tests/fake_oidc.py, standing in for Keycloak), which trusts its own
certificate authority and has aiohttp. The page URL is kanban-web's, the page's only way in. Logs in
through the OIDC flow, uses the Kanban API and its live updates and the board chat, tries the rest of
the dashboard, and logs out. Prints one `name: value` line per check; `task:` is the id of the task it
creates, assigned to `default`.
"""

import asyncio
import http.client
import http.cookiejar
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

import aiohttp

base, title = sys.argv[1].rstrip("/"), sys.argv[2]
tls = ssl.create_default_context(cafile="/tls/ca.crt")

# Dashboard requests a logged-in user must not get through kanban-web: secrets, settings, sessions,
# logs, its terminal chat, and the ways the review found to run code (files plus a cron script, a new
# profile with its own config).
REFUSED = [
    ("GET", "/api/env", None), ("GET", "/api/config", None), ("GET", "/api/sessions", None),
    ("GET", "/api/logs", None), ("GET", "/api/pty", None), ("GET", "/chat", None),
    ("PUT", "/api/config/raw?profile=default", {"yaml": "model: {}"}),
    ("POST", "/api/profiles", {"name": "attacker"}),
    ("POST", "/api/files/mkdir", {"path": "scripts"}),
    ("POST", "/api/fs/write-text", {"path": "scripts/x.py", "content": "print(1)"}),
    ("POST", "/api/cron/jobs", {"name": "x", "schedule": "every 1m", "script": "x.py", "no_agent": True}),
    ("POST", "/api/ops/hooks", {"event": "on_session_start", "command": "id"}),
    ("POST", "/api/skills/hub/install", {"identifier": "x"}),
    ("POST", "/api/gateway/restart", {}),
    # Kanban routes that act on this container's processes or on server paths: a dispatcher tick would
    # reclaim tasks the agent is still running.
    ("POST", "/api/plugins/kanban/dispatch", None),
    ("POST", "/api/plugins/kanban/tasks/TASK/reclaim", {}),
    ("POST", "/api/plugins/kanban/runs/1/terminate", {}),
    ("POST", "/api/plugins/kanban/boards/import", {"path": "/etc/passwd"}),
    ("POST", "/api/plugins/kanban/boards/default/export", {"path": "/tmp/x"}),
    ("POST", "/api/plugins/kanban/boards/default/switch", {}),
]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def browser():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(jar),
                                         urllib.request.HTTPSHandler(context=tls))
    return jar, opener


def request(opener, method, url, body=None, headers=None, raw=None):
    """(status, Location header, body text); redirects are returned, not followed."""
    if not url.startswith("http"):
        url = base + url
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {"Content-Type": "application/json"})
    try:
        response = opener.open(req, timeout=120)
    except urllib.error.HTTPError as e:
        response = e
    return response.status, response.headers.get("Location", ""), response.read().decode(errors="replace")


def login(opener, extra=""):
    """Runs the OIDC flow; `extra` is added to the provider's authorize URL (e.g. &deny=1). Returns
    the callback's status and Location."""
    status, location, _ = request(opener, "GET", "/auth/login?provider=self-hosted")
    if status != 302 or not location.startswith("https://fake-oidc:8443/authorize"):
        return f"login start {status} {location}"
    status, callback, _ = request(opener, "GET", location + extra)
    if status != 302 or not callback.startswith(base + "/auth/callback"):
        return f"authorize {status} {callback}"
    status, location, _ = request(opener, "GET", callback)
    return f"{status} {location}"


def chat(opener, text):
    """The board chat's answer to `text`, or `<status> <error>`."""
    status, _, body = request(opener, "POST", "/board-chat/api", {"messages": [{"role": "user", "content": text}]})
    if status != 200:
        try:
            return f"{status} {json.loads(body).get('error')}"
        except ValueError:
            return f"{status} {body[:100]}"
    answer = ""
    for line in body.splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            try:
                answer += json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
            except (ValueError, KeyError, IndexError):
                pass
    return f"200 {answer}"


async def websocket(jar, opener, path):
    """Connects to a WebSocket through kanban-web with a fresh ticket; `connected` or why not."""
    status, _, body = request(opener, "POST", "/api/auth/ws-ticket")
    if status != 200:
        return f"ticket {status}"
    ticket = json.loads(body)["ticket"]
    cookies = "; ".join(f"{c.name}={c.value}" for c in jar)
    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(f"{base.replace('http', 'ws', 1)}{path}?ticket={ticket}",
                                          headers={"Cookie": cookies, "Origin": base}, timeout=30):
                return "connected"
        except aiohttp.WSServerHandshakeError as e:
            return f"refused {e.status}"


_, anonymous = browser()
for path in ("/api/plugins/kanban/board", "/api/env", "/api/config", "/api/sessions", "/api/cron/jobs"):
    print(f"anonymous {path}: {request(anonymous, 'GET', path)[0]}")
print(f"anonymous create: {request(anonymous, 'POST', '/api/plugins/kanban/tasks', {'title': 'anonymous task'})[0]}")
print(f"anonymous page: {request(anonymous, 'GET', '/')[0]}")
print(f"anonymous chat page: {request(anonymous, 'GET', '/board-chat')[0]}")
print(f"anonymous chat: {chat(anonymous, 'hello')}")

# The provider refuses the user, as Keycloak does when its flow denies access.
jar, denied = browser()
login(denied, "&deny=1")
# The login's PKCE cookie may stay; no session cookie may be set.
print(f"denied session: {' '.join(sorted(c.name for c in jar if not c.name.endswith('hermes_session_pkce'))) or 'none'}")
print(f"denied board: {request(denied, 'GET', '/api/plugins/kanban/board')[0]}")

jar, user = browser()
print(f"login: {login(user)}")
status, _, body = request(user, "GET", "/api/auth/me")
print(f"me: {status} {json.loads(body).get('email') if status == 200 else body[:200]}")
print(f"board: {request(user, 'GET', '/api/plugins/kanban/board')[0]}")
status, _, body = request(user, "POST", "/api/plugins/kanban/tasks",
                          {"title": title, "assignee": "default", "body": "Created by tests/board_page_checks.py."})
task = json.loads(body).get("task", {}).get("id") if status == 200 else None
print(f"create: {status} {'' if task else body[:200]}")
print(f"task: {task}")
status, _, body = request(user, "POST", f"/api/plugins/kanban/tasks/{task}/comments",
                          {"body": "Comment from the board page."})
print(f"comment: {status}")
# An attachment uploaded here must be readable by the agent at the path Kanban stores.
boundary = "board-page-checks"
upload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"spec.txt\"\r\n"
          f"Content-Type: text/plain\r\n\r\nspec from the board page\r\n--{boundary}--\r\n").encode()
status, _, body = request(user, "POST", f"/api/plugins/kanban/tasks/{task}/attachments", raw=upload,
                          headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
print(f"attachment: {status} {(json.loads(body).get('attachment') or {}).get('stored_path') if status == 200 else body[:200]}")
# A forged client address must not reach the dashboard's audit log, which records logins' addresses.
request(user, "GET", "/auth/login?provider=self-hosted", headers={"X-Forwarded-For": "203.0.113.99",
                                                                  "X-Real-IP": "203.0.113.99"})
print(f"live updates: {asyncio.run(websocket(jar, user, '/api/plugins/kanban/events'))}")
print(f"terminal websocket: {asyncio.run(websocket(jar, user, '/api/pty'))}")
print(f"refused: {' '.join(str(request(user, method, path.replace('TASK', str(task)), body)[0]) for method, path, body in REFUSED)}")
tricks = ("/api/plugins/kanban/../../env", "/api/plugins/kanban/%2e%2e/%2e%2e/env", "/api/plugins/kanban/..%2f..%2fenv",
          "//api/env")
print(f"refused paths: {' '.join(str(request(user, 'GET', path)[0]) for path in tricks)}")
print(f"chat: {chat(user, 'hello')}")


def chat_in_pieces(jar, size):
    """Sends a `size`-byte conversation in two pieces, as a slow connection would; the chat's status."""
    body = json.dumps({"messages": [{"role": "assistant", "content": "x" * size}, {"role": "user", "content": "hello"}]})
    url = urllib.parse.urlsplit(base)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=120)
    connection.putrequest("POST", "/board-chat/api")
    for name, value in (("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                        ("Cookie", "; ".join(f"{c.name}={c.value}" for c in jar))):
        connection.putheader(name, value)
    connection.endheaders()
    connection.send(body[: len(body) // 2].encode())
    import time
    time.sleep(0.5)
    connection.send(body[len(body) // 2:].encode())
    status = connection.getresponse().status
    connection.close()
    return status


print(f"chat in pieces: {chat_in_pieces(jar, 100_000)}")
print(f"chat slash command: {chat(user, '/help')}")
print(f"chat shell command: {chat(user, '  !id')}")
status, _, _ = request(user, "POST", "/auth/logout")
print(f"logout: {status}")
print(f"after logout: {request(user, 'GET', '/api/plugins/kanban/board')[0]}")
print(f"after logout chat: {chat(user, 'hello').split(' ')[0]}")
