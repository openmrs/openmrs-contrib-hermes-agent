"""Calls the github-review plugin's hooks and its prepare_workspace and post_pr_review tools the way
Hermes does, used by tests/run.sh.

Runs in agent-reviewer (on stdin, as the Hermes user). Loads the plugin through Hermes' own plugin
discovery, so it also checks that the managed config (/etc/hermes/config.yaml) enables it. Each case
uses its own session id; prepare_workspace really resets workspace-reviewer, and posts go to
tests/fake_github.py, whose log run.sh checks. Prints one summary line, `<case>=<outcome>` for each
case: "posted", "refused", "github-422" (GitHub's message for a line outside the diff came back),
"github-422-final" (a 422 the model must not retry), "unsent" (GitHub couldn't be reached),
"unknown" (the tool can't tell whether GitHub created the review), "prepared", or a case's own
outcome. With the argument `stopped` (workspace-reviewer stopped) or `bashrc` (a planted ~/.bashrc
in the workspace), runs only that case.

The tools need the agent's LiteLLM key for github-token and its workspace host, which Hermes reads
from the profile's secret scope; a bare `compose exec` has no scope, so the scope is loaded here from
the agent's .env first, and the terminal scope too, for the terminal tool.
"""

import contextvars
import json
import os
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
# Until case (bb), GitHub's answer for a pull request is open, not a draft, at the bound commit: the cases use
# commits fake-github doesn't know. (bb) checks the real request.
real_pull_request = plugin._pull_request
plugin._pull_request = lambda binding: ("open", binding.head_sha, False)

CHART = "openmrs/openmrs-esm-patient-chart"
COMMENTS = [
    {"path": "src/a.ts", "line": 3, "body": "First"},
    {"path": "src/b.ts", "line": 8, "side": "RIGHT", "start_line": 6, "body": "Second"},
]


def target(repo, pr, sha):
    return f"REVIEW-TARGET repo={repo} pr={pr} sha={sha}"


def bind(session, message, platform="webhook", first_turn=True):
    """Hermes' pre_llm_call; returns the context the hooks add to the prompt."""
    results = invoke_hook("pre_llm_call", session_id=session, task_id=session, turn_id="t1", user_message=message,
                          conversation_history=[], is_first_turn=first_turn, model="auto", platform=platform,
                          parent_session_id="", sender_id="")
    return " ".join(str(r.get("context")) for r in results or [] if isinstance(r, dict) and r.get("context"))


def prepare(session):
    result = json.loads(handle_function_call("prepare_workspace", {}, task_id=session, session_id=session))
    if result.get("success"):
        return "prepared"
    if "newer commit" in str(result.get("error")):
        return "superseded"
    if "is closed" in str(result.get("error")):
        return "closed"
    if "is a draft" in str(result.get("error")):
        return "draft"
    return "refused"


def end(session, platform="webhook"):
    """The turn ends: Hermes' on_session_end, which releases the workspace."""
    invoke_hook("on_session_end", session_id=session, task_id=session, turn_id="t1", completed=True, failed=False,
                interrupted=False, turn_exit_reason="text_response", model="auto", platform=platform)


def bind_prepared(session, message, **kwargs):
    bind(session, message, **kwargs)
    return prepare(session)


def terminal(session, command):
    return json.loads(handle_function_call("terminal", {"command": command}, task_id=session))["output"].strip()


def terminal_as(session, command):
    """The terminal tool as the session's own model calls it, so its pre_tool_call hook sees the
    session: "blocked" if the plugin refused the call, else the command's output."""
    result = json.loads(handle_function_call("terminal", {"command": command}, task_id=session, session_id=session))
    if "prepare_workspace" in str(result.get("error", "")):
        return "blocked"
    return str(result.get("output", result)).strip()


def post(session, args):
    result = json.loads(handle_function_call("post_pr_review", args, task_id=session, session_id=session))
    if result.get("success"):
        return "posted"
    if "must be part of the diff" in str(result.get("error")):
        return "github-422"
    if "refused the review" in str(result.get("error")) and "Don't retry" in str(result.get("error")):
        return "github-422-final"
    if "nothing was sent" in str(result.get("error")):
        return "unsent"
    if "may or may not have been posted" in str(result.get("error")):
        return "unknown"
    if "being posted" in str(result.get("error")):
        return "busy"
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

