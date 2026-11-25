"""Binds each webhook-started session to the pull request that triggered it, and posts one GitHub
review for that pull request.

The webhook route's prompt (hermes/reviewer/config.yaml), which Hermes appends to the pr-review
skill's text, starts with one line rendered from fields of GitHub's signed payload:

    REVIEW-TARGET repo=<owner/name> pr=<number> sha=<40 hex>

On a webhook session's first turn, the pre_llm_call hook binds the session to that line. Later turns,
other platforms, and messages without exactly one such line bind nothing. post_pr_review takes the
repository, pull request, and commit from the binding, never from its parameters, and always posts
with event COMMENT. It refuses without a binding, before the workspace is prepared, after this
session has posted, while another call is posting the same commit's review, and for a (repository,
pull request, commit) already in posted.json. A review is recorded there once GitHub accepts it,
and also when the outcome is unknown (no answer, a 5xx, an unreadable answer), so neither a retry
nor a redelivered webhook can post a second review.

prepare_workspace, the review's first step, takes the workspace for this session and has its
supervisor reset it (workspace/supervisor.py), so nothing an earlier review's build left behind
remains. It refuses without a binding and for a (repository, pull request, commit) already in
posted.json, so a replayed webhook does no work. One review has the workspace at a time: the next
waits up to LOCK_WAIT_SECONDS, until on_session_end for the holder's webhook turn. That hook doesn't
fire when a turn fails on a model error, so the holder's session keeps a heartbeat (each model
request and tool result); a waiting review takes the workspace from a holder with no heartbeat for
LOCK_STALE_SECONDS, or that has held it for LOCK_BREAK_SECONDS. post_compaction moves the binding and
the workspace to the session a compaction continues in.

A bound session's tool calls, other than prepare_workspace, are blocked (pre_tool_call) unless it has
the workspace and prepare_workspace has reset it for this review: not before then, and not after
another review took the workspace from it. Sessions without a binding are not affected.

Hermes abandons a tool call after a generic deadline (420 s, <H>/agent/tool_executor.py:137), so
prepare_workspace registers a longer one (PREPARE_TIMEOUT_SECONDS). Hermes' tool executor stamps the
turn's activity while a tool runs (<H>/agent/tool_executor.py:626), so the gateway's inactivity
timeout doesn't end the turn while it waits. A wait that Hermes cancels anyway stops without taking
the workspace.

The reset request goes over SSH, but the supervisor's answer is read from a volume that only it
writes (RESET_DONE), never over SSH: every SSH command runs the workspace user's shell, which reads
~/.bashrc first, so an earlier review's build could make it swallow the request and print the nonce.
At worst, that build keeps the request from the supervisor, and prepare_workspace fails.

The github_review token comes from github-token with the agent's LiteLLM key, and stays in this
process: workspaces can't get it.
"""

import json
import logging
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from agent.secret_scope import get_secret
from agent.skill_commands import extract_user_instruction_from_skill_message
from tools.interrupt import is_interrupted
from tools.registry import registry
from tools.terminal_scope import terminal_env

# Also in github-token/levels.json and the webhook route's filter; change all three together.
ALLOWED_REPOS = frozenset({
    "openmrs/openmrs-esm-patient-chart",
    "openmrs/openmrs-esm-patient-management",
    "openmrs/openmrs-core",
})
TARGET_MARKER = "REVIEW-TARGET"
TARGET_LINE = re.compile(r"REVIEW-TARGET repo=([A-Za-z0-9-]+/[A-Za-z0-9._-]+) pr=([1-9][0-9]{0,9}) sha=([0-9a-f]{40})")
POSTED_FILE = Path("/opt/data/github-review/posted.json")
TOKEN_URL = "http://github-token:8080/token/github_review"
# A 422 the model can fix by moving a comment ("Line could not be resolved", "... line must be part of
# the diff"). Any other 422 (a commit no longer in the pull request, say) won't pass on a retry.
LINE_ERROR = re.compile(r"\bline\b|\bpath\b|part of the diff|could not be resolved", re.IGNORECASE)
TIMEOUT_SECONDS = 30
# The agent's workspace: Hermes' terminal settings (hermes/config.yaml), with the host from .env.
SSH_KEY = "/opt/data/ssh/id_ed25519"
SSH_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
RESET_REQUEST_DIR = "/run/workspace-reset"
# The supervisor's answer, on the reviewer-reset volume (read-only here; see compose.yaml).
RESET_DONE = Path("/opt/workspace-reset/reset-done")
RESET_WAIT_SECONDS = 120
RESET_POLL_SECONDS = 2
# Long enough to wait behind a 60-90 minute review. A holder is stale after LOCK_STALE_SECONDS with no
# model request or tool result (no single tool call of a review runs that long), and broken after
# LOCK_BREAK_SECONDS regardless. A waiter rechecks every LOCK_SLICE_SECONDS.
LOCK_WAIT_SECONDS = 2 * 60 * 60
LOCK_STALE_SECONDS = 45 * 60
LOCK_BREAK_SECONDS = 3 * 60 * 60
LOCK_SLICE_SECONDS = 30
# Hermes' deadline for one prepare_workspace call: the longest wait, the reset, and its SSH calls.
PREPARE_TIMEOUT_SECONDS = LOCK_WAIT_SECONDS + RESET_WAIT_SECONDS + 5 * 60

