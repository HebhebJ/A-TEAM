"""OpenRouter LLM client implementation."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from .message_types import LLMResponse, Message, TokenUsage, ToolCall

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
RETRY_BACKOFF = [2, 4, 8, 16, 32]  # seconds between retries


class OpenRouterClient:
    """LLM client using OpenRouter's OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        default_model: str = "anthropic/claude-sonnet-4",
        timeout: float = 120.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/a-team-agent",
                "X-Title": "A-TEAM Agent System",
            },
        )
        # Track cumulative token usage
        self.total_usage = TokenUsage()

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """Send a chat completion request to OpenRouter."""
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": [m.to_openai_dict() for m in messages],
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        logger.debug(
            "LLM request: model=%s, messages=%d, tools=%d",
            payload["model"],
            len(messages),
            len(tools) if tools else 0,
        )

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                )
            except httpx.TimeoutException as e:
                last_error = e
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning("Request timed out (attempt %d/%d), retrying in %ds...", attempt + 1, MAX_RETRIES, wait)
                await asyncio.sleep(wait)
                continue

            if response.status_code == 200:
                data = response.json()
                try:
                    return self._parse_response(data)
                except LLMAPIError as e:
                    # 200 but error in body (provider-side issue) — retry
                    wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    logger.warning(
                        "Provider error in 200 response (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, MAX_RETRIES, wait, e,
                    )
                    last_error = e
                    await asyncio.sleep(wait)
                    continue

            # Rate limited — retry with backoff
            if response.status_code == 429:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning(
                    "Rate limited (attempt %d/%d), retrying in %ds...",
                    attempt + 1, MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                last_error = LLMAPIError(f"OpenRouter API error 429: {response.text}")
                continue

            # Server errors (5xx) — retry
            if response.status_code >= 500:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning(
                    "Server error %d (attempt %d/%d), retrying in %ds...",
                    response.status_code, attempt + 1, MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                last_error = LLMAPIError(f"OpenRouter API error {response.status_code}: {response.text}")
                continue

            # Any other error — fail immediately
            error_body = response.text
            logger.error("LLM API error %d: %s", response.status_code, error_body)
            raise LLMAPIError(f"OpenRouter API error {response.status_code}: {error_body}")

        raise last_error or LLMAPIError("Max retries exceeded")

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        """Parse OpenAI-format response into our types."""
        # OpenRouter can return a 200 with an error body (no 'choices')
        if "error" in data or "choices" not in data:
            error = data.get("error", data)
            msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise LLMAPIError(f"OpenRouter error in response body: {msg}")

        choice = data["choices"][0]
        raw_msg = choice["message"]

        # Parse tool calls if present
        tool_calls: list[ToolCall] | None = None
        if raw_msg.get("tool_calls"):
            tool_calls = []
            for tc in raw_msg["tool_calls"]:
                func = tc["function"]
                try:
                    arguments = json.loads(func["arguments"])
                except (json.JSONDecodeError, TypeError):
                    arguments = {"raw": func["arguments"]}
                tool_calls.append(
                    ToolCall(
                        id=tc["id"],
                        function_name=func["name"],
                        arguments=arguments,
                    )
                )

        message = Message(
            role="assistant",
            content=raw_msg.get("content"),
            tool_calls=tool_calls,
        )

        # Parse usage
        raw_usage = data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=raw_usage.get("prompt_tokens", 0),
            completion_tokens=raw_usage.get("completion_tokens", 0),
            total_tokens=raw_usage.get("total_tokens", 0),
        )

        # Accumulate
        self.total_usage.prompt_tokens += usage.prompt_tokens
        self.total_usage.completion_tokens += usage.completion_tokens
        self.total_usage.total_tokens += usage.total_tokens

        return LLMResponse(
            message=message,
            usage=usage,
            model=data.get("model", ""),
            finish_reason=choice.get("finish_reason", ""),
        )

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()


class LLMAPIError(Exception):
    """Raised when the LLM API returns an error."""
