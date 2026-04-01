"""Shared data structures for LLM communication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolCall:
    """A tool/function call requested by the LLM."""

    id: str
    function_name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """Result of executing a tool call."""

    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    """A single message in the conversation."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None  # for role="tool"

    @staticmethod
    def system(content: str) -> Message:
        return Message(role="system", content=content)

    @staticmethod
    def user(content: str) -> Message:
        return Message(role="user", content=content)

    @staticmethod
    def assistant(content: str | None = None, tool_calls: list[ToolCall] | None = None) -> Message:
        return Message(role="assistant", content=content, tool_calls=tool_calls)

    @staticmethod
    def tool(tool_call_id: str, content: str) -> Message:
        return Message(role="tool", content=content, tool_call_id=tool_call_id)

    def to_openai_dict(self) -> dict[str, Any]:
        """Convert to OpenAI-compatible message dict."""
        msg: dict[str, Any] = {"role": self.role}

        if self.content is not None:
            msg["content"] = self.content

        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function_name,
                        "arguments": _json_dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]

        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id

        return msg


@dataclass
class TokenUsage:
    """Token usage from an LLM response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """Response from an LLM call."""

    message: Message
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    finish_reason: str = ""


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj)
