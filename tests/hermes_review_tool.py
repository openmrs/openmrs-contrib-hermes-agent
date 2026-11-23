"""Calls the github-review plugin's hooks and its prepare_workspace and post_pr_review tools the way
Hermes does, used by tests/run.sh.

Runs in agent-reviewer (on stdin, as the Hermes user). Loads the plugin through Hermes' own plugin
discovery, so it also checks that the managed config (/etc/hermes/config.yaml) enables it. Each case
uses its own session id; prepare_workspace really resets workspace-reviewer, and posts go to
tests/fake_github.py, whose log run.sh checks. Prints one summary line, `<case>=<outcome>` for each
case: "posted", "refused", "github-422" (GitHub's message for a line outside the diff came back),
"unknown" (the tool can't tell whether GitHub created the review), "prepared", or a case's own
outcome. With the argument `stopped` (workspace-reviewer stopped), runs only that case.

The tools need the agent's LiteLLM key for github-token and its workspace host, which Hermes reads
from the profile's secret scope; a bare `compose exec` has no scope, so the scope is loaded here from
the agent's .env first, and the terminal scope too, for the terminal tool.
"""

import contextvars
import json
import sys
import threading
import time

from agent.secret_scope import build_profile_secret_scope, set_secret_scope
from hermes_cli.plugins import discover_plugins, get_plugin_manager, invoke_hook
from hermes_constants import get_hermes_home
from model_tools import handle_function_call
from tools.terminal_scope import install_profile_terminal_scope

set_secret_scope(build_profile_secret_scope(get_hermes_home()), profile_home=str(get_hermes_home()))
install_profile_terminal_scope(get_hermes_home())
discover_plugins()
plugin = next(p.module for p in get_plugin_manager()._plugins.values()
              if p.manifest.name == "github-review" and p.enabled and p.module)

CHART = "openmrs/openmrs-esm-patient-chart"
COMMENTS = [
    {"path": "src/a.ts", "line": 3, "body": "First"},
    {"path": "src/b.ts", "line": 8, "side": "RIGHT", "start_line": 6, "body": "Second"},
]


def target(repo, pr, sha):
    return f"REVIEW-TARGET repo={repo} pr={pr} sha={sha}"


def bind(session, message, platform="webhook", first_turn=True):
    invoke_hook("pre_llm_call", session_id=session, task_id=session, turn_id="t1", user_message=message,
                conversation_history=[], is_first_turn=first_turn, model="auto", platform=platform,
                parent_session_id="", sender_id="")


def prepare(session):
    result = json.loads(handle_function_call("prepare_workspace", {}, task_id=session, session_id=session))
    return "prepared" if result.get("success") else "refused"


def end(session, platform="webhook"):
    """The turn ends: Hermes' on_session_end, which releases the workspace."""
    invoke_hook("on_session_end", session_id=session, task_id=session, turn_id="t1", completed=True, failed=False,
                interrupted=False, turn_exit_reason="text_response", model="auto", platform=platform)


def bind_prepared(session, message, **kwargs):
    bind(session, message, **kwargs)
    return prepare(session)


def terminal(session, command):
    return json.loads(handle_function_call("terminal", {"command": command}, task_id=session))["output"].strip()


def post(session, args):
    result = json.loads(handle_function_call("post_pr_review", args, task_id=session, session_id=session))
    if result.get("success"):
        return "posted"
    if "must be part of the diff" in str(result.get("error")):
        return "github-422"
    if "may or may not have been posted" in str(result.get("error")):
        return "unknown"
    return "refused"


review = {"body": "Looks fine overall.", "comments": COMMENTS}
outcomes = {}

if sys.argv[1:] == ["stopped"]:
    # workspace-reviewer is stopped: prepare_workspace fails, so post_pr_review refuses.
    bind("review-stopped", target(CHART, 18, "5" * 40))
    outcomes["prepare"] = prepare("review-stopped")
    outcomes["post"] = post("review-stopped", review)
    end("review-stopped")
    print(" ".join(f"{case}={outcome}" for case, outcome in outcomes.items()))
    sys.exit()

# Hermes gives prepare_workspace its own deadline, longer than the generic 420 s, to wait behind a
# long review.
from tools.registry import registry  # noqa: E402

deadline = registry.get_entry("prepare_workspace").execution_timeout
outcomes["deadline"] = "extended" if deadline == plugin.PREPARE_TIMEOUT_SECONDS > 7200 else str(deadline)

