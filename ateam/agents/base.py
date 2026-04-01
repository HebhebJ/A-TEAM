"""BaseAgent: the agentic tool-calling loop shared by all agents."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..events import EventBus
from ..llm.base import LLMClient
from ..llm.message_types import LLMResponse, Message, ToolResult
from ..tools.base import ToolRegistry

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 50


@dataclass
class AgentResult:
    """Final result from an agent run."""

    content: str
    tool_calls_made: int = 0
    iterations: int = 0
    total_tokens: int = 0
    log_file: str | None = None


class BaseAgent:
    """Agent that runs the LLM tool-calling loop.

    1. Sends system prompt + user message to LLM
    2. If LLM returns tool_calls -> execute them -> append results -> loop
    3. If LLM returns text (no tool_calls) -> return as final result
    4. Logs everything to .ateam/logs/
    """

    def __init__(
        self,
        agent_type: str,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        project_path: Path,
        system_prompt: str,
        allowed_tools: list[str] | None = None,
        model: str | None = None,
        event_bus: EventBus | None = None,
        task_id: str | None = None,
    ):
        self.agent_type = agent_type
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.project_path = project_path
        self.system_prompt = system_prompt
        self.allowed_tools = allowed_tools
        self.model = model
        self.event_bus = event_bus
        self.task_id = task_id

    async def run(self, user_message: str) -> AgentResult:
        """Run the agent with a user message until completion."""
        messages: list[Message] = [
            Message.system(self.system_prompt),
            Message.user(user_message),
        ]

        tool_schemas = self.tool_registry.get_schemas(self.allowed_tools)
        tool_calls_made = 0
        iterations = 0
        total_tokens = 0
        log_entries: list[dict[str, Any]] = []

        logger.info("[%s] Agent started", self.agent_type)
        if self.event_bus:
            self.event_bus.agent_started(self.agent_type, self.task_id)

        while iterations < MAX_ITERATIONS:
            iterations += 1

            # Call LLM
            response: LLMResponse = await self.llm_client.chat(
                messages=messages,
                tools=tool_schemas if tool_schemas else None,
                model=self.model,
            )

            total_tokens += response.usage.total_tokens
            log_entries.append(self._log_response(response))

            assistant_msg = response.message
            messages.append(assistant_msg)

            # If no tool calls, we're done
            if not assistant_msg.tool_calls:
                logger.info(
                    "[%s] Agent completed after %d iterations, %d tool calls",
                    self.agent_type,
                    iterations,
                    tool_calls_made,
                )
                if self.event_bus:
                    self.event_bus.agent_completed(self.agent_type, iterations, tool_calls_made, total_tokens)
                    self.event_bus.tokens_update(
                        self.llm_client.total_usage.prompt_tokens,
                        self.llm_client.total_usage.completion_tokens,
                        self.llm_client.total_usage.total_tokens,
                    )
                log_file = self._save_log(log_entries)
                return AgentResult(
                    content=assistant_msg.content or "",
                    tool_calls_made=tool_calls_made,
                    iterations=iterations,
                    total_tokens=total_tokens,
                    log_file=log_file,
                )

            # Execute tool calls
            for tc in assistant_msg.tool_calls:
                tool_calls_made += 1
                logger.info(
                    "[%s] Tool call #%d: %s(%s)",
                    self.agent_type,
                    tool_calls_made,
                    tc.function_name,
                    _truncate(str(tc.arguments), 100),
                )

                if self.event_bus:
                    self.event_bus.agent_tool_call(
                        self.agent_type, tc.function_name, _truncate(str(tc.arguments), 200)
                    )

                result = await self.tool_registry.execute(
                    tc.function_name, tc.arguments, self.project_path
                )

                if self.event_bus:
                    self.event_bus.agent_tool_result(
                        self.agent_type, tc.function_name, _truncate(result, 300)
                    )

                log_entries.append(
                    {
                        "type": "tool_call",
                        "tool": tc.function_name,
                        "arguments": tc.arguments,
                        "result": _truncate(result, 2000),
                    }
                )

                # Append tool result message
                messages.append(Message.tool(tc.id, result))

        # Hit max iterations
        logger.warning("[%s] Hit max iterations (%d)", self.agent_type, MAX_ITERATIONS)
        log_file = self._save_log(log_entries)
        return AgentResult(
            content="Error: Agent hit maximum iteration limit",
            tool_calls_made=tool_calls_made,
            iterations=iterations,
            total_tokens=total_tokens,
            log_file=log_file,
        )

    def _log_response(self, response: LLMResponse) -> dict[str, Any]:
        """Create a log entry for an LLM response."""
        entry: dict[str, Any] = {
            "type": "llm_response",
            "model": response.model,
            "finish_reason": response.finish_reason,
            "tokens": response.usage.total_tokens,
        }
        if response.message.content:
            entry["content_preview"] = _truncate(response.message.content, 500)
        if response.message.tool_calls:
            entry["tool_calls"] = [
                {"name": tc.function_name, "args_preview": _truncate(str(tc.arguments), 200)}
                for tc in response.message.tool_calls
            ]
        return entry

    def _save_log(self, entries: list[dict[str, Any]]) -> str | None:
        """Save log entries to .ateam/logs/."""
        try:
            log_dir = self.project_path / ".ateam" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            log_file = log_dir / f"{self.agent_type}_{timestamp}.jsonl"

            with open(log_file, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, default=str) + "\n")

            return str(log_file.relative_to(self.project_path))
        except Exception as e:
            logger.error("Failed to save log: %s", e)
            return None


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
