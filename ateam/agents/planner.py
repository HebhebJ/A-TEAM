"""Planner agent: reads architecture docs, creates phases and tasks."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

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
        """Load architecture docs from .ateam/ as context.

        Supports both the new 2-file format (blueprint.md + standards.md)
        and the legacy 4-file format (architecture.md + standards.md + design.md + tech_stack.md).
        """
        ateam_dir = self.project_path / ".ateam"
        docs = []

        # Try new 2-file format first
        blueprint = ateam_dir / "blueprint.md"
        standards = ateam_dir / "standards.md"
        if blueprint.exists():
            docs.append(f"### blueprint.md\n\n{blueprint.read_text(encoding='utf-8')}")
        if standards.exists():
            docs.append(f"### standards.md\n\n{standards.read_text(encoding='utf-8')}")

        # Fall back to legacy 4-file format
        if not docs:
            for doc_name in ["architecture.md", "standards.md", "design.md", "tech_stack.md"]:
                doc_path = ateam_dir / doc_name
                if doc_path.exists():
                    content = doc_path.read_text(encoding="utf-8")
                    docs.append(f"### {doc_name}\n\n{content}")

        return "\n\n---\n\n".join(docs) if docs else "(No architecture docs found)"

    @staticmethod
    def parse_plan(project_path: Path) -> list[Phase]:
        """Parse the plan.json file into Phase objects with full validation.

        Raises ValueError with a clear message if the plan is malformed,
        has duplicate task IDs, broken dependencies, or invalid agent types.
        """
        plan_file = project_path / ".ateam" / "plan.json"
        if not plan_file.exists():
            raise FileNotFoundError("plan.json not found in .ateam/")

        raw = plan_file.read_text(encoding="utf-8")

        # 1. Strip markdown code fences (common LLM mistake)
        raw = _strip_code_fences(raw)

        # 2. Parse JSON
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"plan.json is not valid JSON: {e}")

        # 3. Validate top-level structure
        if not isinstance(data, dict):
            raise ValueError("plan.json must be a JSON object, got " + type(data).__name__)
        if "phases" not in data:
            raise ValueError("plan.json must have a 'phases' key")
        if not isinstance(data["phases"], list):
            raise ValueError("plan.json 'phases' must be an array")
        if not data["phases"]:
            raise ValueError("plan.json 'phases' is empty — at least one phase is required")

        # 4. Validate each phase and build Phase objects
        phases: list[Phase] = []
        all_task_ids: set[str] = set()
        valid_agent_types = {"frontend", "backend", "database", "devops"}

        for i, phase_data in enumerate(data["phases"]):
            _validate_phase_schema(phase_data, i)

            tasks: list[Task] = []
            for j, task_data in enumerate(phase_data.get("tasks", [])):
                # Check for duplicate task IDs across all phases
                task_id = task_data["id"]
                if task_id in all_task_ids:
                    raise ValueError(
                        f"Duplicate task ID '{task_id}' in phase {i + 1}, task {j + 1}. "
                        f"Each task ID must be unique across the entire plan."
                    )
                all_task_ids.add(task_id)

                # Validate agent_type
                agent_type = task_data.get("agent_type", "backend")
                if agent_type not in valid_agent_types:
                    raise ValueError(
                        f"Task '{task_id}' has invalid agent_type '{agent_type}'. "
                        f"Must be one of: {', '.join(sorted(valid_agent_types))}"
                    )

                # Validate dependencies reference valid task IDs (deferred — collected below)
                tasks.append(
                    Task(
                        id=task_id,
                        title=task_data["title"],
                        description=task_data["description"],
                        agent_type=agent_type,
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

        # 5. Validate dependency integrity — all dep references must exist
        for phase in phases:
            for task in phase.tasks:
                for dep_id in task.dependencies:
                    if dep_id not in all_task_ids:
                        raise ValueError(
                            f"Task '{task.id}' depends on '{dep_id}' which does not exist. "
                            f"Check the 'dependencies' list for typos."
                        )

        # 6. Check for circular dependencies (simple DFS)
        _check_no_cycles(all_task_ids, phases)

        return phases


# ── Validation helpers ────────────────────────────────────────────────────────

_REQUIRED_PHASE_KEYS = {"id", "name", "tasks"}
_REQUIRED_TASK_KEYS = {"id", "title", "description"}


def _strip_code_fences(text: str) -> str:
    """Strip markdown ```json ... ``` fences that LLMs sometimes wrap JSON in."""
    # Match ```json ... ``` or ``` ... ``` blocks
    pattern = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.MULTILINE)
    match = pattern.search(text)
    if match:
        return match.group(1).strip()
    return text


def _validate_phase_schema(phase_data: dict, index: int) -> None:
    """Validate a single phase dict has the required keys."""
    missing = _REQUIRED_PHASE_KEYS - set(phase_data.keys())
    if missing:
        raise ValueError(
            f"Phase {index + 1} is missing required keys: {', '.join(sorted(missing))}. "
            f"Each phase must have 'id', 'name', and 'tasks'."
        )
    if not isinstance(phase_data.get("tasks"), list):
        raise ValueError(f"Phase {index + 1} 'tasks' must be an array")
    if not phase_data["tasks"]:
        raise ValueError(f"Phase {index + 1} ('{phase_data.get('id', '?')}') has no tasks")


def _check_no_cycles(all_task_ids: set[str], phases: list[Phase]) -> None:
    """Detect circular dependencies using DFS. Raises ValueError if found."""
    # Build adjacency list
    graph: dict[str, list[str]] = {}
    for phase in phases:
        for task in phase.tasks:
            graph[task.id] = list(task.dependencies)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in all_task_ids}
    parent: dict[str, str | None] = {tid: None for tid in all_task_ids}

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        for dep in graph.get(node, []):
            if color[dep] == GRAY:
                # Found cycle — reconstruct it
                cycle = [dep, node]
                cur = node
                while cur != dep:
                    cur = parent.get(cur)
                    if cur is None:
                        break
                    cycle.append(cur)
                return cycle
            if color[dep] == WHITE:
                parent[dep] = node
                result = dfs(dep)
                if result:
                    return result
        color[node] = BLACK
        return None

    for tid in all_task_ids:
        if color[tid] == WHITE:
            cycle = dfs(tid)
            if cycle:
                cycle.reverse()
                raise ValueError(
                    f"Circular dependency detected: {' -> '.join(cycle)}. "
                    f"Task dependencies must form a DAG (directed acyclic graph)."
                )
