"""Planner agent: reads architecture docs, creates phases and tasks."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Config
from ..events import EventBus
from ..llm.base import LLMClient
from ..state.phase import Phase, Task
from ..tools.base import ToolRegistry
from .base import AgentResult, BaseAgent

ALLOWED_TOOLS = ["read_file", "write_file", "list_directory"]

PLAN_JSON_SCHEMA = """
{
  "phases": [
    {
      "id": "phase_1",
      "name": "Phase name",
      "description": "What this phase accomplishes",
      "tasks": [
        {
          "id": "phase1_task1",
          "title": "Task title",
          "description": "Detailed description of what to implement",
          "agent_type": "backend|frontend|database|devops",
          "dependencies": ["phase1_task0"]
        }
      ]
    }
  ]
}
"""


class PlannerAgent:
    """Spawns a BaseAgent configured as the Planner."""

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

    async def run(self) -> AgentResult:
        """Run the planner, reading architecture docs from .ateam/."""
        prompt_path = Path(__file__).parent.parent / "prompts" / "planner.md"
        system_prompt = prompt_path.read_text(encoding="utf-8")

        agent = BaseAgent(
            agent_type="planner",
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            project_path=self.project_path,
            system_prompt=system_prompt,
            allowed_tools=ALLOWED_TOOLS,
            model=self.config.model_for_agent("planner"),
            event_bus=self.event_bus,
        )

        # Build context from architecture docs
        docs_context = self._load_architecture_docs()

        user_msg = (
            f"## Architecture Documents\n\n{docs_context}\n\n"
            f"## Plan JSON Schema\n\nYour plan.json must follow this structure:\n"
            f"```json\n{PLAN_JSON_SCHEMA}\n```\n\n"
            f"Read the architecture documents in .ateam/ and create the plan."
        )

        return await agent.run(user_msg)

    def _load_architecture_docs(self) -> str:
        """Load architecture docs from .ateam/ as context."""
        ateam_dir = self.project_path / ".ateam"
        docs = []
        for doc_name in ["architecture.md", "standards.md", "design.md", "tech_stack.md"]:
            doc_path = ateam_dir / doc_name
            if doc_path.exists():
                content = doc_path.read_text(encoding="utf-8")
                docs.append(f"### {doc_name}\n\n{content}")
        return "\n\n---\n\n".join(docs) if docs else "(No architecture docs found)"

    @staticmethod
    def parse_plan(project_path: Path) -> list[Phase]:
        """Parse the plan.json file into Phase objects."""
        plan_file = project_path / ".ateam" / "plan.json"
        if not plan_file.exists():
            raise FileNotFoundError("plan.json not found in .ateam/")

        data = json.loads(plan_file.read_text(encoding="utf-8"))
        phases = []

        for phase_data in data.get("phases", []):
            tasks = []
            for task_data in phase_data.get("tasks", []):
                tasks.append(
                    Task(
                        id=task_data["id"],
                        title=task_data["title"],
                        description=task_data["description"],
                        agent_type=task_data.get("agent_type", "backend"),
                        dependencies=task_data.get("dependencies", []),
                    )
                )
            phases.append(
                Phase(
                    id=phase_data["id"],
                    name=phase_data["name"],
                    description=phase_data.get("description", ""),
                    tasks=tasks,
                )
            )

        return phases
