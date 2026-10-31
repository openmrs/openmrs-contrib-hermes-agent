"""Checks that every Hermes side task (auxiliary task) routes to LiteLLM's `fast` model.

Runs in the Hermes image with HERMES_HOME holding hermes/config.yaml. Tasks come from Hermes'
defaults, plugin registrations, and literal `task="..."` arguments in its source.
"""

import pathlib
import re
import sys

from agent.auxiliary_client import _resolve_task_provider_model, resolve_provider_client
from hermes_cli.config import DEFAULT_CONFIG

EXPECTED_PROVIDER = "litellm"
EXPECTED_MODEL = "fast"
EXPECTED_BASE_URL = "http://litellm:4000/v1"
# Sentinels passed as task names that are not real tasks.
NOT_TASKS = {"__reset__"}

tasks = {k for k, v in DEFAULT_CONFIG["auxiliary"].items() if isinstance(v, dict) and "provider" in v}

try:
    from hermes_cli.plugins import discover_plugins, get_plugin_auxiliary_tasks

    discover_plugins()
    tasks |= {entry["key"] for entry in get_plugin_auxiliary_tasks()}
except Exception as e:  # noqa: BLE001 -- report and keep checking the other sources
    print(f"WARN: plugin task discovery failed: {e}")

literal = re.compile(r"""\btask=["']([a-z_]+)["']""")
for path in pathlib.Path("/opt/hermes").rglob("*.py"):
    parts = set(path.parts)
    if ".venv" in parts or "tests" in parts or "node_modules" in parts:
        continue
    tasks |= set(literal.findall(path.read_text(errors="ignore")))
tasks -= NOT_TASKS

failures = []
for task in sorted(tasks):
    provider, model, *_ = _resolve_task_provider_model(task)
    if (provider, model) != (EXPECTED_PROVIDER, EXPECTED_MODEL):
        failures.append(f"{task}: routed to provider={provider!r} model={model!r}")

client, _ = resolve_provider_client(EXPECTED_PROVIDER, EXPECTED_MODEL)
base_url = str(getattr(client, "base_url", "")).rstrip("/")
if base_url != EXPECTED_BASE_URL:
    failures.append(f"provider {EXPECTED_PROVIDER!r} resolves to base_url {base_url!r}, expected {EXPECTED_BASE_URL!r}")

# Some tasks build a full runtime with resolve_runtime_provider instead, silently falling back to
# the main model (`auto`) if it fails, so check them through their own entry points.
from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: E402

runtime = resolve_runtime_provider(requested=EXPECTED_PROVIDER, target_model=EXPECTED_MODEL)
if str(runtime.get("base_url") or "").rstrip("/") != EXPECTED_BASE_URL:
    failures.append(f"resolve_runtime_provider({EXPECTED_PROVIDER!r}) base_url is {runtime.get('base_url')!r}")


class _MainAgent:
    """The parts of an AIAgent that background review reads: the main route is `auto`."""

    provider = EXPECTED_PROVIDER
    model = "auto"

    def _current_main_runtime(self):
        return {"base_url": EXPECTED_BASE_URL, "api_key": "sk-placeholder", "api_mode": "chat_completions"}


from agent.background_review import _resolve_review_runtime as resolve_background_review  # noqa: E402

review = resolve_background_review(_MainAgent())
if not (review.get("routed") and review.get("model") == EXPECTED_MODEL
        and str(review.get("base_url") or "").rstrip("/") == EXPECTED_BASE_URL):
    failures.append(f"background_review runtime: routed={review.get('routed')!r} model={review.get('model')!r} "
                    f"base_url={review.get('base_url')!r}")

from agent.curator import _resolve_review_provider as resolve_curator  # noqa: E402

curator_runtime, curator_model, _, _ = resolve_curator()
if not (curator_model == EXPECTED_MODEL and str(curator_runtime.get("base_url") or "").rstrip("/") == EXPECTED_BASE_URL):
    failures.append(f"curator runtime: model={curator_model!r} base_url={curator_runtime.get('base_url')!r}")

print(f"checked {len(tasks)} side tasks: {' '.join(sorted(tasks))}")
for failure in failures:
    print(f"FAIL: {failure}")
sys.exit(1 if failures else 0)
