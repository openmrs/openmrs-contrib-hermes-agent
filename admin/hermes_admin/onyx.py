"""Onyx configuration through its REST API: the administrator login, Onyx's LiteLLM key and models
(one for search's LLM calls, one for embeddings), the search token for LiteLLM's onyx_search MCP
server, and the indexed sources.

Onyx keeps everything this configures, sources and their tokens included, in its own database (the
onyx-db-data volume), so nothing here writes files. Onyx's free edition stores credentials there
unencrypted, so backups of that volume hold sources' tokens. Onyx's LiteLLM key is the only key
with `hermes_role: onyx`; its monthly budget is on the LiteLLM user `hermes-onyx`.

Sources are added one at a time with `add`, from the fields SOURCE_TYPES lists for each kind of
source. Onyx indexes only what the source's account can see and doesn't copy its permissions:
everything indexed is searchable by every caller, so use accounts that see only public content.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .agents import BUDGET_DURATION, MANAGED_BY, NO_MCP_SERVERS, AgentError
from .litellm import LiteLLM

ONYX_ROLE = "onyx"
ONYX_USER_ID = "hermes-onyx"
# The model_list entries in litellm/config.yaml reserved for Onyx.
ONYX_MODEL = "onyx"
ONYX_EMBEDDING_MODEL = "onyx-embedding"
ONYX_ROUTES = ["/v1/chat/completions", "/chat/completions", "/v1/embeddings", "/embeddings", "/v1/models",
               "/models"]
DEFAULT_ONYX_BUDGET = 100.0
# Onyx's name for its LLM provider entry pointing at LiteLLM.
PROVIDER_NAME = "litellm"
SEARCH_TOKEN_NAME = "litellm-onyx-search"
DEFAULT_REFRESH_HOURS = 24
# The empty credential Onyx creates for sources that need none; never deleted.
PUBLIC_CREDENTIAL_ID = 0


class OnyxError(AgentError):
    """An Onyx operation was refused or failed; the message is meant for the operator."""


@dataclass(frozen=True)
class SourceField:
    """One setting asked for when adding a source. `key` is Onyx's name for it, in the connector's
    config or, when `secret`, its credential. Optional fields left blank are left out, which for
    scope fields means "everything the account can see"."""

    key: str
    label: str
    kind: str = "text"  # "text", "list" (comma-separated), or "bool"
    required: bool = True
    secret: bool = False
    default: bool = False  # for "bool" fields


@dataclass(frozen=True)
class SourceType:
    """A kind of source admin can add: Onyx's connector type, how Onyx runs it ("load_state" crawls
    everything each time; "poll" fetches only what changed), the fields to ask for, and settings
    that are always the same."""

    source: str
    description: str
    input_type: str
    fields: tuple[SourceField, ...]
    fixed: dict[str, Any] = field(default_factory=dict)


# Onyx reaches Atlassian through api.atlassian.com for a scoped token, which the site's own URL
# refuses (401). Service-account keys are always scoped.
SCOPED_TOKEN_LABEL = "Scoped API token (service-account keys are)"

SOURCE_TYPES = {
    "web": SourceType("web", "a public website, crawled from its base URL", "load_state", (
        SourceField("base_url", "Base URL (pages under it are crawled)"),
    ), {"web_connector_type": "recursive"}),
    "confluence": SourceType("confluence", "a Confluence wiki (Atlassian Cloud)", "poll", (
        SourceField("wiki_base", "Wiki URL, e.g. https://<site>.atlassian.net/wiki"),
        SourceField("space", "Space key (blank: every space the account can see)", required=False),
        SourceField("cql_query", "CQL query instead of a space (blank: none)", required=False),
        SourceField("include_attachments", "Index attachments", kind="bool"),
        SourceField("confluence_username", "Account email", secret=True),
        SourceField("confluence_access_token", "Atlassian API token", secret=True),
        SourceField("scoped_token", SCOPED_TOKEN_LABEL, kind="bool", default=True),
    ), {"is_cloud": True}),
    "jira": SourceType("jira", "Jira issues (Atlassian Cloud)", "poll", (
        SourceField("jira_base_url", "Jira URL, e.g. https://<site>.atlassian.net"),
        SourceField("project_key", "Project key (blank: every project the account can see)", required=False),
        SourceField("jql_query", "JQL query instead of a project (blank: none)", required=False),
        SourceField("jira_user_email", "Account email", secret=True),
        SourceField("jira_api_token", "Atlassian API token", secret=True),
        SourceField("scoped_token", SCOPED_TOKEN_LABEL, kind="bool", default=True),
    )),
    "discourse": SourceType("discourse", "a Discourse forum's topics and posts", "poll", (
        SourceField("base_url", "Forum URL"),
        SourceField("categories", "Categories, comma-separated (blank: every category the account can see)",
                    kind="list", required=False),
        SourceField("discourse_api_username", "API username (an ordinary user, never system)", secret=True),
        SourceField("discourse_api_key", "API key", secret=True),
    )),
    "github": SourceType("github", "GitHub repositories' files, pull requests, and issues", "poll", (
        SourceField("repo_owner", "Organization or user"),
        SourceField("repositories", "Repositories, comma-separated (blank: all of them)", required=False),
        SourceField("include_files", "Index files (README, docs)", kind="bool", default=True),
        SourceField("include_prs", "Index pull requests", kind="bool", default=True),
        SourceField("include_issues", "Index issues", kind="bool"),
        SourceField("github_access_token", "Token (fine-grained, read-only, public repositories)", secret=True),
    )),
}


def parse_bool(value: str) -> bool:
    if value.strip().lower() in ("y", "yes", "true", "1"):
        return True
    if value.strip().lower() in ("n", "no", "false", "0"):
        return False
    raise OnyxError(f"expected yes or no, not {value!r}")


def source_settings(kind: str, name: str, values: dict[str, Any],
                    refresh_hours: float = DEFAULT_REFRESH_HOURS) -> tuple[SourceType, dict, dict]:
    """Checks a new source's settings without calling Onyx, returning its type, connector config,
    and credential. `values` maps field keys to strings (lists comma-separated, booleans as
    yes/no) or, for "bool" fields, booleans."""
    source_type = SOURCE_TYPES.get(kind)
    if source_type is None:
        raise OnyxError(f"unknown source type {kind!r}; expected one of {', '.join(SOURCE_TYPES)}")
    if not name.strip():
        raise OnyxError("a source needs a name")
    if refresh_hours <= 0:
        raise OnyxError("refresh hours must be positive")
    unknown = set(values) - {f.key for f in source_type.fields}
    if unknown:
        raise OnyxError(f"{kind} sources have no {', '.join(sorted(unknown))}")
    config, credential = dict(source_type.fixed), {}
    for f in source_type.fields:
        value = values.get(f.key, "")
        if f.kind == "bool":
            value = value if isinstance(value, bool) else parse_bool(value) if str(value).strip() else f.default
        else:
            value = str(value).strip()
            if not value:
                if f.required:
                    raise OnyxError(f"{f.label} is required")
                continue
            if f.kind == "list":
                value = [item.strip() for item in value.split(",") if item.strip()]
        (credential if f.secret else config)[f.key] = value
    return source_type, config, credential


@dataclass(frozen=True)
class OnyxSettings:
    url: str
    email: str
    password: str
    litellm_url: str

    @classmethod
    def from_env(cls) -> OnyxSettings:
        return cls(
            url=os.environ.get("ONYX_URL", "http://onyx-api:8080"),
            email=os.environ.get("ONYX_ADMIN_EMAIL", ""),
            password=os.environ.get("ONYX_ADMIN_PASSWORD", ""),
            litellm_url=os.environ.get("LITELLM_URL", "http://litellm:4000"),
        )


class OnyxClient:
    """Calls Onyx's API (without the /api prefix nginx would add) with a login session cookie."""

    def __init__(self, url: str) -> None:
        self._url = url.rstrip("/")
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body: Any = None, form: dict[str, str] | None = None,
             ok: tuple[int, ...] = (200, 201, 204)) -> Any:
        data, headers = None, {}
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(f"{self._url}{path}", data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=120) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except urllib.error.URLError as e:
            raise OnyxError(f"cannot reach Onyx at {self._url}: {e.reason}") from None
        if status not in ok:
            raise OnyxError(f"{method} {path} failed with HTTP {status}: {raw[:500].decode(errors='replace')}")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return raw.decode(errors="replace")

    def login(self, email: str, password: str) -> None:
        """Registers the account if it doesn't exist yet, then logs in. On a new Onyx the first
        account registered becomes its administrator."""
        if not email or not password:
            raise OnyxError("ONYX_ADMIN_EMAIL and ONYX_ADMIN_PASSWORD must be set (secrets/onyx-admin.env)")
        # 400 is "already exists"; the login below is the real check.
        self.call("POST", "/auth/register", {"email": email, "password": password}, ok=(201, 400))
        try:
            self.call("POST", "/auth/login", form={"username": email, "password": password})
        except OnyxError as e:
            raise OnyxError(f"cannot log in to Onyx as {email}: {e}") from None


