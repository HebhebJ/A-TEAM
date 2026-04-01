"""Architect agent: analyzes requirements, decides stack, creates architecture docs."""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..events import EventBus
from ..llm.base import LLMClient
from ..tools.base import ToolRegistry
from .base import AgentResult, BaseAgent

ALLOWED_TOOLS = ["read_file", "write_file", "list_directory"]


class ArchitectAgent:
    """Spawns a BaseAgent configured as the Architect."""

    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        project_path: Path,
        config: Config,
        event_bus: EventBus | None = None,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.project_path = project_path
        self.config = config
        self.event_bus = event_bus

    async def run(self, user_request: str) -> AgentResult:
        """Run the architect on the user's request."""
        prompt_path = Path(__file__).parent.parent / "prompts" / "architect.md"
        system_prompt = prompt_path.read_text(encoding="utf-8")

        agent = BaseAgent(
            agent_type="architect",
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            project_path=self.project_path,
            system_prompt=system_prompt,
            allowed_tools=ALLOWED_TOOLS,
            model=self.config.model_for_agent("architect"),
            event_bus=self.event_bus,
        )

        user_msg = f"## Project Request\n\n{user_request}"
        return await agent.run(user_msg)
