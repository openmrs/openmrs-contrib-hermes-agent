"""Checks for litellm/tier_classifier.py that need LiteLLM itself, used by tests/run.sh.

Runs in the litellm container (tier_classifier.py is at /app) against Routers built here, not the
proxy's:
  - the LLM classifier refuses a router model (`auto`), an alias to one, and an unknown model;
  - Jev is skipped without a `jev-router` model or a TypeSafe key, and otherwise calls
    `jev-router` through the Router;
  - delegates are rebuilt when the proxy replaces its Router, and reused otherwise;
  - classify() declines when re-entered from its own classifier call.
"""

import asyncio
import os
import sys

sys.path.insert(0, "/app")

import tier_classifier as tc  # noqa: E402
from litellm import Router  # noqa: E402
from litellm.proxy import proxy_server  # noqa: E402
from litellm.router_strategy.complexity_router.jev_classifier import RouterJevClassifierClient  # noqa: E402
from litellm.types.router import RoutingContext  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}: {name}{'' if ok else f' ({detail})'}")
    if not ok:
        failures.append(name)


def make_router(jev_params=None):
    tiers = {"SIMPLE": "fast", "MEDIUM": "fast", "COMPLEX": "fast", "REASONING": "fast"}
    jev = [{"model_name": "jev-router", "litellm_params": jev_params}] if jev_params is not None else []
    return Router(
        model_list=jev + [
            {"model_name": "fast", "litellm_params": {"model": "openai/fast", "api_key": "x", "mock_response": "ok"}},
            {
                "model_name": "auto",
                "litellm_params": {"model": "auto_router/complexity_router", "complexity_router_config": {"tiers": tiers}},
            },
        ],
        model_group_alias={"smart": "auto", "quick": "fast"},
    )


router = make_router()

# Startup-time refusal of models that would route back into the classifier.
check("plain model accepted", tc._router_model_problem(router, "fast") is None, tc._router_model_problem(router, "fast"))
check("alias to plain model accepted", tc._router_model_problem(router, "quick") is None,
      tc._router_model_problem(router, "quick"))
check("router model `auto` refused", "router" in (tc._router_model_problem(router, "auto") or ""),
      tc._router_model_problem(router, "auto"))
check("alias to a router refused", "router" in (tc._router_model_problem(router, "smart") or ""),
      tc._router_model_problem(router, "smart"))
check("unknown model refused", "not in" in (tc._router_model_problem(router, "nope") or ""),
      tc._router_model_problem(router, "nope"))

proxy_server.llm_router = router
refused = tc.ChainedTierClassifier(("llm",), "auto")
check("ROUTER_CLASSIFIER_MODEL=auto: llm classifier skipped", refused._current_delegates() == (),
      refused._current_delegates())

# The Jev classifier needs the jev-router model and a TypeSafe key, from the model or the environment.
def jev_delegates(router, env_key):
    saved = os.environ.pop("TYPESAFE_API_KEY", None)
    if env_key is not None:
        os.environ["TYPESAFE_API_KEY"] = env_key
    try:
        proxy_server.llm_router = router
        return tc.ChainedTierClassifier(("jev",), "fast")._current_delegates()
    finally:
        os.environ.pop("TYPESAFE_API_KEY", None)
        if saved is not None:
            os.environ["TYPESAFE_API_KEY"] = saved


check("no jev-router model: jev classifier skipped", jev_delegates(router, "k") == ())
keyless = make_router({"model": "typesafe/jev-latest"})
check("no TypeSafe key: jev classifier skipped", jev_delegates(keyless, None) == ())
for name, jev_router, env_key in (
    ("key from TYPESAFE_API_KEY", keyless, "k"),
    ("key on the model", make_router({"model": "typesafe/jev-latest", "api_key": "k"}), None),
):
    delegates = jev_delegates(jev_router, env_key)
    client = delegates[0][1]._jev_client if len(delegates) == 1 else None
    check(f"{name}: jev classifier calls jev-router through the Router",
          isinstance(client, RouterJevClassifierClient) and client._deployment_name == "jev-router", delegates)
proxy_server.llm_router = router

# Delegates follow the proxy's current Router.
chain = tc.ChainedTierClassifier(("llm",), "fast")
first = chain._current_delegates()
check("delegates built on first use", len(first) == 1 and first[0][1].litellm_router_instance is router, first)
check("delegates reused while the Router is unchanged", chain._current_delegates() is first)
replacement = make_router()
proxy_server.llm_router = replacement
second = chain._current_delegates()
check("delegates rebuilt when the proxy replaces its Router",
      second is not first and second[0][1].litellm_router_instance is replacement, second)

# Runtime guard against re-entry.
context = RoutingContext(raw_messages=[], structured_messages=[{"role": "user", "content": "hi"}], candidate_models=[])
calls = []


async def fake_classify(ctx):
    calls.append(tc._classifying.get())
    return "SIMPLE"


chain._classify = fake_classify
result = asyncio.run(chain.classify(context))
check("classify() runs the chain and marks itself active", result == "SIMPLE" and calls == [True], (result, calls))
check("active marker is cleared afterwards", tc._classifying.get() is False)

calls.clear()
token = tc._classifying.set(True)
try:
    nested = asyncio.run(chain.classify(context))
finally:
    tc._classifying.reset(token)
check("re-entered classify() declines without running the chain", nested is None and calls == [], (nested, calls))

sys.exit(1 if failures else 0)