if sys.argv[1:] == ["bashrc"]:
    # An earlier build left a ~/.bashrc that swallows the reset request and makes `cat` print its
    # nonce: prepare_workspace reads the answer from its volume, so it refuses (a 6-second wait).
    plugin.RESET_WAIT_SECONDS = 6
    bind("review-bashrc", target(CHART, 31, "6" * 40))
    outcomes["prepare"] = prepare("review-bashrc")
    end("review-bashrc")
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

# (p) a bound session's tools run only while it has the workspace: not before prepare_workspace,
# and not after another review took the workspace from it. Other sessions' tools are not affected.
bind("review-p1", target(CHART, 20, "b" * 40))
outcomes["p-before"] = terminal_as("review-p1", "echo ran")
outcomes["p"] = prepare("review-p1")
outcomes["p-after"] = terminal_as("review-p1", "echo ran")
time.sleep(5)
bind_prepared("review-p2", target(CHART, 20, "c" * 40))
outcomes["p-taken"] = terminal_as("review-p1", "echo ran")
bind("review-p3", target(CHART, 20, "d" * 40), platform="slack")
outcomes["p-unbound"] = terminal_as("review-p3", "echo ran")
end("review-p1")
end("review-p2")

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
outcomes["l"] = "refused" if "other reviews held the workspace" in error else str(second)
end("review-l1")
end("review-l2")
plugin.LOCK_WAIT_SECONDS, plugin.LOCK_STALE_SECONDS, plugin.LOCK_SLICE_SECONDS = limits

# (a) bound and prepared: posts. (b) a second post in the same session: refused.
outcomes["a0"] = bind_prepared("review-a", f"{target(CHART, 11, 'a' * 40)}\nReview this pull request.")
outcomes["a"] = post("review-a", review)
outcomes["b"] = post("review-a", review)
end("review-a")
# (q) a post in progress holds no lock (hooks and other sessions go on), and a second post in the
# same session is refused until it ends. Its token request waits until the second post is refused.
token_started, token_release = threading.Event(), threading.Event()
github_token = plugin._github_token


def slow_github_token():
    token_started.set()
    token_release.wait(20)
    return github_token()


plugin._github_token = slow_github_token
bind_prepared("review-q", target(CHART, 19, "b" * 40))
first = {}
poster = threading.Thread(target=contextvars.copy_context().run,
                          args=(lambda: first.update(outcome=post("review-q", review)),))
poster.start()
token_started.wait(20)
unlocked = plugin._lock.acquire(timeout=2)
if unlocked:
    plugin._lock.release()
outcomes["q-unlocked"] = "yes" if unlocked else "no"
threading.Timer(3, token_release.set).start()
outcomes["q-second"] = post("review-q", review)
token_release.set()
poster.join(60)
outcomes["q"] = first.get("outcome", "running")
plugin._github_token = github_token
end("review-q")
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
# (r) GitHub's 422 that isn't about a line (the commit left the pull request): the model is told
# not to retry, and nothing is recorded.
bind_prepared("review-r", target(CHART, 30, "e" * 40))
outcomes["r"] = post("review-r", review)
# ...and it leaves the review queue, so no later session tries it again.
outcomes["r-queue"] = "none" if plugin._read_queue().get(f"{CHART}#30") is None else "kept"
end("review-r")
# (t) GitHub's address refuses the connection: nothing was sent, so nothing is recorded, and the
# same session posts once GitHub is back.
bind_prepared("review-t", target(CHART, 32, "7" * 40))
github_api = os.environ["GITHUB_API_URL"]
os.environ["GITHUB_API_URL"] = "http://127.0.0.1:9"
outcomes["t1"] = post("review-t", review)
os.environ["GITHUB_API_URL"] = github_api
outcomes["t2"] = post("review-t", review)
end("review-t")

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

# The review queue. Waiters recheck every second.
plugin.LOCK_WAIT_SECONDS, plugin.LOCK_SLICE_SECONDS = 60, 1
plugin._write_queue({})


def entry(repo, pr):
    return plugin._read_queue().get(f"{repo}#{pr}")


def in_thread(function):
    """Runs function in a thread with this context (the secret scope is a context variable)."""
    thread = threading.Thread(target=contextvars.copy_context().run, args=(function,))
    thread.start()
    return thread


