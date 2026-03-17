import anthropic
from llm.base import LLMProvider, LLMResponse, ToolCall


class AnthropicProvider(LLMProvider):
    """Claude via Anthropic API."""

    def __init__(self, api_key: str, model: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def chat(self, messages: list[dict], tools: list[dict], system: str) -> LLMResponse:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            tools=tools,
            messages=messages,
        )

        tool_calls = [
            ToolCall(id=block.id, name=block.name, input=block.input)
            for block in response.content
            if block.type == "tool_use"
        ]

        reply = next(
            (block.text for block in response.content if block.type == "text"),
            ""
        )

        # Use the stop_reason reported by the API directly.
        # Previously this was inferred from tool_calls alone, which masked
        # other stop reasons (max_tokens, stop_sequence, etc.) and could
        # misclassify a tool_use response that also contained a text block.
        stop_reason = response.stop_reason or "end_turn"

        return LLMResponse(
            reply=reply,
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            raw=response,
        )

    def build_tool_result_message(self, tool_call_id: str, content: str) -> dict:
        return {
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": content,
        }

    def assistant_message_from_raw(self, raw_response) -> dict:
        """Convert a raw Anthropic response to a history message."""
        return {"role": "assistant", "content": raw_response.content}
