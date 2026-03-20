from llm.base import LLMProvider


def get_provider(provider_name: str, model: str, api_key: str) -> LLMProvider:
    """
    Return the correct LLMProvider instance.

    provider_name: "anthropic" | "gemini"
    model:         e.g. "claude-sonnet-4-6" | "gemini-2.0-flash"
    api_key:       the corresponding API key
    """
    name = provider_name.lower().strip()

    if name == "anthropic":
        from llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(api_key=api_key, model=model)

    if name == "gemini":
        from llm.gemini_provider import GeminiProvider
        return GeminiProvider(api_key=api_key, model=model)

    if name == "openai":
        from llm.openai_provider import OpenAIProvider
        return OpenAIProvider(api_key=api_key, model=model)

    raise ValueError(
        f"Unknown LLM provider: '{provider_name}'. "
        f"Supported: 'anthropic', 'gemini', 'openai'"
    )
