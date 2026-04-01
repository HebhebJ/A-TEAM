"""Reviewer agent: reviews completed work and approves or rejects."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..events import EventBus
from ..llm.base import LLMClient
from ..state.phase import Task
from ..tools.base import ToolRegistry
from .base import AgentResult, BaseAgent

ALLOWED_TOOLS = [
    "read_file",
    "write_file",
    "list_directory",
    "search_files",
    "search_content",
]


@dataclass
class ReviewResult:
    """Parsed review result."""

    approved: bool
    feedback: str
    issues: list[str]
    raw_content: str


class ReviewerAgent:
    """Spawns a BaseAgent configured as the Reviewer."""

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

    async def run(self, task: Task) -> ReviewResult:
        """Review a completed task."""
        prompt_path = Path(__file__).parent.parent / "prompts" / "reviewer.md"
        system_prompt = prompt_path.read_text(encoding="utf-8")

        agent = BaseAgent(
            agent_type="reviewer",
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            project_path=self.project_path,
            system_prompt=system_prompt,
            allowed_tools=ALLOWED_TOOLS,
            model=self.config.model_for_agent("reviewer"),
            event_bus=self.event_bus,
            task_id=task.id,
        )

        # Build context about the task and files to review
        files_info = self._gather_files_info(task)

        user_msg = (
            f"## Task to Review\n\n"
            f"**Title:** {task.title}\n"
            f"**Description:** {task.description}\n"
            f"**Agent Type:** {task.agent_type}\n\n"
            f"## Files Created/Modified\n\n{files_info}\n\n"
            f"Review the task implementation. Read the files, check for quality, "
            f"correctness, and adherence to standards. Then write your review to "
            f".ateam/reviews/{task.id}_review.md and respond with a JSON verdict:\n"
            f'{{"verdict": "APPROVE" or "REJECT", "feedback": "...", "issues": [...]}}'
        )

        result = await agent.run(user_msg)
        return self._parse_review(result, task)

    async def run_batch(self, tasks: list[Task], batch_id: str) -> dict[str, ReviewResult]:
        """Review a batch of tasks in one LLM call. Returns {task_id: ReviewResult}."""
        prompt_path = Path(__file__).parent.parent / "prompts" / "reviewer_batch.md"
        system_prompt = prompt_path.read_text(encoding="utf-8")

        agent = BaseAgent(
            agent_type="reviewer_batch",
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            project_path=self.project_path,
            system_prompt=system_prompt,
            allowed_tools=ALLOWED_TOOLS,
            model=self.config.model_for_agent("reviewer"),
            event_bus=self.event_bus,
        )

        # Build task list context
        tasks_desc = "\n\n".join(
            f"### Task {i+1}: {t.id}\n"
            f"**Title:** {t.title}\n"
            f"**Agent:** {t.agent_type}\n"
            f"**Description:** {t.description}"
            for i, t in enumerate(tasks)
        )

        user_msg = (
            f"## Batch ID: {batch_id}\n\n"
            f"## Tasks to Review\n\n{tasks_desc}\n\n"
            f"Explore the project files, review the work for each task, "
            f"write your review to `.ateam/reviews/batch_{batch_id}_review.md`, "
            f"then respond with the JSON verdict."
        )

        result = await agent.run(user_msg)
        return self._parse_batch_review(result, tasks)

    def _parse_batch_review(
        self, agent_result: AgentResult, tasks: list[Task]
    ) -> dict[str, ReviewResult]:
        """Parse batch review JSON into per-task ReviewResults."""
        content = agent_result.content
        results: dict[str, ReviewResult] = {}

        try:
            json_start = content.find("{")
            json_end = content.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(content[json_start:json_end])
                for t_data in data.get("tasks", []):
                    tid = t_data.get("id", "")
                    results[tid] = ReviewResult(
                        approved=t_data.get("verdict", "").upper() == "APPROVE",
                        feedback=t_data.get("feedback", ""),
                        issues=[],
                        raw_content=content,
                    )
        except (json.JSONDecodeError, KeyError):
            pass

        # Fill in any missing tasks with fallback
        upper = content.upper()
        overall_approved = "REJECT" not in upper
        for task in tasks:
            if task.id not in results:
                results[task.id] = ReviewResult(
                    approved=overall_approved,
                    feedback=content[:500] if not overall_approved else "Approved (batch)",
                    issues=[],
                    raw_content=content,
                )

        return results

    def _gather_files_info(self, task: Task) -> str:
        """List files the task claims to have created."""
        if not task.files_created:
            return "(No files recorded — reviewer should explore the project to find relevant changes)"

        lines = []
        for f in task.files_created:
            path = self.project_path / f
            if path.exists():
                size = path.stat().st_size
                lines.append(f"- {f} ({size} bytes)")
            else:
                lines.append(f"- {f} (NOT FOUND)")
        return "\n".join(lines)

    def _parse_review(self, agent_result: AgentResult, task: Task) -> ReviewResult:
        """Extract structured review from agent output."""
        content = agent_result.content

        # Try to find JSON in the response
        try:
            # Look for JSON block in the content
            json_start = content.find("{")
            json_end = content.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                json_str = content[json_start:json_end]
                data = json.loads(json_str)
                return ReviewResult(
                    approved=data.get("verdict", "").upper() == "APPROVE",
                    feedback=data.get("feedback", ""),
                    issues=data.get("issues", []),
                    raw_content=content,
                )
        except (json.JSONDecodeError, KeyError):
            pass

        # Fallback: check for APPROVE/REJECT keywords
        upper = content.upper()
        approved = "APPROVE" in upper and "REJECT" not in upper

        return ReviewResult(
            approved=approved,
            feedback=content,
            issues=[],
            raw_content=content,
        )