# (j) prepare_workspace kills what an earlier review left running and deletes its files, also one
# left with the reset requests, and the terminal still works after the reset closed Hermes' SSH
# connection.
# Counts sleep processes; the image has no pgrep.
SLEEPS = "$(grep -l '^Name:[[:space:]]*sleep$' /proc/[0-9]*/status 2>/dev/null | wc -l)"
planted = terminal("review-j", "echo x > /work/home/planted; echo x > /run/workspace-reset/.planted; "
                               "(setsid sleep 999 >/dev/null 2>&1 &); sleep 1; "
                               f"echo $(ls /work/home/planted /run/workspace-reset/.planted) {SLEEPS}")
bind("review-j", target(CHART, 10, "9" * 40))
outcomes["j"] = prepare("review-j")
left = terminal("review-j", f"echo $(ls /work/home/planted /run/workspace-reset/.planted 2>/dev/null) {SLEEPS}")
outcomes["j-reset"] = ("clean" if (planted, left) == ("/run/workspace-reset/.planted /work/home/planted 1", "0")
                       else f"{planted!r}->{left!r}")
end("review-j")

# (k) one review at a time: a second session's prepare_workspace waits until the first session's
# turn ends.
bind_prepared("review-k1", target(CHART, 10, "8" * 40))
bind("review-k2", target(CHART, 10, "7" * 40))
second = {}
# The secret scope is a context variable, which a new thread doesn't inherit.
waiter = threading.Thread(target=contextvars.copy_context().run,
                          args=(lambda: second.update(outcome=prepare("review-k2")),))
waiter.start()
time.sleep(8)
waited = waiter.is_alive()
end("review-k1")
waiter.join(150)
outcomes["k"] = "waited" if waited and second.get("outcome") == "prepared" else f"waited={waited} {second}"
end("review-k2")

# Short limits for (s) and (l): stale after 4 s with no heartbeat, waiters recheck every second.
limits = plugin.LOCK_WAIT_SECONDS, plugin.LOCK_STALE_SECONDS, plugin.LOCK_SLICE_SECONDS
plugin.LOCK_STALE_SECONDS, plugin.LOCK_SLICE_SECONDS = 4, 1


def heartbeat(session):
    """The session's review is still running: a model request, then a tool result."""
    invoke_hook("pre_api_request", session_id=session, task_id=session, turn_id="t1", api_request_id="r1",
                platform="webhook", model="auto")
    invoke_hook("post_tool_call", tool_name="terminal", args={}, result="", task_id=session, session_id=session,
                tool_call_id="c1", turn_id="t1", api_request_id="r1", duration_ms=1, status="ok")


# (s) a holder whose turn failed (no heartbeat, no on_session_end) is stale: a waiting session takes
# the workspace within a slice, and the old holder can't post.
bind_prepared("review-s1", target(CHART, 10, "5" * 40))
time.sleep(5)
bind("review-s2", target(CHART, 10, "4" * 40))
started = time.monotonic()
outcomes["s"] = prepare("review-s2")
outcomes["s-fast"] = "yes" if time.monotonic() - started < 30 else f"{time.monotonic() - started:.0f}s"
outcomes["s-old"] = post("review-s1", review)
end("review-s1")
end("review-s2")

# (l) a holder that keeps its heartbeat is not broken: the waiter (a 10-second wait) gives up and
# says why.
bind_prepared("review-l1", target(CHART, 10, "3" * 40))
bind("review-l2", target(CHART, 10, "2" * 40))
plugin.LOCK_WAIT_SECONDS = 10
second = {}
waiter = threading.Thread(target=contextvars.copy_context().run, args=(lambda: second.update(
    result=json.loads(handle_function_call("prepare_workspace", {}, task_id="review-l2", session_id="review-l2"))),))
waiter.start()
while waiter.is_alive():
    heartbeat("review-l1")
    time.sleep(1)
error = str(second.get("result", {}).get("error"))
outcomes["l"] = "refused" if "another review held the workspace" in error else str(second)
end("review-l1")
end("review-l2")
plugin.LOCK_WAIT_SECONDS, plugin.LOCK_STALE_SECONDS, plugin.LOCK_SLICE_SECONDS = limits

