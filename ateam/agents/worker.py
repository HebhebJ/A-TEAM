"""Worker agent: implements tasks (frontend, backend, database, devops)."""

from __future__ import annotations

import platform
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

        # Platform awareness — prevents agents from using wrong shell syntax
        os_name = platform.system()  # "Windows", "Linux", "Darwin"
        if os_name == "Windows":
            parts.append(
                "## Platform: Windows\n\n"
                "You are running on **Windows**. Use Windows-compatible commands:\n"
                "- Use `mkdir` (not `mkdir -p`) — Windows mkdir creates parents by default\n"
                "- Use `copy` / `xcopy` / `powershell Copy-Item` (not `cp`)\n"
                "- Use `type` or `powershell Get-Content` (not `cat`)\n"
                "- Use `del` / `rmdir` (not `rm`, `rm -rf`)\n"
                "- Use `dir` (not `ls`)\n"
                "- Do NOT use `sed`, `grep`, `awk`, `chmod`, `ln -s` — they don't exist\n"
                "- Use `powershell` one-liners for text manipulation if needed\n"
                "- Paths use backslashes `\\` but forward slashes `/` usually work too\n"
                "- npm/npx/node commands work the same as on Unix\n"
                "- NEVER delete the whole project or large directories to recover from an error\n"
                "- NEVER install global packages (`npm install -g`) or kill unrelated processes"
            )
        elif os_name == "Darwin":
            parts.append("## Platform: macOS\n\nYou are running on macOS. Use Unix shell commands.")
        else:
            parts.append(f"## Platform: {os_name}\n\nYou are running on Linux. Use Unix shell commands.")

        # Load architecture context
        arch_context = self._load_context()
        if arch_context:
            parts.append(f"## Project Context\n\n{arch_context}")

        if completed_tasks_summary:
            parts.append(f"## Previously Completed Tasks\n\n{completed_tasks_summary}")

        parts.append(
            "## Safety Constraints\n\n"
            "- Stay within this task's scope. Do not pre-implement later-phase work just because it appears in the architecture.\n"
            "- Preserve the existing framework, major version, and style format unless the task or reviewer explicitly asks to change them.\n"
            "- Prefer targeted file edits over project-wide cleanup.\n"
            "- Do not delete/recreate the project, remove large directories, install global packages, or kill processes as a recovery tactic."
        )

        if retry_feedback:
            parts.append(
                f"## IMPORTANT: Reviewer Feedback (fix these issues)\n\n{retry_feedback}"
            )
            parts.append(
                "## Retry Constraints\n\n"
                "- Fix only the issues called out above.\n"
                "- Do not reinterpret the task from scratch.\n"
                "- Do not change framework version, routing strategy, or CSS/SCSS choice unless the reviewer explicitly requires it.\n"
                "- Do not delete and recreate the project to recover from a rejected review."
            )

        return await agent.run("\n\n---\n\n".join(parts))

    def _load_context(self) -> str:
        """Load architecture docs as context for the worker.

        Supports both the new 2-file format (blueprint.md + standards.md)
        and the legacy 4-file format (architecture.md + standards.md + design.md + tech_stack.md).
        """
        ateam_dir = self.project_path / ".ateam"
        docs = []

        # Try new 2-file format first
        blueprint = ateam_dir / "blueprint.md"
        standards = ateam_dir / "standards.md"
        if blueprint.exists():
            docs.append(blueprint.read_text(encoding="utf-8"))
        if standards.exists():
            docs.append(standards.read_text(encoding="utf-8"))

        # Fall back to legacy 4-file format
        if not docs:
            for name in ["architecture.md", "standards.md", "design.md", "tech_stack.md"]:
                path = ateam_dir / name
                if path.exists():
                    docs.append(path.read_text(encoding="utf-8"))

        return "\n\n---\n\n".join(docs) if docs else "(No project context found)"
