"""Abstract LLM client protocol."""

from __future__ import annotations

from typing import Protocol

from .message_types import LLMResponse, Message


class LLMClient(Protocol):
    """Protocol for LLM providers."""

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """Send a chat completion request.

        Args:
            messages: Conversation messages.
            tools: OpenAI-format tool/function schemas (optional).
            model: Model override (optional, uses client default).

        Returns:
            LLMResponse with the assistant's message and metadata.
        """
        ...