def wake_gate():
    """The review-queue job's wake script: "sleep" if it skips the model, else "wake"."""
    import subprocess
    out = subprocess.run([sys.executable, "/opt/data/scripts/review-queue-check.py"], capture_output=True, text=True,
                         check=False).stdout.strip().splitlines()
    return "sleep" if out and out[-1] == '{"wakeAgent": false}' else f"wake:{out}"


def queue_state(repo, pr):
    e = entry(repo, pr)
    return "none" if e is None else f"{e['attempts']}:{(e.get('owner') or {}).get('session')}"


# (v) a bound session queues its pull request; a redelivery of the same commit counts another
# session; both ending leave the entry free.
bind("review-v", target(CHART, 50, "1" * 40))
outcomes["v"] = queue_state(CHART, 50)
bind("review-v2", target(CHART, 50, "1" * 40))
outcomes["v2"] = queue_state(CHART, 50)
end("review-v")
end("review-v2")
outcomes["v-end"] = queue_state(CHART, 50)

# (w) a newer commit of the pull request a review waits for: the waiting review stops (superseded),
# and the newer one's review gets the workspace.
bind_prepared("review-w0", target(CHART, 51, "a" * 40))
bind("review-w1", target(CHART, 52, "1" * 40))
w = {}
w1 = in_thread(lambda: w.update(w1=prepare("review-w1")))
time.sleep(2)
bind("review-w2", target(CHART, 52, "2" * 40))
w1.join(15)
outcomes["w1"] = w.get("w1", "waiting")
w2 = in_thread(lambda: w.update(w2=prepare("review-w2")))
time.sleep(2)
end("review-w0")
w2.join(60)
outcomes["w2"] = w.get("w2", "waiting")
end("review-w1")
end("review-w2")

# (x) waiting reviews get the workspace in the order they asked.
bind_prepared("review-x0", target(CHART, 53, "a" * 40))
bind("review-x1", target(CHART, 54, "1" * 40))
bind("review-x2", target(CHART, 55, "2" * 40))
x = {}
x1 = in_thread(lambda: x.update(x1=prepare("review-x1")))
time.sleep(1)
x2 = in_thread(lambda: x.update(x2=prepare("review-x2")))
time.sleep(1)
end("review-x0")
x1.join(60)
x2_waited = x2.is_alive()
end("review-x1")
x2.join(60)
outcomes["x"] = ("in-order" if x.get("x1") == "prepared" and x2_waited and x.get("x2") == "prepared"
                 else f"{x} x2_waited={x2_waited}")
end("review-x2")

# (y) a posted review leaves the queue.
bind_prepared("review-y", target(CHART, 56, "a" * 40))
outcomes["y"] = f"{post('review-y', review)}:{queue_state(CHART, 56)}"
end("review-y")

# (z) the review-queue cron job: its sessions take the oldest free entries (one whose session's
# process is gone, then one with no session), never one at MAX_ATTEMPTS or for an unlisted
# repository, and get its REVIEW-TARGET line. A cron session without the job's marker takes none.
# The wake script skips the model once no entry is free.
WORKER = f"{plugin.WORKER_MARKER}: review the next pull request in the review queue."
plugin._write_queue({
    f"{CHART}#60": {"sha": "1" * 40, "queued": 1, "attempts": 1, "owner": {"pid": 999999, "start": "1", "session": "gone"}},
    f"{CHART}#61": {"sha": "2" * 40, "queued": 2, "attempts": 0, "owner": None},
    f"{CHART}#62": {"sha": "3" * 40, "queued": 0.5, "attempts": 3, "owner": None},
    "openmrs/openmrs-module-fhir2#63": {"sha": "4" * 40, "queued": 0.1, "attempts": 0, "owner": None},
})
outcomes["z-gate"] = wake_gate().split(":")[0]
outcomes["z0"] = (bind("review-z0", "Some other cron job.", platform="cron") or "none") + ":" + prepare("review-z0")
outcomes["z1"] = "pr60" if target(CHART, 60, "1" * 40) in bind("review-z1", WORKER, platform="cron") else "other"
outcomes["z2"] = "pr61" if target(CHART, 61, "2" * 40) in bind("review-z2", WORKER, platform="cron") else "other"
outcomes["z3"] = "empty" if "no pull request for you" in bind("review-z3", WORKER, platform="cron") else "other"
outcomes["z3-prepare"] = prepare("review-z3")
# A review-queue session with no pull request can't use the workspace's tools.
outcomes["z3-terminal"] = terminal_as("review-z3", "echo ran")
outcomes["z-unlisted"] = "dropped" if entry("openmrs/openmrs-module-fhir2", 63) is None else "kept"
outcomes["z-gate-after"] = wake_gate().split(":")[0]
outcomes["z1-prepare"] = prepare("review-z1")
end("review-z1", platform="cron")
outcomes["z1-end"] = queue_state(CHART, 60)
end("review-z2", platform="cron")
end("review-z3", platform="cron")

