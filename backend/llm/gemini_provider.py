import json
import google.generativeai as genai
import google.api_core.exceptions
from llm.base import LLMProvider, LLMResponse, ToolCall


def _anthropic_schema_to_gemini(tools: list[dict]) -> list:
    """
    Convert Anthropic-style tool definitions to Gemini FunctionDeclaration format.

    Anthropic:  {"name": ..., "description": ..., "input_schema": {"type": "object", "properties": {...}}}
    Gemini:     FunctionDeclaration(name=..., description=..., parameters={...})
    """
    from google.generativeai.types import FunctionDeclaration, Tool as GeminiTool

    def _convert_type(t: str) -> str:
        return {"string": "STRING", "integer": "INTEGER", "number": "NUMBER",
                "boolean": "BOOLEAN", "array": "ARRAY", "object": "OBJECT"}.get(t.lower(), "STRING")

    def _convert_properties(props: dict) -> dict:
        result = {}
        for name, schema in props.items():
            converted = {"type": _convert_type(schema.get("type", "string"))}
            if "description" in schema:
                converted["description"] = schema["description"]
            if "enum" in schema:
                converted["enum"] = schema["enum"]
            if schema.get("type") == "object" and "properties" in schema:
                converted["properties"] = _convert_properties(schema["properties"])
            result[name] = converted
        return result

    declarations = []
    for tool in tools:
        schema = tool.get("input_schema", {})
        declarations.append(FunctionDeclaration(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters={
                "type": "OBJECT",
                "properties": _convert_properties(schema.get("properties", {})),
                "required": schema.get("required", []),
            }
        ))

    return [GeminiTool(function_declarations=declarations)]


class GeminiProvider(LLMProvider):
    """Google Gemini via google-generativeai SDK."""

    def __init__(self, api_key: str, model: str):
        genai.configure(api_key=api_key)
        self.model_name = model
        self._system_cache: dict[str, genai.GenerativeModel] = {}

    def _get_model(self, system: str, tools) -> genai.GenerativeModel:
        key = hash(system)
        if key not in self._system_cache:
            self._system_cache[key] = genai.GenerativeModel(
                model_name=self.model_name,
                system_instruction=system,
                tools=tools,
            )
        return self._system_cache[key]

    def chat(self, messages: list[dict], tools: list[dict], system: str) -> LLMResponse:
        gemini_tools = _anthropic_schema_to_gemini(tools)
        model = self._get_model(system, gemini_tools)

        # Convert normalized message history to Gemini format
        history = []
        for msg in messages[:-1]:  # all but the last (which we send as the new message)
            role = "user" if msg["role"] == "user" else "model"
            content = msg["content"]

            if isinstance(content, str):
                history.append({"role": role, "parts": [content]})
            elif isinstance(content, list):
                # Tool results or mixed content
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            parts.append({"function_response": {
                                "name": block.get("tool_use_id", "tool"),
                                "response": {"result": block.get("content", "")}
                            }})
                        elif block.get("type") == "text":
                            parts.append(block.get("text", ""))
                if parts:
                    history.append({"role": role, "parts": parts})

        chat_session = model.start_chat(history=history)

        # Send the last message
        last_msg = messages[-1]
        if isinstance(last_msg["content"], str):
            user_input = last_msg["content"]
        else:
            user_input = " ".join(
                b.get("text", "") for b in last_msg["content"]
                if isinstance(b, dict) and b.get("type") == "text"
            )

        try:
            response = chat_session.send_message(user_input)
        except google.api_core.exceptions.ResourceExhausted as e:
            # Re-raise with a clear label so the endpoint layer can map it to HTTP 429.
            raise google.api_core.exceptions.ResourceExhausted(
                "Gemini API quota exceeded. Try switching LLM_PROVIDER=anthropic in .env "
                "or upgrade your Google AI plan."
            ) from e

        # Parse response
        tool_calls = []
        reply = ""

        for part in response.parts:
            if hasattr(part, "function_call") and part.function_call.name:
                fc = part.function_call
                tool_calls.append(ToolCall(
                    id=f"gemini-{fc.name}-{len(tool_calls)}",
                    name=fc.name,
                    input=dict(fc.args),
                ))
            elif hasattr(part, "text") and part.text:
                reply += part.text

        return LLMResponse(
            reply=reply,
            stop_reason="tool_use" if tool_calls else "end_turn",
            tool_calls=tool_calls,
            raw=response,
        )