logger = logging.getLogger(__name__)


@dataclass
class Binding:
    repo: str
    pr_number: int
    head_sha: str
    prepared: bool = False
    posted: bool = False
    # A post_pr_review call is sending this review to GitHub.
    posting: bool = False


@dataclass
class Holder:
    session_id: str
    since: float
    heartbeat: float


# Hermes session id -> Binding, and the session that has the workspace. One lock for all state, held
# only briefly: never across a network call, while waiting for the workspace (the condition releases
# it), or while resetting it, because Hermes cuts hooks that wait for it off after 30 s and then skips
# them for 60 s. A post marks its binding `posting` under the lock instead, so two calls can't both
# pass the checks. Tools run on the agent's thread, hooks on Hermes' hook threads.
_bindings: dict[str, Binding] = {}
_holder: Holder | None = None
_lock = threading.Lock()
_workspace_free = threading.Condition(_lock)


def _result(success, **fields):
    return json.dumps({"success": success, **fields})


def bind_review_target(session_id="", user_message="", is_first_turn=False, platform="", **kwargs):
    """pre_llm_call: binds a webhook session's first turn to its REVIEW-TARGET line. Returns None,
    so nothing is added to the prompt."""
    del kwargs
    if platform != "webhook" or not is_first_turn or not session_id or not isinstance(user_message, str):
        return None
    if user_message.count(TARGET_MARKER) != 1:
        return None
    # The route's skill wraps the prompt: "...instruction alongside the skill invocation: REVIEW-TARGET ...".
    instruction = extract_user_instruction_from_skill_message(user_message) or ""
    lines = [line.strip() for line in instruction.splitlines() if TARGET_MARKER in line]
    match = TARGET_LINE.fullmatch(lines[0]) if len(lines) == 1 else None
    if not match or match.group(1) not in ALLOWED_REPOS:
        return None
    with _lock:
        _bindings.setdefault(session_id, Binding(match.group(1), int(match.group(2)), match.group(3)))
    return None


def guard_tool_call(tool_name="", session_id="", **kwargs):
    """pre_tool_call: blocks a bound session's tools, other than prepare_workspace, unless the
    workspace is prepared for it and it still has it."""
    del kwargs
    if not session_id or tool_name == "prepare_workspace":
        return None
    with _lock:
        binding = _bindings.get(session_id)
        if binding is None or (binding.prepared and _holder is not None and _holder.session_id == session_id):
            return None
    return {"action": "block",
            "message": "This review doesn't have the workspace. Call prepare_workspace first; if it already "
                       "succeeded, another review has since taken the workspace, so stop."}


def release_workspace(session_id="", platform="", **kwargs):
    """on_session_end: a webhook turn ended, so its session gives up the workspace."""
    del kwargs
    if platform == "webhook" and session_id:
        _release(session_id)


def heartbeat(session_id="", **kwargs):
    """pre_api_request and post_tool_call: the holder's review is still running."""
    del kwargs
    with _lock:
        if _holder is not None and session_id and _holder.session_id == session_id:
            _holder.heartbeat = time.monotonic()


def follow_compaction(session_id="", old_session_id="", **kwargs):
    """post_compaction: a compaction that continues in a new session moves the binding and the
    workspace there, so the review can still post and on_session_end still releases."""
    global _holder
    del kwargs
    if not old_session_id or not session_id or old_session_id == session_id:
        return
    with _lock:
        binding = _bindings.pop(old_session_id, None)
        if binding is not None:
            _bindings[session_id] = binding
        if _holder is not None and _holder.session_id == old_session_id:
            _holder.session_id = session_id