# (aa) an entry's third session ends without a review: the entry is dropped.
plugin._write_queue({f"{CHART}#64": {"sha": "5" * 40, "queued": 1, "attempts": 2, "owner": None}})
bind("review-aa", WORKER, platform="cron")
outcomes["aa"] = queue_state(CHART, 64)
end("review-aa", platform="cron")
outcomes["aa-end"] = queue_state(CHART, 64)

# (cc) an entry whose session's process is alive but hasn't stamped it for OWNER_STALE_SECONDS (a
# run that failed without on_session_end) is free: the wake script wakes, and a session takes it.
plugin._write_queue({f"{CHART}#65": {"sha": "6" * 40, "queued": 1, "attempts": 1, "owner": {
    **plugin._PROCESS, "session": "failed-run", "beat": time.time() - plugin.OWNER_STALE_SECONDS - 5}}})
outcomes["cc-gate"] = wake_gate().split(":")[0]
outcomes["cc"] = "pr65" if target(CHART, 65, "6" * 40) in bind("review-cc", WORKER, platform="cron") else "other"
end("review-cc", platform="cron")
# (dd) a live session's heartbeat stamps its entry again, so it stays taken.
plugin._write_queue({})
bind("review-dd", target(CHART, 66, "7" * 40))
queue = plugin._read_queue()
queue[f"{CHART}#66"]["owner"]["beat"] = time.time() - plugin.OWNER_STALE_SECONDS - 5
plugin._write_queue(queue)
outcomes["dd-stale"] = wake_gate().split(":")[0]
beat_write = plugin.BEAT_WRITE_SECONDS
plugin.BEAT_WRITE_SECONDS = 0
heartbeat("review-dd")
plugin.BEAT_WRITE_SECONDS = beat_write
outcomes["dd"] = wake_gate().split(":")[0]
end("review-dd")
# (ee) the wake script doesn't count an entry for a commit already reviewed (posted.json has (a)).
plugin._write_queue({f"{CHART}#11": {"sha": "a" * 40, "queued": 1, "attempts": 0, "owner": None}})
outcomes["ee"] = wake_gate().split(":")[0]
# (ff) a moved head that is already queued keeps its entry, and the session that has it.
plugin._write_queue({f"{CHART}#41": {"sha": "f" * 40, "queued": 1, "attempts": 1, "owner": plugin._owner("ff-live")}})
with plugin._lock:
    plugin._requeue(plugin.Binding(CHART, 41, "c" * 40), "f" * 40)
outcomes["ff"] = queue_state(CHART, 41)
plugin._write_queue({})

# (bb) GitHub's answer, from fake-github with the github_review token: a closed pull request leaves
# the queue; a moved head stops this review and queues the new head; an open one at the bound commit
# is prepared.
plugin._pull_request = real_pull_request
bind("review-bb1", target(CHART, 40, "c" * 40))
outcomes["bb1"] = f"{prepare('review-bb1')}:{queue_state(CHART, 40)}"
end("review-bb1")
bind("review-bb2", target(CHART, 41, "c" * 40))
outcomes["bb2"] = f"{prepare('review-bb2')}:{(entry(CHART, 41) or {}).get('sha', '')[:1]}:{queue_state(CHART, 41)}"
end("review-bb2")
bind("review-bb3", target(CHART, 42, "c" * 40))
outcomes["bb3"] = prepare("review-bb3")
end("review-bb3")
# A draft leaves the queue; an answer that isn't JSON stops the review but keeps the entry.
bind("review-bb4", target(CHART, 43, "c" * 40))
outcomes["bb4"] = f"{prepare('review-bb4')}:{queue_state(CHART, 43)}"
end("review-bb4")
bind("review-bb5", target(CHART, 44, "c" * 40))
outcomes["bb5"] = f"{prepare('review-bb5')}:{queue_state(CHART, 44)}"
end("review-bb5")

print(" ".join(f"{case}={outcome}" for case, outcome in outcomes.items()))
