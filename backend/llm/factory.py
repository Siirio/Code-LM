from llm.base import LLMProvider


def get_provider(provider_name: str, model: str, api_key: str) -> LLMProvider:
    """
    Return the correct LLMProvider instance.

    provider_name: "anthropic"
    model:         e.g. "claude-sonnet-4-6"
    api_key:       the corresponding API key
    """
    name = provider_name.lower().strip()

    if name == "anthropic":
        from llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(api_key=api_key, model=model)

    raise ValueError(
        f"Unknown LLM provider: '{provider_name}'. "
        f"Supported: 'anthropic'"
    )
