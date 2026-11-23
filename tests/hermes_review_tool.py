"""Calls the github-review plugin's pre_llm_call hook and post_pr_review tool the way Hermes does,
used by tests/run.sh.

Runs in agent-reviewer (on stdin, as the Hermes user). Loads the plugin through Hermes' own plugin
discovery, so it also checks that the managed config (/etc/hermes/config.yaml) enables it. Each case
uses its own session id; posts go to tests/fake_github.py, whose log run.sh checks. Prints one
summary line, `<case>=<outcome>` for each case: "posted", "refused", "github-422" (GitHub's
message for a line outside the diff came back), or "unknown" (the tool can't tell whether GitHub
created the review).

The tool needs the agent's LiteLLM key for github-token, which Hermes reads from the profile's
secret scope; a bare `compose exec` has no scope and no LITELLM_API_KEY, so the scope is loaded here
from the agent's .env first.
"""

import json

from agent.secret_scope import build_profile_secret_scope, set_secret_scope
from hermes_cli.plugins import discover_plugins, get_plugin_manager, invoke_hook
from hermes_constants import get_hermes_home
from model_tools import handle_function_call

set_secret_scope(build_profile_secret_scope(get_hermes_home()), profile_home=str(get_hermes_home()))
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


def bind_prepared(session, message, **kwargs):
    bind(session, message, **kwargs)
    plugin._test_mark_prepared(session)


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

# (a) bound and prepared: posts. (b) a second post in the same session: refused.
bind_prepared("review-a", f"{target(CHART, 11, 'a' * 40)}\nReview this pull request.")
outcomes["a"] = post("review-a", review)
outcomes["b"] = post("review-a", review)
# (c) bound but not prepared.
bind("review-c", target(CHART, 12, "c" * 40))
outcomes["c"] = post("review-c", review)
# (d) no binding: no REVIEW-TARGET line, a repository outside the list, another platform, a later
# turn, two REVIEW-TARGET lines, a malformed line.
bind_prepared("review-d1", "Review openmrs/openmrs-esm-patient-chart#12, please.")
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
# (e) a new session bound to the PR and commit (a) already reviewed.
bind_prepared("review-e", target(CHART, 11, "a" * 40))
outcomes["e"] = post("review-e", review)
# (f) repo, pr_number, commit_id, and event in the parameters are ignored.
bind_prepared("review-f", target("openmrs/openmrs-core", 13, "f" * 40))
outcomes["f"] = post("review-f", {"body": "Other PR?", "comments": COMMENTS[:1], "repo": CHART, "pr_number": 99,
                                   "commit_id": "0" * 40, "event": "APPROVE"})
# (g) GitHub refuses a line outside the diff: the model gets GitHub's message and can post again.
bind_prepared("review-g", target("openmrs/openmrs-esm-patient-management", 14, "1" * 40))
outcomes["g1"] = post("review-g", {"body": "x", "comments": [{"path": "not-in-diff.txt", "line": 1, "body": "y"}]})
outcomes["g2"] = post("review-g", review)
# (h) GitHub answers 500: the outcome is unknown, so neither a retry nor a new session for the same
# commit (a redelivered webhook) may post.
bind_prepared("review-h", target(CHART, 15, "2" * 40))
outcomes["h1"] = post("review-h", review)
outcomes["h2"] = post("review-h", review)
bind_prepared("review-h3", target(CHART, 15, "2" * 40))
outcomes["h3"] = post("review-h3", review)
# (i) GitHub answers 200 with a body that isn't JSON: posted, and recorded like any other post.
bind_prepared("review-i", target(CHART, 16, "3" * 40))
outcomes["i1"] = post("review-i", review)
outcomes["i2"] = post("review-i", review)
bind_prepared("review-i3", target(CHART, 16, "3" * 40))
outcomes["i3"] = post("review-i3", review)

print(" ".join(f"{case}={outcome}" for case, outcome in outcomes.items()))
