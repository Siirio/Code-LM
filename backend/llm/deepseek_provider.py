import json

from openai import OpenAI

from llm.base import LLMProvider, LLMResponse, ToolCall

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-style tool schemas to OpenAI function-calling format."""
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return result


def anthropic_messages_to_openai(messages: list[dict]) -> list[dict]:
    """Convert Anthropic-format message history to OpenAI/DeepSeek format.

    Handles:
    - Plain string content → passed through
    - Anthropic tool_use assistant messages → OpenAI tool_calls format
    - Anthropic tool_result user messages → OpenAI tool role messages
    - Mixed content lists → text extracted; tool_results become separate tool messages
    """
    result = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            result.append({"role": role, "content": str(content)})
            continue

        text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
        tool_use_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        tool_result_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]

        if role == "assistant" and tool_use_blocks:
            tool_calls = []
            for b in tool_use_blocks:
                tool_input = b.get("input", {})
                if isinstance(tool_input, dict):
                    args_str = json.dumps(tool_input)
                elif isinstance(tool_input, str):
                    args_str = tool_input or "{}"
                else:
                    args_str = json.dumps(tool_input)
                tool_calls.append({
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": args_str},
                })
            oai_msg: dict = {"role": "assistant", "tool_calls": tool_calls}
            if text_blocks:
                oai_msg["content"] = text_blocks[0].get("text", "")
            result.append(oai_msg)

        elif role == "user" and tool_result_blocks:
            for b in tool_result_blocks:
                content_val = b.get("content", "")
                if isinstance(content_val, list):
                    # Anthropic prompt-caching format: list of typed blocks
                    content_val = "\n".join(
                        x.get("text", "") for x in content_val
                        if isinstance(x, dict) and x.get("type") == "text"
                    )
                result.append({
                    "role": "tool",
                    "tool_call_id": b["tool_use_id"],
                    "content": str(content_val),
                })
            # Any plain text blocks in the same user turn become a follow-up user message
            for b in text_blocks:
                text = b.get("text", "")
                if text:
                    result.append({"role": "user", "content": text})

        else:
            text = "\n".join(b.get("text", "") for b in text_blocks)
            if text:
                result.append({"role": role, "content": text})
            elif content:
                result.append({"role": role, "content": str(content)})

    return result


class DeepSeekProvider(LLMProvider):
    """DeepSeek via OpenAI-compatible API."""

    def __init__(self, api_key: str, model: str):
        self.client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        self.model = model

    def chat(self, messages: list[dict], tools: list[dict], system: str) -> LLMResponse:
        oai_messages = [{"role": "system", "content": system}] + anthropic_messages_to_openai(messages)
        oai_tools = anthropic_tools_to_openai(tools)

        kwargs: dict = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": oai_messages,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools

        response = self.client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    input_dict = json.loads(tc.function.arguments)
                except Exception:
                    input_dict = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, input=input_dict))

        if tool_calls:
            stop_reason = "tool_use"
        elif choice.finish_reason == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"

        return LLMResponse(
            reply=msg.content or "",
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            raw=response,
        )

    def assistant_message(self, response: LLMResponse) -> dict:
        """Build an Anthropic-format assistant history entry from a DeepSeek response."""
        content: list[dict] = []
        msg = response.raw.choices[0].message
        if msg.content:
            content.append({"type": "text", "text": msg.content})
        for tc in (msg.tool_calls or []):
            try:
                input_dict = json.loads(tc.function.arguments)
            except Exception:
                input_dict = {}
            content.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.function.name,
                "input": input_dict,
            })
        return {"role": "assistant", "content": content}
