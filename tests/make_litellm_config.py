"""Derives tests/run.sh's LiteLLM config from litellm/config.yaml.

Usage: python3 tests/make_litellm_config.py <litellm/config.yaml> <output.yaml> [<upstream.yaml>]

Changes from the real config:
  - fast / balanced / strong get a mock_response naming the tier, so a reply shows which model
    `auto` chose and no provider is called.
  - `classifier-mock` (always REASONING) and `classifier-mock-invalid` (not a classification)
    are added for the LLM classifier.
  - atlassian_* MCP servers point at tests/fake_atlassian.py instead of mcp-atlassian.
  - onyx_search points at tests/fake_onyx.py instead of onyx-mcp.
  - each tier is copied, unmocked, as `cache-check-<tier>` against tests/fake_anthropic.py, to see
    what LiteLLM sends upstream.
  - prompts are stored in spend logs, so the test can check what reached the model.

<upstream.yaml>, if given, is the same except that fast / balanced / strong are not mocked but sent
to tests/fake_anthropic.py, so a test can see what an agent sends to its model.
"""

import sys

TIER_MODELS = ("FAST", "BALANCED", "STRONG")
ATLASSIAN_URL = "url: http://mcp-atlassian:9000/mcp"
ONYX_URL = "url: http://onyx-mcp:8090/"
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


def main(source: str, dest: str, upstream_dest: str = "") -> None:
    with open(source) as f:
        source_config = f.read()
    write(source_config, dest, source, upstream=False)
    if upstream_dest:
        write(source_config, upstream_dest, source, upstream=True)


def write(config: str, dest: str, source: str, upstream: bool) -> None:
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
        if upstream:
            config = config.replace(line, f"{line}      api_base: {FAKE_ANTHROPIC}\n")
        else:
            config = config.replace(line, f'{line}      mock_response: "{tier}-model-answered"\n')

    if config.count("model_list:\n") != 1:
        sys.exit(f"{source}: expected exactly one 'model_list:' line")
    config = config.replace("model_list:\n", CLASSIFIER_MOCK + cache_checks, 1)

    if ATLASSIAN_URL not in config:
        sys.exit(f"{source}: expected Atlassian MCP servers with '{ATLASSIAN_URL}'")
    config = config.replace(ATLASSIAN_URL, "url: http://fake-atlassian:8002/mcp")

    if config.count(ONYX_URL) != 1:
        sys.exit(f"{source}: expected an onyx_search MCP server with '{ONYX_URL}'")
    config = config.replace(ONYX_URL, "url: http://fake-onyx:8006/")

    if config.count(GENERAL_SETTINGS) != 1:
        sys.exit(f"{source}: expected exactly one 'general_settings:' line")
    config = config.replace(GENERAL_SETTINGS, GENERAL_SETTINGS + "  store_prompts_in_spend_logs: true\n", 1)

    with open(dest, "w") as f:
        f.write(config)


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    main(*sys.argv[1:])