def _release(session_id):
    """Frees the workspace if this session has it; its review must prepare again to post."""
    global _holder
    with _lock:
        if _holder is None or _holder.session_id != session_id:
            return
        _holder = None
        binding = _bindings.get(session_id)
        if binding is not None:
            binding.prepared = False
        _workspace_free.notify_all()


def _take_workspace(session_id):
    """Waits for the workspace and gives it to this session; returns an error message if it stays
    taken. Call with _lock held."""
    global _holder
    if _holder is not None and _holder.session_id == session_id:
        return None
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while _holder is not None:
        now = time.monotonic()
        idle, held = now - _holder.heartbeat, now - _holder.since
        if idle >= LOCK_STALE_SECONDS or held >= LOCK_BREAK_SECONDS:
            logger.warning("github-review: session %s takes the workspace from session %s (held %d s, no activity "
                           "for %d s)", session_id, _holder.session_id, held, idle)
            stale = _bindings.get(_holder.session_id)
            if stale is not None:
                stale.prepared = False
            break
        if is_interrupted():
            logger.warning("github-review: session %s stopped waiting for the workspace (call cancelled)", session_id)
            return "The call was cancelled while waiting for the workspace."
        if now >= deadline:
            logger.warning("github-review: session %s gave up after waiting %d s for the workspace, held by session %s",
                           session_id, LOCK_WAIT_SECONDS, _holder.session_id)
            return (f"This review was not done: another review held the workspace for the whole "
                    f"{LOCK_WAIT_SECONDS // 60}-minute wait. Stop; don't retry.")
        _workspace_free.wait(min(deadline - now, LOCK_SLICE_SECONDS))
    now = time.monotonic()
    _holder = Holder(session_id, now, now)
    return None


def _ssh(host, command):
    """Runs a command in the workspace on a new connection (not Hermes' shared one, which a reset
    closes)."""
    return subprocess.run(
        ["ssh", "-i", SSH_KEY, "-p", "2222", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
         "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o", "ControlPath=none",
         f"workspace@{host}", command],
        capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False)


def _reset_workspace():
    """Asks the workspace's supervisor for a reset and waits until it has written back our nonce to
    RESET_DONE, which only it can. Raises if it can't."""
    # TERMINAL_* is not a profile secret (get_secret reads it from the process environment); the
    # terminal tool reads it from the turn's terminal scope, built from the agent's .env.
    host = terminal_env("TERMINAL_SSH_HOST")
    if not SSH_HOST.fullmatch(host):
        raise RuntimeError("TERMINAL_SSH_HOST is not set")
    nonce = secrets.token_hex(16)
    # Renamed into place, so the supervisor never sees a half-written request.
    request = _ssh(host, f"t=$(mktemp {RESET_REQUEST_DIR}/.request-XXXXXX) && printf '%s\\n' {nonce} > \"$t\" && "
                         f"mv -f \"$t\" {RESET_REQUEST_DIR}/request")
    if request.returncode != 0:
        raise RuntimeError(f"can't reach the workspace ({request.stderr.strip()[-200:]})")
    deadline = time.monotonic() + RESET_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(RESET_POLL_SECONDS)
        try:
            if RESET_DONE.read_text().strip() == nonce:
                return
        except FileNotFoundError:
            continue
    raise RuntimeError(f"the workspace didn't reset within {RESET_WAIT_SECONDS} seconds")


def _refusal(binding):
    """Why this binding's review can't start, or None. Call with _lock held."""
    if binding is None:
        return "This session is not bound to a pull request; only a review the webhook started can prepare a workspace."
    if binding.posted:
        return "This session has already posted its review."
    try:
        posted = _read_posted()
    except (OSError, ValueError) as e:
        return f"Can't read the record of posted reviews: {e}"
    if _key(binding) in posted:
        return "This pull request's head commit has already been reviewed."
    return None


def prepare_workspace(args, session_id="", **kwargs):
    del args, kwargs
    with _lock:
        binding = _bindings.get(session_id)
        refusal = _refusal(binding)
        if refusal:
            return _result(False, error=refusal)
        error = _take_workspace(session_id)
        if error:
            return _result(False, error=error)
        # Checked again: another session may have posted this commit's review while this one waited.
        refusal = _refusal(binding)
    if refusal:
        _release(session_id)
        return _result(False, error=refusal)
    try:
        _reset_workspace()
    except BaseException as e:
        _release(session_id)
        if not isinstance(e, Exception):
            raise
        return _result(False, error=f"Can't prepare the workspace: {e}")
    with _lock:
        binding.prepared = True
    return _result(True)


