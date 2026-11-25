"""The front door of a board page (kanban-web-<name> in compose.yaml).

It shares the board-<name> container's network namespace, whose Hermes dashboard (9119) and API server
(8642) listen on 127.0.0.1 only, so every browser request goes through here. It passes on only what
the Kanban tab needs: logging in and out, the single-page app's files and start-up reads, and the
Kanban API and its live updates. Everything else the dashboard offers (its terminal chat, cron jobs,
files, profiles, settings, keys, sessions, logs) is refused, since Hermes' dashboard has no roles and
some of it runs commands.

It also serves a chat page (/board-chat) whose messages go to the API server's chat completions,
with the API server's key added here, only for users whose dashboard session is valid. The API
server runs a plain agent turn with the board's configured tools (hermes/board.yaml): no shell, and
no slash commands, which are refused here.

Fails closed: while the dashboard doesn't require a login (e.g. its public URL is a loopback one),
every request is refused.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web

DASHBOARD = os.environ.get("BOARD_DASHBOARD", "http://127.0.0.1:9119")
API = os.environ.get("BOARD_API", "http://127.0.0.1:8642")
KEY_FILE = Path(os.environ.get("BOARD_API_KEY_FILE", "/run/board-web/api-server-key"))
CHAT_PAGE = Path(__file__).with_name("chat.html")
PORT = int(os.environ.get("BOARD_WEB_PORT", "8080"))

# GET (and HEAD) only: the app's pages and files, the login flow, and the reads the app makes on start.
# Not /api/config: it holds the page's key, expanded in its MCP servers' headers.
GET_PATHS = {
    "/", "/kanban", "/login", "/favicon.ico", "/auth/login", "/auth/callback",
    "/api/status", "/api/auth/me", "/api/auth/providers", "/api/dashboard/font", "/api/dashboard/plugins",
    "/api/dashboard/themes", "/api/profiles", "/api/profiles/active", "/api/shared-metrics/consent",
}
GET_PREFIXES = ("/assets/", "/dashboard-plugins/", "/ds-assets/", "/fonts/", "/fonts-terminal/")
POST_PATHS = {"/auth/logout", "/api/auth/ws-ticket"}
# Any method: the Kanban tab's API, except what acts on this container's processes or on server paths:
# a dispatcher tick (which reclaims tasks the agent is still running, since the agent's workers aren't
# processes here), reclaiming or terminating a run, exporting or importing a board (server file
# paths), and switching the shared current board. Its only WebSocket is its live updates.
KANBAN_PREFIX = "/api/plugins/kanban/"
KANBAN_EVENTS = "/api/plugins/kanban/events"
KANBAN_REFUSED = re.compile(r"^/api/plugins/kanban/(dispatch|tasks/[^/]+/reclaim|runs/[^/]+/terminate|"
                            r"boards/import|boards/[^/]+/(export|switch))/?$")

# Not forwarded either way (RFC 9110 hop-by-hop headers, and lengths aiohttp recomputes).
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "content-length"}
# Forwarding headers from the browser are dropped (the dashboard trusts them from here, so a user could
# forge the client address it logs, or the prefix it redirects to); X-Forwarded-For is set to the peer.
# X-Forwarded-Proto is kept: the host's TLS proxy sets it, and it only decides whether cookies are Secure.
CLIENT_FORWARDING = {"x-forwarded-for", "x-forwarded-host", "x-forwarded-prefix", "x-forwarded-port",
                     "x-forwarded-server", "x-real-ip", "forwarded"}
# Raw paths with dot segments, encoded slashes or dots, or backslashes could be read differently here
# and by the dashboard, so they're refused outright.
SUSPICIOUS_PATH = re.compile(r"(^|/)\.\.?(/|$)|%2e|%2f|%5c|\\|//", re.IGNORECASE)

MAX_CHAT_BYTES = 256 * 1024
MAX_CHAT_MESSAGES = 100
AUTH_CHECK_SECONDS = 30


def allowed(method: str, path: str, websocket: bool) -> bool:
    if websocket:
        return method == "GET" and path == KANBAN_EVENTS
    if path.startswith(KANBAN_PREFIX):
        return not KANBAN_REFUSED.match(path)
    if method in ("GET", "HEAD"):
        return path in GET_PATHS or path.startswith(GET_PREFIXES)
    return method == "POST" and path in POST_PATHS


class BoardWeb:
    def __init__(self) -> None:
        self.session: ClientSession | None = None
        self._gate_checked = 0.0
        self._gate_on = False

    async def start(self, app: web.Application) -> None:
        self.session = ClientSession(auto_decompress=False, cookie_jar=DummyCookieJar(),
                                     timeout=ClientTimeout(total=None, connect=10))

    async def stop(self, app: web.Application) -> None:
        if self.session:
            await self.session.close()

    async def gate_on(self) -> bool:
        """Whether the dashboard requires a login, checked at most every AUTH_CHECK_SECONDS."""
        if time.monotonic() - self._gate_checked < AUTH_CHECK_SECONDS:
            return self._gate_on
        try:
            async with self.session.get(DASHBOARD + "/api/status") as response:
                status = json.loads(await response.read()) if response.status == 200 else {}
            self._gate_on = status.get("auth_required") is True
        except Exception:  # noqa: BLE001 -- unreachable or malformed: closed until the next check
            self._gate_on = False
        self._gate_checked = time.monotonic()
        return self._gate_on

    @staticmethod
    def upstream_headers(request: web.Request) -> dict[str, str]:
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() not in CLIENT_FORWARDING
                   and not k.lower().startswith("sec-websocket")}
        # The host's TLS proxy says whether the browser used HTTPS; the dashboard trusts loopback.
        headers.setdefault("X-Forwarded-Proto", "http")
        if request.remote:
            headers["X-Forwarded-For"] = request.remote
        return headers

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if not await self.gate_on():
            return web.Response(status=503, text="The board page isn't configured to require a login.\n")
        raw = request.raw_path.split("?", 1)[0]
        if SUSPICIOUS_PATH.search(raw):
            return web.Response(status=400, text="Bad path.\n")
        path = request.path
        if path in ("/board-chat", "/board-chat/"):
            if request.method not in ("GET", "HEAD"):
                return web.Response(status=405)
            return web.FileResponse(CHAT_PAGE, headers={"Cache-Control": "no-store"})
        if path == "/board-chat/api":
            return await self.chat(request)
        websocket = request.headers.get("Upgrade", "").lower() == "websocket"
        if not allowed(request.method, path, websocket):
            return web.Response(status=404, text="Not available on the board page.\n")
        try:
            if websocket:
                return await self.relay_websocket(request)
            return await self.forward(request)
        except (OSError, asyncio.TimeoutError, ClientError):
            return web.Response(status=502, text="The board page isn't answering.\n")

    async def forward(self, request: web.Request) -> web.StreamResponse:
        async with self.session.request(request.method, DASHBOARD + request.raw_path,
                                        headers=self.upstream_headers(request),
                                        data=request.content if request.can_read_body else None,
                                        allow_redirects=False) as upstream:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for name, value in upstream.headers.items():
                if name.lower() not in HOP_BY_HOP:
                    response.headers.add(name, value)
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response

    async def relay_websocket(self, request: web.Request) -> web.StreamResponse:
        headers = {k: v for k, v in self.upstream_headers(request).items()
                   if k.lower() in ("cookie", "origin", "host", "user-agent", "x-forwarded-proto")}
        try:
            upstream = await self.session.ws_connect(DASHBOARD + request.raw_path, headers=headers)
        except Exception:  # noqa: BLE001 -- the dashboard refused (e.g. a bad ticket)
            return web.Response(status=403, text="WebSocket refused.\n")
        client = web.WebSocketResponse()
        await client.prepare(request)

        async def pump(source, sink):
            async for message in source:
                if message.type == WSMsgType.TEXT:
                    await sink.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await sink.send_bytes(message.data)
                else:
                    break

        tasks = [asyncio.create_task(pump(client, upstream)), asyncio.create_task(pump(upstream, client))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        await upstream.close()
        await client.close()
        return client

    async def logged_in(self, request: web.Request) -> bool:
        headers = {k: v for k, v in self.upstream_headers(request).items()
                   if k.lower() in ("cookie", "host", "x-forwarded-proto")}
        async with self.session.get(DASHBOARD + "/api/auth/me", headers=headers, allow_redirects=False) as response:
            return response.status == 200

    async def chat(self, request: web.Request) -> web.StreamResponse:
        if request.method != "POST":
            return web.Response(status=405)
        try:
            if not await self.logged_in(request):
                return web.json_response({"error": "Log in to the board page first."}, status=401)
        except (OSError, asyncio.TimeoutError, ClientError):
            return web.json_response({"error": "The board page isn't answering."}, status=502)
        body = await self.read_body(request)
        try:
            messages = self.chat_messages(body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        try:
            key = KEY_FILE.read_text().strip()
        except OSError:
            key = ""
        if not key:
            return web.json_response({"error": "The board chat has no key; see `bin/admin agent board`."}, status=502)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
        try:
            async with self.session.post(API + "/v1/chat/completions",
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                         json={"model": "hermes-agent", "messages": messages, "stream": True}) as upstream:
                if upstream.status != 200:
                    return web.json_response({"error": f"The chat failed (HTTP {upstream.status})."}, status=502)
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
        except (OSError, asyncio.TimeoutError, ClientError):
            if not response.prepared:
                return web.json_response({"error": "The board chat isn't answering."}, status=502)
        await response.write_eof()
        return response

    @staticmethod
    async def read_body(request: web.Request) -> bytes:
        """The whole request body, or its first MAX_CHAT_BYTES + 1 bytes (enough to refuse it)."""
        chunks, size = [], 0
        while size <= MAX_CHAT_BYTES:
            chunk = await request.content.read(MAX_CHAT_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)

    @staticmethod
    def chat_messages(body: bytes) -> list[dict[str, str]]:
        """The conversation to send: only user and assistant text. Raises ValueError for anything
        else, and for a user message that would be read as a command (`/...`, `!...`)."""
        if len(body) > MAX_CHAT_BYTES:
            raise ValueError("The conversation is too long; start a new one.")
        try:
            messages = json.loads(body).get("messages")
        except (ValueError, AttributeError):
            raise ValueError("Expected a JSON object with `messages`.") from None
        if not isinstance(messages, list) or not messages or len(messages) > MAX_CHAT_MESSAGES:
            raise ValueError(f"Expected 1 to {MAX_CHAT_MESSAGES} messages.")
        cleaned = []
        for message in messages:
            role = message.get("role") if isinstance(message, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if role not in ("user", "assistant") or not isinstance(content, str):
                raise ValueError("Each message needs a role (user or assistant) and text content.")
            if role == "user" and content.lstrip().startswith(("/", "!")):
                raise ValueError("Commands (messages starting with / or !) aren't available here.")
            cleaned.append({"role": role, "content": content})
        if cleaned[-1]["role"] != "user":
            raise ValueError("The last message must be the user's.")
        return cleaned


def main() -> None:
    board = BoardWeb()
    app = web.Application(client_max_size=26 * 1024 * 1024)  # Kanban attachments are up to 25 MB.
    app.on_startup.append(board.start)
    app.on_cleanup.append(board.stop)
    app.router.add_route("*", "/{tail:.*}", board.handle)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
