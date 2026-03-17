import json
import logging
import openai
from llm.base import LLMProvider, LLMResponse, ToolCall

logger = logging.getLogger(__name__)


def _anthropic_tools_to_openai(tools: list[dict]) -> list:
    """
    Convert Anthropic-style tool definitions to OpenAI function-calling format.

    Anthropic: {"name": ..., "description": ..., "input_schema": {"type": "object", "properties": {...}}}
    OpenAI:    {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    """
    result = []
    for tool in tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            }
        })
    return result


def _normalize_messages(messages: list[dict], system: str) -> list[dict]:
    """
    Convert normalized EngramAI message history to OpenAI format.
    OpenAI puts the system prompt as the first message in the array.
    Tool results use role="tool" with tool_call_id.
    """
    result = [{"role": "system", "content": system}]

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            result.append({"role": role, "content": content})

        elif isinstance(content, list):
            # Could be tool results (role=user) or assistant content with tool_use blocks
            if role == "user":
                # Tool results
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block.get("content", ""),
                        })
                    elif isinstance(block, dict) and block.get("type") == "text":
                        result.append({"role": "user", "content": block["text"]})
            elif role == "assistant":
                # Assistant message — may include tool_calls stored from a previous turn
                tool_calls = [
                    b for b in content
                    if isinstance(b, dict) and b.get("type") == "openai_tool_call"
                ]
                text_parts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                assistant_msg: dict = {"role": "assistant", "content": " ".join(text_parts) or None}
                if tool_calls:
                    assistant_msg["tool_calls"] = [tc["raw"] for tc in tool_calls]
                result.append(assistant_msg)

    return result


class OpenAIProvider(LLMProvider):
    """OpenAI GPT via openai SDK."""

    def __init__(self, api_key: str, model: str):
        # Guard: an empty key produces "Authorization: Bearer " which OpenAI
        # rejects with 401. Surface the misconfiguration immediately instead.
        if not api_key:
            raise ValueError(
                "OpenAI API key is empty. Set OPENAI_API_KEY in your .env file."
            )
        logger.debug("OpenAIProvider: api_key prefix=%s, model=%s", api_key[:8], model)
        self.client = openai.OpenAI(api_key=api_key)
        self.model = model

    def chat(self, messages: list[dict], tools: list[dict], system: str) -> LLMResponse:
        openai_messages = _normalize_messages(messages, system)
        openai_tools = _anthropic_tools_to_openai(tools)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=openai_messages,
            tools=openai_tools,
            tool_choice="auto",
        )

        choice = response.choices[0]
        message = choice.message

        # Extract tool calls
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    input=args,
                ))

        reply = message.content or ""
        stop_reason = "tool_use" if tool_calls else "end_turn"

        # Store raw tool_calls in a format the orchestrator can put back into history
        raw_content = []
        if reply:
            raw_content.append({"type": "text", "text": reply})
        for tc in (message.tool_calls or []):
            raw_content.append({"type": "openai_tool_call", "raw": tc.model_dump()})

        return LLMResponse(
            reply=reply,
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            raw=raw_content,  # used by orchestrator to append assistant turn to history
        )
