"""Minimal client for the LiteLLM management API, authenticated with the master key."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class LiteLLMError(Exception):
    """A LiteLLM API call failed; the message includes LiteLLM's own error text.

    `status` is the HTTP status (None if LiteLLM couldn't be reached) and `detail` LiteLLM's parsed
    error body, when it sent JSON.
    """

    def __init__(self, message: str, status: int | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class LiteLLM:
    def __init__(self, base_url: str, master_key: str) -> None:
        if not master_key:
            raise LiteLLMError("LITELLM_MASTER_KEY is not set (expected from secrets/master.env)")
        self._base_url = base_url.rstrip("/")
        self._master_key = master_key

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._master_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                detail = json.loads(body)
            except ValueError:
                detail = None
            raise LiteLLMError(f"{method} {path} failed with HTTP {e.code}: {body[:500]}", e.code, detail) from None
        except urllib.error.URLError as e:
            raise LiteLLMError(f"cannot reach LiteLLM at {self._base_url}: {e.reason}") from None

    def mcp_server_ids(self) -> dict[str, str]:
        """MCP server names and their LiteLLM server ids."""
        return {server["server_name"]: server["server_id"] for server in self._call("GET", "/v1/mcp/server")}

    def mcp_server_fault(self, server_id: str) -> str | None:
        """Lists the server's tools through LiteLLM and returns None if that works, or LiteLLM's fault
        tag if not: "internal" when LiteLLM itself can't use the server (e.g. its credential is
        empty), "unreachable" or "timeout" when the upstream server is down. Raises LiteLLMError if
        the failure says nothing about the server (e.g. LiteLLM is down)."""
        try:
            result = self._call("GET", f"/mcp-rest/tools/list?server_id={server_id}")
        except LiteLLMError as e:
            fault = ((e.detail or {}).get("detail") or {}).get("error") if isinstance(e.detail, dict) else None
            if e.status is None or not isinstance(fault, str):
                raise
            return fault
        return (result or {}).get("error")

    def keys(self) -> list[dict[str, Any]]:
        """Every virtual key, as full key objects."""
        keys: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self._call("GET", f"/key/list?return_full_object=true&size=100&page={page}")
            keys.extend(result.get("keys") or [])
            if page >= (result.get("total_pages") or 1):
                return keys
            page += 1

    def key_info(self, key_hash: str) -> dict[str, Any]:
        return self._call("GET", f"/key/info?key={key_hash}").get("info") or {}

    def generate_key(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/key/generate", body)

    def update_key(self, key_hash: str, changes: dict[str, Any]) -> None:
        self._call("POST", "/key/update", {"key": key_hash, **changes})

    def user_info(self, user_id: str) -> dict[str, Any] | None:
        """The user's record, or None if LiteLLM has no such user."""
        try:
            return self._call("GET", f"/user/info?user_id={user_id}").get("user_info") or None
        except LiteLLMError as e:
            if e.status == 404:
                return None
            raise

    def new_user(self, body: dict[str, Any]) -> None:
        # LiteLLM issues the new user a key unless told not to.
        self._call("POST", "/user/new", {**body, "auto_create_key": False})

    def update_user(self, user_id: str, changes: dict[str, Any]) -> None:
        self._call("POST", "/user/update", {"user_id": user_id, **changes})

    def delete_user(self, user_id: str) -> None:
        self._call("POST", "/user/delete", {"user_ids": [user_id]})

    def delete_keys(self, key_hashes: list[str]) -> None:
        if key_hashes:
            self._call("POST", "/key/delete", {"keys": key_hashes})
