"""Qwen Flash request configuration with thinking enabled."""
import json
from pathlib import Path

MODEL = 'qwen3.8-flash'
ENDPOINT = 'https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1'
PROVIDER = 'qwen-token-plan'
REASONING_EFFORT = 'high'
ENABLE_THINKING = True


def harness_options(directory, budget, key, *, endpoint=ENDPOINT):
    """Keep the minimal bash-only profile and use native Qwen wire settings.

    pi-ai's Qwen compatibility maps effort=off to enable_thinking=false.
    Merely sending DeepSeek's thinking.type=disabled is insufficient for Qwen.
    The endpoint override is used only by the local wire-contract test.
    """
    directory = Path(directory)
    route = {
        'apiKeyEnv': 'QWEN_TOKEN_PLAN_API_KEY',
        'api': 'openai-completions',
        'baseURL': endpoint,
        'reasoning': REASONING_EFFORT,
        'compat': {'thinkingFormat': 'qwen', 'supportsReasoningEffort': False,
                   'maxTokensField': 'max_tokens'},
        'models': [{'id': MODEL, 'contextWindow': 1000000, 'maxTokens': 65536,
                    'reasoningEfforts': {'off': None, 'high': 'high'}}],
    }
    patch = directory / 'qwen-provider.patch.yml'
    patch.write_text(json.dumps([
        {'id': 'llm-deepseek', 'disabled': True},
        {'insert': [{'id': 'qwen-pi-ai', 'name': '@deepseek-ai/dsh-llm-pi-ai',
                     'config': {'providers': {PROVIDER: route}}}]},
    ], indent=2))
    return {
        'model': MODEL, 'provider': PROVIDER, 'reasoning_effort': REASONING_EFFORT,
        'max_tokens': budget['max_tokens_per_request'], 'profile': 'sdk-minimal',
        'dsh_home': str(directory / 'home'), 'cwd': str(directory),
        'patches': (str(directory / 'native.patch.yml'), str(patch)),
        'env': {'QWEN_TOKEN_PLAN_API_KEY': key},
        'initialize_timeout_seconds': 300,
        'request_timeout_seconds': budget['agent_sdk_request_timeout_seconds'],
    }
