"""Binds each webhook-started session to the pull request that triggered it, and posts one GitHub
review for that pull request.

The webhook route's prompt (hermes/reviewer/config.yaml) starts with one line rendered from fields of
GitHub's signed payload:

    REVIEW-TARGET repo=<owner/name> pr=<number> sha=<40 hex>

On a webhook session's first turn, the pre_llm_call hook binds the session to that line. Later turns,
other platforms, and messages without exactly one such line bind nothing. post_pr_review takes the
repository, pull request, and commit from the binding, never from its parameters, and always posts
with event COMMENT. It refuses without a binding, before the workspace is prepared, after this
session has posted, and for a (repository, pull request, commit) already in posted.json. A review
is recorded there once GitHub accepts it, and also when the outcome is unknown (no answer, a 5xx,
an unreadable answer), so neither a retry nor a redelivered webhook can post a second review.

The github_review token comes from github-token with the agent's LiteLLM key, and stays in this
process: workspaces can't get it.
"""

import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from agent.secret_scope import get_secret

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
TIMEOUT_SECONDS = 30


@dataclass
class Binding:
    repo: str
    pr_number: int
    head_sha: str
    prepared: bool = False
    posted: bool = False


# Hermes session id -> Binding. One lock for all state, held across a post, so two calls can't both
# pass the checks.
_bindings: dict[str, Binding] = {}
_lock = threading.Lock()


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
    lines = [line.strip() for line in user_message.splitlines() if TARGET_MARKER in line]
    match = TARGET_LINE.fullmatch(lines[0]) if len(lines) == 1 else None
    if not match or match.group(1) not in ALLOWED_REPOS:
        return None
    with _lock:
        _bindings.setdefault(session_id, Binding(match.group(1), int(match.group(2)), match.group(3)))
    return None


def _test_mark_prepared(session_id):
    """Test seam for tests/hermes_review_tool.py until prepare_workspace exists: marks a bound
    session's workspace as prepared. Not a tool; the model can't call it."""
    with _lock:
        binding = _bindings.get(session_id)
        if binding:
            binding.prepared = True
        return binding is not None


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
        try:
            posted = _read_posted()
        except (OSError, ValueError) as e:
            return _result(False, error=f"Can't read the record of posted reviews: {e}")
        if _key(binding) in posted:
            return _result(False, error="This pull request's head commit has already been reviewed.")
        try:
            token = _github_token()
        except (OSError, urllib.error.URLError) as e:
            return _result(False, error=f"Can't get a GitHub token ({type(e).__name__}).")
        try:
            status, response = _create_review(binding, body, comments, token)
        except Exception as e:  # noqa: BLE001 - once the request is sent, any failure is an unknown outcome
            return _unknown_outcome(binding, posted, f"GitHub's answer was lost ({type(e).__name__})")
        if status == 422:
            return _result(False, error=f"GitHub refused the review: {response.get('message', 'unprocessable')}. "
                                        "Fix the comments (each line must be in the diff) and call post_pr_review again.")
        if status >= 500:
            return _unknown_outcome(binding, posted, f"GitHub answered {status}")
        if not 200 <= status < 300:
            return _result(False, error=f"GitHub answered {status}: {response.get('message', '')}")
        warning = _record(binding, posted)
        return _result(True, url=response.get("html_url"), **({"warning": warning} if warning else {}))


def _record(binding, posted):
    """Marks the binding posted and adds its key to posted.json; returns a warning if the write
    failed (the session still can't post again)."""
    binding.posted = True
    try:
        _write_posted(posted + [_key(binding)])
    except OSError as e:
        return f"Not recorded in {POSTED_FILE}: {e}"
    return None


def _unknown_outcome(binding, posted, reason):
    """The review may or may not exist now: record it as posted, so no retry or redelivered webhook
    can post a second one."""
    warning = _record(binding, posted)
    return _result(False, error=f"{reason}; the review may or may not have been posted. Don't retry.",
                   **({"warning": warning} if warning else {}))


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
    ctx.register_tool(name="post_pr_review", toolset="github_review", schema=SCHEMA, handler=post_pr_review)
    ctx.register_hook("pre_llm_call", bind_review_target)
