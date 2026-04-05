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
    Each provider translates between the normalized CodeLM format
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

    def assistant_message(self, response: LLMResponse) -> dict:
        """Build an assistant history entry (in Anthropic-canonical format) from a response.

        Providers override this to preserve tool call structure in the message history.
        Default: plain text only (loses tool call info — always override when tools are used).
        """
        return {"role": "assistant", "content": response.reply or ""}
