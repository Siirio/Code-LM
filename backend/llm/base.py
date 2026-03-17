from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    reply: str                          # Final text reply (empty if stop_reason == "tool_use")
    stop_reason: str                    # "end_turn" | "tool_use"
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: object = None                  # Original provider response (for debugging)


class LLMProvider(ABC):
    """
    Abstract base for all LLM providers.
    Each provider translates between the normalized EngramAI format
    and its own API format.
    """

    @abstractmethod
    def chat(
        self,
        messages: list[dict],   # [{"role": "user"|"assistant", "content": str|list}]
        tools: list[dict],       # Anthropic-style tool schemas (we use this as canonical)
        system: str,
    ) -> LLMResponse:
        """Send a chat turn and return the response."""
        ...