class Onyx:
    def __init__(self, settings: OnyxSettings, api: LiteLLM, client: OnyxClient | None = None) -> None:
        self.settings = settings
        self.api = api
        self._client = client

    @property
    def client(self) -> OnyxClient:
        if self._client is None:
            self._client = OnyxClient(self.settings.url)
            self._client.login(self.settings.email, self.settings.password)
        return self._client

    # -- LiteLLM key and model -----------------------------------------------------------------

    def _key_hashes(self) -> list[str]:
        return [key["token"] for key in self.api.keys()
                if (key.get("metadata") or {}).get("managed_by") == MANAGED_BY
                and (key.get("metadata") or {}).get("hermes_role") == ONYX_ROLE]

    def _set_budget(self, budget: float) -> None:
        limits = {"max_budget": budget, "budget_duration": BUDGET_DURATION}
        if self.api.user_info(ONYX_USER_ID) is None:
            self.api.new_user({"user_id": ONYX_USER_ID, **limits})
        else:
            self.api.update_user(ONYX_USER_ID, limits)

    def _provider(self) -> dict | None:
        providers = self.client.call("GET", "/admin/llm/provider")
        if isinstance(providers, dict):
            providers = providers.get("providers") or []
        return next((p for p in providers if p.get("name") == PROVIDER_NAME), None)

    def _embedding_provider(self) -> dict | None:
        providers = self.client.call("GET", "/admin/embedding/embedding-provider") or []
        return next((p for p in providers if p.get("provider_type") == "litellm"), None)

    def setup(self, budget: float | None = None, rotate_key: bool = False) -> list[str]:
        """Logs in (registering the administrator on a new Onyx) and points Onyx's models, its
        embedding model included, at LiteLLM with its own key. Issues a new key only if Onyx has
        none yet or `rotate_key`; the old ones are revoked once Onyx has the new one."""
        done = []
        old_keys = self._key_hashes()
        provider = self._provider()
        embedding_provider = self._embedding_provider()
        if budget is not None or self.api.user_info(ONYX_USER_ID) is None:
            self._set_budget(DEFAULT_ONYX_BUDGET if budget is None else budget)
            done.append(f"Onyx's monthly LiteLLM budget: ${DEFAULT_ONYX_BUDGET if budget is None else budget:g}")
        if provider is None or embedding_provider is None or not old_keys or rotate_key:
            response = self.api.generate_key({
                "key_alias": f"hermes-onyx-{time.strftime('%Y%m%d%H%M%S')}",
                "user_id": ONYX_USER_ID,
                "models": [ONYX_MODEL, ONYX_EMBEDDING_MODEL],
                "allowed_routes": ONYX_ROUTES,
                "object_permission": {"mcp_servers": [NO_MCP_SERVERS]},
                "metadata": {"managed_by": MANAGED_BY, "hermes_role": ONYX_ROLE},
            })
            body = {
                "name": PROVIDER_NAME,
                "provider": "litellm_proxy",
                "api_key": response["key"],
                "api_key_changed": True,
                "api_base": self.settings.litellm_url,
                "is_public": True,
                "is_auto_mode": False,
                "model_configurations": [{"name": ONYX_MODEL, "is_visible": True}],
            }
            if provider is not None:
                body["id"] = provider["id"]
            self.client.call("PUT", f"/admin/llm/provider?is_creation={str(provider is None).lower()}", body)
            provider = self._provider()
            if provider is None:
                raise OnyxError(f"Onyx accepted the {PROVIDER_NAME!r} model provider but doesn't list it")
            self.client.call("PUT", "/admin/embedding/embedding-provider", {
                "provider_type": "litellm", "api_key": response["key"], "api_key_changed": True,
                "api_url": self._embeddings_url,
            })
            self.api.delete_keys(old_keys)
            done.append("Issued Onyx a new LiteLLM key" + (" and revoked the old one" if old_keys else ""))
        self.client.call("POST", "/admin/llm/default", {"provider_id": provider["id"], "model_name": ONYX_MODEL})
        done.append(f"Onyx uses LiteLLM's {ONYX_MODEL!r} model")
        done.append(self._use_embedding_model())
        return done

    @property
    def _embeddings_url(self) -> str:
        return f"{self.settings.litellm_url.rstrip('/')}/v1/embeddings"

    def _use_embedding_model(self) -> str:
        """Switches Onyx's search settings to LiteLLM's embedding model unless they use it already
        or a switch to it is under way. Onyx starts out with a local model; switching builds a new
        index and re-embeds what's indexed, and search uses the new index once that finishes."""
        for path in ("/search-settings/get-secondary-search-settings", "/search-settings/get-current-search-settings"):
            settings = self.client.call("GET", path)
            if settings and settings.get("provider_type") == "litellm" and settings.get("model_name") == ONYX_EMBEDDING_MODEL:
                return f"Onyx {'is switching to' if 'secondary' in path else 'uses'} LiteLLM's {ONYX_EMBEDDING_MODEL!r} model"
        dimensions = self.api.embedding_dimensions(ONYX_EMBEDDING_MODEL)
        self.client.call("POST", "/search-settings/set-new-search-settings", {
            "model_name": ONYX_EMBEDDING_MODEL, "model_dim": dimensions, "normalize": True,
            "query_prefix": "", "passage_prefix": "", "provider_type": "litellm", "api_url": self._embeddings_url,
            "index_name": None, "multipass_indexing": False, "enable_contextual_rag": False,
        })
        return (f"Onyx is switching to LiteLLM's {ONYX_EMBEDDING_MODEL!r} model ({dimensions} dimensions); "
                "search uses it once Onyx has re-embedded what's indexed")

    # -- search token ----------------------------------------------------------------------------

    def token(self) -> str:
        """A new search-only token for LiteLLM's onyx_search server, replacing any earlier one. It
        belongs to the administrator but can only call Onyx's search endpoints."""
        for pat in self.client.call("GET", "/user/pats") or []:
            if pat.get("name") == SEARCH_TOKEN_NAME:
                self.client.call("DELETE", f"/user/pats/{pat['id']}")
        created = self.client.call("POST", "/user/pats",
                                   {"name": SEARCH_TOKEN_NAME, "expiration_days": None, "scopes": ["read:search"]})
        if not isinstance(created, dict) or not created.get("token"):
            raise OnyxError("Onyx created the search token but didn't return it")
        return created["token"]

    # -- sources ---------------------------------------------------------------------------------

    def sources(self) -> list[dict]:
        """Onyx's sources with their indexing state."""
        listed = []
        for status in self.client.call("GET", "/manage/admin/connector/status") or []:
            pair = self.client.call("GET", f"/manage/admin/cc-pair/{status['cc_pair_id']}")
            listed.append({
                "name": status["name"],
                "source": status["connector"]["source"],
                "status": pair.get("status"),
                "documents": pair.get("num_docs_indexed"),
                "last_index": pair.get("last_index_attempt_status"),
            })
        return listed

    def add(self, kind: str, name: str, values: dict[str, Any], refresh_hours: float = DEFAULT_REFRESH_HOURS) -> str:
        """Creates a source of SOURCE_TYPES[kind] named `name` (see source_settings); Onyx starts
        indexing it shortly."""
        source_type, config, credential = source_settings(kind, name, values, refresh_hours)
        name = name.strip()
        if name in {status["name"] for status in self.client.call("GET", "/manage/admin/connector/status") or []}:
            raise OnyxError(f"Onyx already has a source named {name!r}")
        created = self.client.call("POST", "/manage/credential", {
            "source": source_type.source, "name": name, "credential_json": credential,
            "admin_public": True, "curator_public": False, "groups": [],
        })
        connector = self.client.call("POST", "/manage/admin/connector", {
            "name": name, "source": source_type.source, "input_type": source_type.input_type,
            "connector_specific_config": config, "refresh_freq": int(refresh_hours * 3600),
            "prune_freq": None, "indexing_start": None, "access_type": "public", "groups": [],
        })
        # Public: Onyx's free edition can't restrict documents by group anyway.
        self.client.call("PUT", f"/manage/connector/{connector['id']}/credential/{created['id']}",
                         {"name": name, "access_type": "public", "groups": []})
        return f"Added {name} ({kind}); Onyx starts indexing it shortly"

    def unused(self) -> tuple[list[dict], list[dict]]:
        """The connectors and credentials no source uses, e.g. left behind when Onyx refused an
        `add` part-way. Leftover credentials still hold their tokens."""
        connectors = self.client.call("GET", "/manage/connector") or []
        used = {credential_id for connector in connectors for credential_id in connector.get("credential_ids") or []}
        credentials = [credential for credential in self.client.call("GET", "/manage/admin/credential") or []
                       if credential["id"] != PUBLIC_CREDENTIAL_ID and credential["id"] not in used]
        return [connector for connector in connectors if not connector.get("credential_ids")], credentials

    def cleanup(self, connectors: list[dict], credentials: list[dict]) -> list[str]:
        """Deletes connectors and credentials found by `unused`."""
        lines = []
        for connector in connectors:
            self.client.call("DELETE", f"/manage/admin/connector/{connector['id']}")
            lines.append(f"Deleted connector {connector['name']!r} ({connector['source']})")
        for credential in credentials:
            self.client.call("DELETE", f"/manage/admin/credential/{credential['id']}")
            lines.append(f"Deleted credential {credential['name']!r} ({credential['source']})")
        return lines
