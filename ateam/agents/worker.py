"""Worker agent: implements tasks (frontend, backend, database, devops)."""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..events import EventBus
from ..llm.base import LLMClient
from ..state.phase import Task
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

# Map agent_type to prompt file
PROMPT_MAP = {
    "frontend": "worker_frontend.md",
    "backend": "worker_backend.md",
    "database": "worker_database.md",
    "devops": "worker_devops.md",
}


class WorkerAgent:
    """Spawns a BaseAgent configured as a specialized Worker."""

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

    async def run(
        self,
        task: Task,
        completed_tasks_summary: str = "",
        retry_feedback: str | None = None,
    ) -> AgentResult:
        """Run a worker on a specific task."""
        prompt_file = PROMPT_MAP.get(task.agent_type, "worker_backend.md")
        prompt_path = Path(__file__).parent.parent / "prompts" / prompt_file
        system_prompt = prompt_path.read_text(encoding="utf-8")

        agent = BaseAgent(
            agent_type=f"worker_{task.agent_type}",
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            project_path=self.project_path,
            system_prompt=system_prompt,
            allowed_tools=ALL_TOOLS,
            model=self.config.model_for_agent("worker"),
            event_bus=self.event_bus,
            task_id=task.id,
        )

        # Build user message with task context
        parts = [
            f"## Task: {task.title}\n\n{task.description}",
        ]

        # Load architecture context
        arch_context = self._load_context()
        if arch_context:
            parts.append(f"## Project Context\n\n{arch_context}")

        if completed_tasks_summary:
            parts.append(f"## Previously Completed Tasks\n\n{completed_tasks_summary}")

        if retry_feedback:
            parts.append(
                f"## IMPORTANT: Reviewer Feedback (fix these issues)\n\n{retry_feedback}"
            )

        return await agent.run("\n\n---\n\n".join(parts))

    def _load_context(self) -> str:
        """Load architecture + standards as context for the worker."""
        ateam_dir = self.project_path / ".ateam"
        docs = []
        for name in ["architecture.md", "standards.md"]:
            path = ateam_dir / name
            if path.exists():
                docs.append(path.read_text(encoding="utf-8"))
        return "\n\n---\n\n".join(docs)
