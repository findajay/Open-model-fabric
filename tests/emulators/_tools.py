"""Deterministic tool-calling behaviour shared by the protocol emulators.

Rule: when the request offers tools (and ``tool_choice`` is not ``none``) and the last
message is from the user, the "model" calls one tool (the forced one, else the first)
with arguments built from the schema's required properties. When the last message is a
tool result, it answers in text that quotes the results. No model runs.
"""

from __future__ import annotations

from typing import Any


def _value(schema: dict[str, Any], prompt: str) -> Any:
    if schema.get("enum"):
        return schema["enum"][0]
    return {"integer": 1, "number": 1, "boolean": True, "array": [], "object": {}}.get(
        str(schema.get("type")), prompt or "example"
    )


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if p.get("type") == "text")


def plan_call(body: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    tools = body.get("tools") or []
    messages = body.get("messages") or []
    choice = body.get("tool_choice", "auto")
    if not tools or choice == "none" or not messages or messages[-1].get("role") != "user":
        return None
    functions = [t.get("function") or {} for t in tools]
    forced = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
    function = next((f for f in functions if f.get("name") == forced), functions[0])
    schema = function.get("parameters") or {}
    properties = schema.get("properties") or {}
    prompt = _text(messages[-1].get("content"))[:100]
    required = schema.get("required") or list(properties)[:1]
    return str(function.get("name")), {
        name: _value(properties.get(name) or {}, prompt) for name in required
    }


def result_text(messages: list[dict[str, Any]], engine: str) -> str | None:
    if not messages or messages[-1].get("role") != "tool":
        return None
    results = [
        f"{m.get('tool_name') or m.get('tool_call_id') or 'tool'}={_text(m.get('content'))}"
        for m in messages
        if m.get("role") == "tool"
    ]
    return f"{engine} used tool results: {'; '.join(results)}. Not a real model."
