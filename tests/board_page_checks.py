"""Checks a board page as a browser would, used by tests/run.sh.

Usage: python board_page_checks.py <page URL> <task title>

Runs in the fake-oidc container (tests/fake_oidc.py, standing in for Keycloak), which trusts its own
certificate authority. Logs in through the OIDC flow, uses the Kanban API, and logs out. Prints one
`name: value` line per check; `task:` is the id of the task it creates, assigned to `default`.
"""

import http.cookiejar
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

base, title = sys.argv[1].rstrip("/"), sys.argv[2]
tls = ssl.create_default_context(cafile="/tls/ca.crt")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def browser():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(jar),
                                         urllib.request.HTTPSHandler(context=tls))
    return jar, opener


def request(opener, method, url, body=None):
    """(status, Location header, body text); redirects are returned, not followed."""
    if not url.startswith("http"):
        url = base + url
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        response = opener.open(req, timeout=60)
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


_, anonymous = browser()
for path in ("/api/plugins/kanban/board", "/api/env", "/api/config", "/api/sessions", "/api/cron/jobs"):
    print(f"anonymous {path}: {request(anonymous, 'GET', path)[0]}")
print(f"anonymous create: {request(anonymous, 'POST', '/api/plugins/kanban/tasks', {'title': 'anonymous task'})[0]}")
print(f"anonymous page: {request(anonymous, 'GET', '/')[0]}")

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
# The page must never run tasks itself, even when asked to dispatch.
status, _, body = request(user, "POST", "/api/plugins/kanban/dispatch")
result = json.loads(body) if status == 200 else {}
print(f"dispatch: {status} spawned={len(result.get('spawned') or [])} "
      f"skipped={task in (result.get('skipped_nonspawnable') or [])}")
status, _, _ = request(user, "POST", "/auth/logout")
print(f"logout: {status}")
print(f"after logout: {request(user, 'GET', '/api/plugins/kanban/board')[0]}")
