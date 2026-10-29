"""Derives the LiteLLM config used by tests/run.sh from litellm/config.yaml.

Usage: python3 tests/make_litellm_config.py <litellm/config.yaml> <output.yaml>

The real config is kept as-is apart from five test changes:
  - fast / balanced / strong get a mock_response naming the model, so a reply shows which tier
    model the `auto` router chose, and no provider is called.
  - `classifier-mock` (always classifies as REASONING) and `classifier-mock-invalid` (returns
    text that isn't a classification) are added for the LLM classifier.
  - the Rovo MCP URLs point at tests/fake_rovo.py, so no requests go to Atlassian.
  - each tier entry is copied, unmocked, as `cache-check-<tier>` pointing at
    tests/fake_anthropic.py, so the test can see the request LiteLLM actually sends upstream
    (e.g. the prompt-cache markers).
  - prompts are stored in the spend logs, so the test can check what reached the model (e.g.
    SOUL.md).
"""

import sys

TIER_MODELS = ("FAST", "BALANCED", "STRONG")
ROVO_URL = "url: https://mcp.atlassian.com/v2/mcp"
FAKE_ANTHROPIC = "http://fake-anthropic:8003"
GENERAL_SETTINGS = "general_settings:\n"

CLASSIFIER_MOCK = """model_list:
  - model_name: classifier-mock
    litellm_params:
      model: openai/classifier-mock
      api_key: test-classifier-key
      mock_response: '{"tier": "REASONING"}'
  - model_name: classifier-mock-invalid
    litellm_params:
      model: openai/classifier-mock-invalid
      api_key: test-classifier-key
      mock_response: "not a classification"
"""


def model_entry(config: str, name: str) -> str:
    """The model_list entry for `name`, up to the next entry or top-level key."""
    start = config.index(f"  - model_name: {name}\n")
    end = start + 1
    while True:
        end = config.index("\n", end) + 1
        if not config[end:].startswith("   "):
            return config[start:end]


def main(source: str, dest: str) -> None:
    with open(source) as f:
        config = f.read()

    cache_checks = ""
    for name in TIER_MODELS:
        line = f"model: os.environ/LLM_MODEL_{name}\n"
        if line not in config:
            sys.exit(f"{source}: expected a model_list entry with '{line.strip()}'")
        tier = name.lower()
        cache_checks += (
            model_entry(config, tier)
            .replace(f"model_name: {tier}\n", f"model_name: cache-check-{tier}\n")
            .replace(line, f"{line}      api_base: {FAKE_ANTHROPIC}\n")
        )
        config = config.replace(line, f'{line}      mock_response: "{tier}-model-answered"\n')

    if config.count("model_list:\n") != 1:
        sys.exit(f"{source}: expected exactly one 'model_list:' line")
    config = config.replace("model_list:\n", CLASSIFIER_MOCK + cache_checks, 1)

    if ROVO_URL not in config:
        sys.exit(f"{source}: expected Rovo MCP servers with '{ROVO_URL}'")
    config = config.replace(ROVO_URL, "url: http://fake-rovo:8002/mcp")

    if config.count(GENERAL_SETTINGS) != 1:
        sys.exit(f"{source}: expected exactly one 'general_settings:' line")
    config = config.replace(GENERAL_SETTINGS, GENERAL_SETTINGS + "  store_prompts_in_spend_logs: true\n", 1)

    with open(dest, "w") as f:
        f.write(config)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