def _key(binding):
    return f"{binding.repo}#{binding.pr_number}@{binding.head_sha}"


def _read_posted():
    """The (repository, PR, commit) keys already reviewed. Raises if the file can't be read, so a
    damaged record refuses rather than allows a second review."""
    try:
        posted = json.loads(POSTED_FILE.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(posted, list):
        raise ValueError(f"{POSTED_FILE} is not a list")
    return posted


def _write_posted(posted):
    POSTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=POSTED_FILE.parent, prefix=".posted-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(posted, f, indent=1)
        os.replace(tmp, POSTED_FILE)
    except BaseException:
        os.unlink(tmp)
        raise


def _comments(raw):
    """GitHub's review comment fields from the model's comments, or an error message."""
    if not isinstance(raw, list):
        return None, "comments must be a list"
    comments = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            return None, f"comments[{i}] must be an object"
        path, line, body = c.get("path"), c.get("line"), c.get("body")
        if not isinstance(path, str) or not path or not isinstance(body, str) or not body:
            return None, f"comments[{i}] needs a path and a body"
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            return None, f"comments[{i}].line must be a positive integer"
        comment = {"path": path, "line": line, "body": body}
        side = c.get("side")
        if side is not None:
            if side not in ("LEFT", "RIGHT"):
                return None, f"comments[{i}].side must be LEFT or RIGHT"
            comment["side"] = side
        start_line = c.get("start_line")
        if start_line is not None:
            if not isinstance(start_line, int) or isinstance(start_line, bool) or not 1 <= start_line < line:
                return None, f"comments[{i}].start_line must be a positive integer below line"
            comment["start_line"] = start_line
            if side is not None:
                comment["start_side"] = side
        comments.append(comment)
    return comments, None


def _github_token():
    request = urllib.request.Request(TOKEN_URL, headers={"Authorization": f"Bearer {get_secret('LITELLM_API_KEY')}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode().strip()


def _json_or_empty(raw):
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _create_review(binding, body, comments, token):
    """POSTs the review; returns (status, response JSON, or {} if the body isn't a JSON object).
    Raises on network errors."""
    api = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    payload = {"commit_id": binding.head_sha, "event": "COMMENT", "body": body, "comments": comments}
    request = urllib.request.Request(
        f"{api.rstrip('/')}/repos/{binding.repo}/pulls/{binding.pr_number}/reviews",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, _json_or_empty(response.read())
    except urllib.error.HTTPError as e:
        return e.code, _json_or_empty(e.read())


def post_pr_review(args, session_id="", **kwargs):
    del kwargs
    body = args.get("body") if isinstance(args, dict) else None
    if not isinstance(body, str) or not body.strip():
        return _result(False, error="body must be a non-empty string")
    comments, error = _comments(args.get("comments", []))
    if error:
        return _result(False, error=error)
    with _lock:
        binding = _bindings.get(session_id)
        if binding is None:
            return _result(False, error="This session is not bound to a pull request; only a review the webhook started can post.")
        if not binding.prepared:
            return _result(False, error="The workspace has not been prepared for this review.")
        if binding.posted:
            return _result(False, error="This session has already posted its review.")
        if any(b.posting and _key(b) == _key(binding) for b in _bindings.values()):
            return _result(False, error="A review of this pull request's head commit is being posted; wait for that "
                                        "call's result.")
        try:
            posted = _read_posted()
        except (OSError, ValueError) as e:
            return _result(False, error=f"Can't read the record of posted reviews: {e}")
        if _key(binding) in posted:
            return _result(False, error="This pull request's head commit has already been reviewed.")
        binding.posting = True
    try:
        return _post(binding, body, comments)
    finally:
        with _lock:
            binding.posting = False


def _post(binding, body, comments):
    """Gets a token and POSTs the review, without _lock; records it when GitHub accepted it or the
    outcome is unknown."""
    try:
        token = _github_token()
    except (OSError, urllib.error.URLError) as e:
        return _result(False, error=f"Can't get a GitHub token ({type(e).__name__}).")
    try:
        status, response = _create_review(binding, body, comments, token)
    except urllib.error.URLError as e:
        # GitHub's address didn't resolve or the connection was refused: the request never left.
        if isinstance(e.reason, (socket.gaierror, ConnectionRefusedError)):
            return _result(False, error=f"Can't reach GitHub ({type(e.reason).__name__}); nothing was sent.")
        return _unknown_outcome(binding, f"GitHub's answer was lost ({type(e).__name__})")
    except Exception as e:  # noqa: BLE001 - once the request is sent, any failure is an unknown outcome
        return _unknown_outcome(binding, f"GitHub's answer was lost ({type(e).__name__})")
    if status == 422:
        message = " ".join([str(response.get("message", "unprocessable")), *map(str, response.get("errors") or [])])
        if not LINE_ERROR.search(message):
            return _result(False, error=f"GitHub refused the review: {message}. Don't retry.")
        return _result(False, error=f"GitHub refused the review: {message}. "
                                    "Fix the comments (each line must be in the diff) and call post_pr_review again.")
    if status >= 500:
        return _unknown_outcome(binding, f"GitHub answered {status}")
    if not 200 <= status < 300:
        return _result(False, error=f"GitHub answered {status}: {response.get('message', '')}")
    warning = _record(binding)
    return _result(True, url=response.get("html_url"), **({"warning": warning} if warning else {}))


def _record(binding):
    """Marks the binding posted and adds its key to posted.json, read again so a review another
    session recorded during the post is kept; returns a warning if that failed (the session still
    can't post again)."""
    with _lock:
        binding.posted = True
        try:
            posted = _read_posted()
            if _key(binding) not in posted:
                _write_posted(posted + [_key(binding)])
        except (OSError, ValueError) as e:
            return f"Not recorded in {POSTED_FILE}: {e}"
    return None


def _unknown_outcome(binding, reason):
    """The review may or may not exist now: record it as posted, so no retry or redelivered webhook
    can post a second one."""
    warning = _record(binding)
    return _result(False, error=f"{reason}; the review may or may not have been posted. Don't retry.",
                   **({"warning": warning} if warning else {}))


PREPARE_SCHEMA = {
    "name": "prepare_workspace",
    "description": (
        "Reset your workspace to an empty /work/home for this review. Call it first, before anything else: it waits "
        "while another review uses the workspace, deletes everything in /work, and ends all running commands. "
        "post_pr_review refuses until it has succeeded."),
    "parameters": {"type": "object", "properties": {}},
}

SCHEMA = {
    "name": "post_pr_review",
    "description": (
        "Post your review of the pull request you were asked to review, as one GitHub review with inline "
        "comments. The pull request and commit are fixed by the webhook; you can't choose them. Call it once, "
        "at the end. If GitHub refuses a comment's line, fix the comments and call it again."),
    "parameters": {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": "The review's summary, in Markdown."},
            "comments": {
                "type": "array",
                "description": "Inline comments on lines of the diff.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to the repository root."},
                        "line": {"type": "integer", "description": "Line in the file the comment is on (the last line for a range)."},
                        "side": {"type": "string", "enum": ["LEFT", "RIGHT"],
                                 "description": "RIGHT (default) for the new version, LEFT for removed lines."},
                        "start_line": {"type": "integer", "description": "First line, for a comment on a range."},
                        "body": {"type": "string", "description": "The comment, in Markdown."},
                    },
                    "required": ["path", "line", "body"],
                },
            },
        },
        "required": ["body", "comments"],
    },
}


def register(ctx):
    ctx.register_tool(name="prepare_workspace", toolset="github_review", schema=PREPARE_SCHEMA,
                      handler=prepare_workspace)
    # The plugin API takes no deadline; MCP tools set the same field (<H>/tools/registry.py:232).
    entry = registry.get_entry("prepare_workspace")
    if entry is not None:
        entry.execution_timeout = PREPARE_TIMEOUT_SECONDS
    else:
        logger.warning("github-review: prepare_workspace keeps Hermes' generic tool deadline")
    ctx.register_tool(name="post_pr_review", toolset="github_review", schema=SCHEMA, handler=post_pr_review)
    ctx.register_hook("pre_llm_call", bind_review_target)
    ctx.register_hook("pre_tool_call", guard_tool_call)
    ctx.register_hook("on_session_end", release_workspace)
    ctx.register_hook("post_compaction", follow_compaction)
    ctx.register_hook("pre_api_request", heartbeat)
    ctx.register_hook("post_tool_call", heartbeat)
