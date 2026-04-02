"""Intervention agent: operator-directed project maintenance and repair."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Config
from ..events import EventBus
from ..intervention import (
    append_intervention_history,
    read_intervention_history,
    write_intervention_state,
)
from ..llm.base import LLMClient
from ..tools.base import ToolRegistry
from .base import AgentResult, BaseAgent

ALL_TOOLS = [
    "read_file",
    "write_file",
    "list_directory",
    "search_files",
    "search_content",
    "web_search",
    "fetch_url",
    "run_command",
]


class InterventionAgent:
    """Operator-facing maintenance agent for targeted project fixes."""

    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        project_path: Path,
        config: Config,
        event_bus: EventBus | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.project_path = project_path
        self.config = config
        self.event_bus = event_bus

    async def run(self, instruction: str) -> AgentResult:
        prompt_path = Path(__file__).parent.parent / "prompts" / "intervention.md"
        system_prompt = prompt_path.read_text(encoding="utf-8")

        agent = BaseAgent(
            agent_type="intervention",
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            project_path=self.project_path,
            system_prompt=system_prompt,
            allowed_tools=ALL_TOOLS,
            model=self.config.model_for_agent("intervention"),
            event_bus=self.event_bus,
        )

        append_intervention_history(
            self.project_path,
            "user",
            instruction,
            kind="instruction",
        )

        if self.event_bus:
            self.event_bus.emit("intervention.started", instruction=instruction)

        user_message = self._build_user_message(instruction)
        result = await agent.run(user_message)

        append_intervention_history(
            self.project_path,
            "assistant",
            result.content,
            kind="result",
            meta={
                "tool_calls": result.tool_calls_made,
                "iterations": result.iterations,
                "tokens": result.total_tokens,
                "log_file": result.log_file,
            },
        )

        write_intervention_state(
            self.project_path,
            {
                "last_instruction": instruction,
                "last_result": result.content,
                "summary": result.content[:4000],
                "log_file": result.log_file,
            },
        )

        if self.event_bus:
            self.event_bus.emit(
                "intervention.completed",
                summary=result.content[:500],
                tool_calls=result.tool_calls_made,
                iterations=result.iterations,
                tokens=result.total_tokens,
            )

        return result

    def _build_user_message(self, instruction: str) -> str:
        parts = [
            "## Operator Instruction\n\n" + instruction,
            "## Current Project State\n\n" + self._load_state_context(),
            "## Project Docs\n\n" + self._load_docs_context(),
        ]

        history = read_intervention_history(self.project_path, limit=12)
        if history:
            rendered = []
            for item in history:
                role = item.get("role", "unknown").upper()
                kind = item.get("kind", "message")
                content = item.get("content", "")
                rendered.append(f"[{role}:{kind}] {content}")
            parts.append("## Recent Intervention History\n\n" + "\n\n".join(rendered))

        parts.append(
            "## Operating Constraints\n\n"
            "- You are in maintenance mode, not feature-delivery mode.\n"
            "- Make the smallest high-confidence change that addresses the operator's request.\n"
            "- You may edit normal project files and `.ateam/` metadata/docs when needed to repair consistency.\n"
            "- Do not resume the main project process automatically.\n"
            "- Do not delete the whole project, remove large directories, install global packages, or kill unrelated processes.\n"
            "- If you change docs/state/plan, explain exactly why they needed alignment.\n"
            "- End with a concise operator-facing summary of what you changed, what remains risky, and the recommended next action."
        )

        return "\n\n---\n\n".join(parts)

    def _load_state_context(self) -> str:
        ateam_dir = self.project_path / ".ateam"
        chunks: list[str] = []
        for name in ["state.json", "plan.json", "launch.json"]:
            path = ateam_dir / name
            if path.exists():
                text = path.read_text(encoding="utf-8")
                if name.endswith(".json"):
                    try:
                        parsed = json.loads(text)
                        text = json.dumps(parsed, indent=2)
                    except Exception:
                        pass
                if len(text) > 30000:
                    text = text[:30000] + "\n... [truncated]"
                chunks.append(f"### {name}\n{text}")
        return "\n\n".join(chunks) if chunks else "(No saved project state files found)"

    def _load_docs_context(self) -> str:
        ateam_dir = self.project_path / ".ateam"
        docs: list[str] = []
        for name in ["architecture.md", "standards.md", "design.md", "tech_stack.md", "plan.md"]:
            path = ateam_dir / name
            if path.exists():
                text = path.read_text(encoding="utf-8")
                if len(text) > 24000:
                    text = text[:24000] + "\n... [truncated]"
                docs.append(f"### {name}\n{text}")
        return "\n\n".join(docs) if docs else "(No project docs found)"