# (a) bound and prepared: posts. (b) a second post in the same session: refused.
outcomes["a0"] = bind_prepared("review-a", f"{target(CHART, 11, 'a' * 40)}\nReview this pull request.")
outcomes["a"] = post("review-a", review)
outcomes["b"] = post("review-a", review)
end("review-a")
# (c) bound but not prepared.
bind("review-c", target(CHART, 12, "c" * 40))
outcomes["c"] = post("review-c", review)
end("review-c")
# (d) no binding: no REVIEW-TARGET line, a repository outside the list, another platform, a later
# turn, two REVIEW-TARGET lines, a malformed line.
outcomes["d0"] = bind_prepared("review-d1", "Review openmrs/openmrs-esm-patient-chart#12, please.")
outcomes["d1"] = post("review-d1", review)
bind_prepared("review-d2", target("openmrs/openmrs-module-fhir2", 12, "d" * 40))
outcomes["d2"] = post("review-d2", review)
bind_prepared("review-d3", target(CHART, 12, "d" * 40), platform="slack")
outcomes["d3"] = post("review-d3", review)
bind_prepared("review-d4", target(CHART, 12, "d" * 40), first_turn=False)
outcomes["d4"] = post("review-d4", review)
bind_prepared("review-d5", f"{target(CHART, 12, 'd' * 40)}\n{target(CHART, 13, 'e' * 40)}")
outcomes["d5"] = post("review-d5", review)
bind_prepared("review-d6", target(CHART, 12, "D" * 40))
outcomes["d6"] = post("review-d6", review)
# (e) a new session bound to the PR and commit (a) already reviewed (a replayed webhook): neither
# prepare_workspace nor post_pr_review.
outcomes["e0"] = bind_prepared("review-e", target(CHART, 11, "a" * 40))
outcomes["e"] = post("review-e", review)
end("review-e")
# (f) repo, pr_number, commit_id, and event in the parameters are ignored.
bind_prepared("review-f", target("openmrs/openmrs-core", 13, "f" * 40))
outcomes["f"] = post("review-f", {"body": "Other PR?", "comments": COMMENTS[:1], "repo": CHART, "pr_number": 99,
                                   "commit_id": "0" * 40, "event": "APPROVE"})
end("review-f")
# (g) GitHub refuses a line outside the diff: the model gets GitHub's message and can post again.
bind_prepared("review-g", target("openmrs/openmrs-esm-patient-management", 14, "1" * 40))
outcomes["g1"] = post("review-g", {"body": "x", "comments": [{"path": "not-in-diff.txt", "line": 1, "body": "y"}]})
outcomes["g2"] = post("review-g", review)
end("review-g")
# (h) GitHub answers 500: the outcome is unknown, so neither a retry nor a new session for the same
# commit (a redelivered webhook) may post.
bind_prepared("review-h", target(CHART, 15, "2" * 40))
outcomes["h1"] = post("review-h", review)
outcomes["h2"] = post("review-h", review)
end("review-h")
bind_prepared("review-h3", target(CHART, 15, "2" * 40))
outcomes["h3"] = post("review-h3", review)
end("review-h3")
# (i) GitHub answers 200 with a body that isn't JSON: posted, and recorded like any other post.
bind_prepared("review-i", target(CHART, 16, "3" * 40))
outcomes["i1"] = post("review-i", review)
outcomes["i2"] = post("review-i", review)
end("review-i")
bind_prepared("review-i3", target(CHART, 16, "3" * 40))
outcomes["i3"] = post("review-i3", review)
end("review-i3")

# (m) Hermes compacts session m1 into m2 mid-review: m2 keeps the binding and the workspace, m1 has
# neither. While m2 holds the workspace, another session's prepare_workspace times out (the wait is
# shortened to 3 seconds), even after m1's turn ends; after m2's, it gets the workspace.
bind_prepared("review-m1", target(CHART, 17, "4" * 40))
invoke_hook("post_compaction", session_id="review-m2", old_session_id="review-m1", kind="summarize",
            reason="manual", before_tokens=1000, after_tokens=100, messages_removed=10)
outcomes["m1"] = post("review-m1", review)
outcomes["m2"] = post("review-m2", review)
plugin.LOCK_WAIT_SECONDS = 3
bind("review-n", target(CHART, 10, "6" * 40))
end("review-m1")
outcomes["n1"] = prepare("review-n")
end("review-m2")
outcomes["n2"] = prepare("review-n")
end("review-n")

print(" ".join(f"{case}={outcome}" for case, outcome in outcomes.items()))
