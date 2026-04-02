"""Orchestrator: deterministic state machine driving the agent pipeline."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable, Awaitable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..config import Config
from ..events import EventBus
from ..llm.openrouter import OpenRouterClient
from ..state.phase import Phase, Task
from ..state.project_state import ProjectState
from ..tools.base import ToolRegistry
from ..tools.file_ops import ReadFileTool, WriteFileTool, ListDirectoryTool
from ..tools.search import SearchFilesTool, SearchContentTool
from ..tools.shell import RunCommandTool
from ..tools.web import WebSearchTool, FetchUrlTool
from .architect import ArchitectAgent
from .planner import PlannerAgent
from .reviewer import ReviewerAgent
from .worker import WorkerAgent

logger = logging.getLogger(__name__)
console = Console()

# Type for the checkpoint callback
CheckpointCallback = Callable[[str, str, list[Path]], Awaitable[bool]]


class Orchestrator:
    """State machine that drives the A-TEAM pipeline.

    NOT an LLM agent — this is deterministic Python.
    """

    def __init__(
        self,
        config: Config,
        project_name: str,
        checkpoint_callback: CheckpointCallback | None = None,
    ):
        self.config = config
        self.project_name = project_name
        self.project_path = (config.workspace_dir / project_name).resolve()
        self.checkpoint_callback = checkpoint_callback

        # Initialize LLM client
        self.llm_client = OpenRouterClient(
            api_key=config.openrouter_api_key,
            base_url=config.api_base_url,
            default_model=config.default_model,
        )

        # Initialize tool registry
        self.tool_registry = ToolRegistry()
        self._register_tools()

        # EventBus — created lazily once project_path is set up
        self.event_bus: EventBus | None = None

    def _register_tools(self) -> None:
        """Register all available tools."""
        self.tool_registry.register(ReadFileTool())
        self.tool_registry.register(WriteFileTool())
        self.tool_registry.register(ListDirectoryTool())
        self.tool_registry.register(SearchFilesTool())
        self.tool_registry.register(SearchContentTool())
        self.tool_registry.register(RunCommandTool(timeout=self.config.command_timeout))
        self.tool_registry.register(WebSearchTool())
        self.tool_registry.register(FetchUrlTool())

    async def run(self, user_request: str) -> None:
        """Run the full pipeline from request to completion."""
        # Set up project workspace
        self.project_path.mkdir(parents=True, exist_ok=True)
        (self.project_path / ".ateam").mkdir(exist_ok=True)
        (self.project_path / ".ateam" / "logs").mkdir(exist_ok=True)
        (self.project_path / ".ateam" / "reviews").mkdir(exist_ok=True)

        # Initialize event bus now that project_path exists
        self.event_bus = EventBus(self.project_path)

        # Load or create state
        state = ProjectState.load(self.project_path)
        if state.status == "initialized" or not state.project_name:
            state = ProjectState.create(self.project_name, user_request)
            state.save(self.project_path)

        # Backfill user_request if it was lost (e.g. dashboard launch / crash)
        if user_request and not state.user_request:
            state.user_request = user_request
            state.save(self.project_path)

        self.event_bus.project_started(self.project_name, user_request or state.user_request)

        console.print(
            Panel(
                f"[bold]Project:[/bold] {self.project_name}\n"
                f"[bold]Request:[/bold] {user_request}\n"
                f"[bold]Workspace:[/bold] {self.project_path}",
                title="[bold blue]A-TEAM[/bold blue]",
            )
        )

        try:
            # Resume from current state
            await self._run_from_state(state, user_request)
        finally:
            await self.llm_client.close()
            self._print_usage()

    async def _run_from_state(self, state: ProjectState, user_request: str) -> None:
        """Resume execution from the current state."""

        # --- Safety: validate required outputs exist before continuing past a stage ---
        # Only reset if we're already INTO a later stage (not just at review).
        # architecture_review means architect finished — let the checkpoint handle it.
        if state.status in ("planning", "plan_review", "executing"):
            missing_arch = self._validate_stage_outputs(self.ARCHITECT_REQUIRED_FILES)
            if missing_arch:
                console.print(
                    f"[bold yellow]Architecture files missing ({missing_arch}) "
                    f"but status is '{state.status}' — resetting to re-run architect.[/bold yellow]"
                )
                if self.event_bus:
                    self.event_bus.emit("validation.failed", stage="architect", missing_files=missing_arch)
                state.transition("initialized")
                state.save(self.project_path)

        if state.status in ("executing",) and not state.phases:
            missing_plan = self._validate_stage_outputs(self.PLANNER_REQUIRED_FILES)
            if missing_plan:
                console.print(
                    f"[bold yellow]Plan missing ({missing_plan}) and no phases in state "
                    f"— resetting to re-run planner.[/bold yellow]"
                )
                if self.event_bus:
                    self.event_bus.emit("validation.failed", stage="planner", missing_files=missing_plan)
                state.transition("planning")
                state.save(self.project_path)

        # --- ARCHITECTURE ---
        if state.status in ("initialized", "architecting"):
            await self._run_architecture(state, user_request)

        if state.status == "architecture_review":
            approved = await self._checkpoint(
                "architecture",
                "Architecture documents are ready for review.",
                [
                    self.project_path / ".ateam" / "architecture.md",
                    self.project_path / ".ateam" / "standards.md",
                    self.project_path / ".ateam" / "design.md",
                    self.project_path / ".ateam" / "tech_stack.md",
                ],
            )
            if not approved:
                console.print("[yellow]Architecture rejected. Re-running architect...[/yellow]")
                state.transition("initialized")
                state.save(self.project_path)
                await self._run_from_state(state, user_request)
                return

            state.transition("planning")
            state.save(self.project_path)

        # --- PLANNING ---
        if state.status == "planning":
            await self._run_planning(state)

        if state.status == "plan_review":
            approved = await self._checkpoint(
                "planning",
                "Project plan is ready for review.",
                [self.project_path / ".ateam" / "plan.json"],
            )
            if not approved:
                console.print("[yellow]Plan rejected. Re-running planner...[/yellow]")
                state.transition("planning")
                state.save(self.project_path)
                await self._run_from_state(state, user_request)
                return

            state.transition("executing")
            state.save(self.project_path)

        # --- EXECUTION ---
        # "failed" during execution is resumable — reset and retry
        if state.status == "failed":
            state.transition("executing")
            state.save(self.project_path)

        if state.status == "executing":
            issues = self._execution_consistency_issues(state)
            if issues:
                console.print(
                    "[bold red]Execution context is inconsistent. Refusing to continue until "
                    "state, plan, and docs are aligned.[/bold red]"
                )
                for issue in issues:
                    console.print(f"  [red]- {issue}[/red]")
                if self.event_bus:
                    self.event_bus.emit(
                        "validation.failed",
                        stage="execution_context",
                        issues=issues,
                    )
                state.transition("failed")
                state.save(self.project_path)
                return
            await self._run_execution(state)

        # --- DONE ---
        if state.status == "completed":
            console.print(
                Panel(
                    "[bold green]Project completed successfully![/bold green]\n"
                    f"Workspace: {self.project_path}",
                    title="[bold blue]A-TEAM Complete[/bold blue]",
                )
            )

    # Required outputs from each pipeline stage (minimal — only truly essential files)
    ARCHITECT_REQUIRED_FILES = ["architecture.md"]
    PLANNER_REQUIRED_FILES = ["plan.json"]

    async def _run_architecture(self, state: ProjectState, user_request: str) -> None:
        """Run the architect agent."""
        state.transition("architecting")
        state.save(self.project_path)

        console.print("\n[bold cyan]>>> Phase: Architecture[/bold cyan]")
        console.print("Running Architect agent...")

        architect = ArchitectAgent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            project_path=self.project_path,
            config=self.config,
            event_bus=self.event_bus,
        )

        result = await architect.run(user_request)

        console.print(f"[green]Architect completed.[/green] "
                      f"({result.tool_calls_made} tool calls, {result.total_tokens} tokens)")

        # ── Validate architect output ──
        missing = self._validate_stage_outputs(self.ARCHITECT_REQUIRED_FILES)
        if missing:
            console.print(
                f"[bold red]Architect failed to produce required files: "
                f"{missing}[/bold red]"
            )
            if self.event_bus:
                self.event_bus.emit(
                    "validation.failed",
                    stage="architect",
                    missing_files=missing,
                )
            # Retry once before giving up
            console.print("[yellow]Retrying architect...[/yellow]")
            result = await architect.run(user_request)
            missing = self._validate_stage_outputs(self.ARCHITECT_REQUIRED_FILES)
            if missing:
                console.print(
                    f"[bold red]Architect still missing files after retry: "
                    f"{missing}. Failing.[/bold red]"
                )
                state.transition("failed")
                state.save(self.project_path)
                return

        state.transition("architecture_review")
        state.save(self.project_path)

    async def _run_planning(self, state: ProjectState) -> None:
        """Run the planner agent."""
        console.print("\n[bold cyan]>>> Phase: Planning[/bold cyan]")
        console.print("Running Planner agent...")

        planner = PlannerAgent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            project_path=self.project_path,
            config=self.config,
            event_bus=self.event_bus,
        )

        result = await planner.run()

        console.print(f"[green]Planner completed.[/green] "
                      f"({result.tool_calls_made} tool calls, {result.total_tokens} tokens)")

        # ── Validate planner output ──
        missing = self._validate_stage_outputs(self.PLANNER_REQUIRED_FILES)
        if missing:
            console.print(
                f"[bold red]Planner failed to produce required files: {missing}[/bold red]"
            )
            if self.event_bus:
                self.event_bus.emit("validation.failed", stage="planner", missing_files=missing)
            # Retry once
            console.print("[yellow]Retrying planner...[/yellow]")
            result = await planner.run()
            missing = self._validate_stage_outputs(self.PLANNER_REQUIRED_FILES)
            if missing:
                console.print(
                    f"[bold red]Planner still missing files after retry: "
                    f"{missing}. Failing.[/bold red]"
                )
                state.transition("failed")
                state.save(self.project_path)
                return

        # Parse the plan into state
        try:
            phases = PlannerAgent.parse_plan(self.project_path)
            if not phases:
                raise ValueError("Plan parsed but contains zero phases")
            state.phases = phases
            self._print_plan(phases)
        except Exception as e:
            console.print(f"[red]Error parsing plan: {e}[/red]")
            state.transition("failed")
            state.save(self.project_path)
            return

        state.transition("plan_review")
        state.save(self.project_path)

    async def _run_execution(self, state: ProjectState) -> None:
        """Execute all phases and tasks."""
        console.print("\n[bold cyan]>>> Phase: Execution[/bold cyan]")

        # On resume, any task stuck in_progress/review was interrupted — reset to pending
        for phase in state.phases:
            for task in phase.tasks:
                if task.status in ("in_progress", "review"):
                    logger.info("Resetting interrupted task '%s' to pending", task.id)
                    task.status = "pending"
        state.save(self.project_path)

        worker = WorkerAgent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            project_path=self.project_path,
            config=self.config,
            event_bus=self.event_bus,
        )
        reviewer = ReviewerAgent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            project_path=self.project_path,
            config=self.config,
            event_bus=self.event_bus,
        )

        for phase_idx in range(state.current_phase_index, len(state.phases)):
            state.current_phase_index = phase_idx
            phase = state.phases[phase_idx]
            phase.status = "in_progress"

            console.print(f"\n[bold magenta]--- {phase.name} ---[/bold magenta]")
            console.print(f"  {phase.description}")
            if self.event_bus:
                self.event_bus.phase_started(phase.id, phase.name)

            # Process tasks based on review mode
            if self.config.review_mode == "milestones":
                await self._execute_phase_milestones(state, phase, worker, reviewer)
            else:
                await self._execute_phase_full(state, phase, worker, reviewer)

            if state.status == "failed" or phase.status == "failed":
                return

            phase.status = "completed"
            state.save(self.project_path)

            # Phase checkpoint
            if phase_idx < len(state.phases) - 1:
                approved = await self._checkpoint(
                    "phase_complete",
                    f"Phase '{phase.name}' completed. Ready for next phase?",
                    [],
                )
                if not approved:
                    console.print("[yellow]Phase not approved. Stopping.[/yellow]")
                    return

        state.transition("completed")
        state.save(self.project_path)

    async def _execute_phase_full(
        self,
        state: ProjectState,
        phase: "Phase",
        worker: WorkerAgent,
        reviewer: ReviewerAgent,
    ) -> None:
        """Execute all tasks in a phase with per-task worker→reviewer loop (or no review in yolo)."""
        pending = [t for t in phase.tasks if t.status == "pending"]
        while pending:
            # Pick the first task whose dependencies are all completed
            ready = self._next_ready_task(phase)
            if ready is None:
                # Check for deadlock
                still_pending = [t for t in phase.tasks if t.status == "pending"]
                if still_pending:
                    stuck_ids = [t.id for t in still_pending]
                    stuck_deps = {
                        t.id: [d for d in t.dependencies if d not in
                               {x.id for x in phase.tasks if x.status == "completed"}]
                        for t in still_pending
                    }
                    logger.error(
                        "DEADLOCK in phase '%s': %d tasks stuck with unresolvable "
                        "dependencies. Stuck: %s  Missing deps: %s",
                        phase.name, len(still_pending), stuck_ids, stuck_deps,
                    )
                    if self.event_bus:
                        self.event_bus.emit(
                            "phase.deadlock",
                            phase_id=phase.id,
                            phase_name=phase.name,
                            stuck_tasks=stuck_ids,
                            missing_deps=stuck_deps,
                        )
                    # Mark stuck tasks as failed — don't pretend they completed
                    for t in still_pending:
                        t.status = "rejected"
                        t.review_feedback = (
                            f"DEADLOCK: task blocked by unresolvable dependencies: "
                            f"{stuck_deps.get(t.id, [])}"
                        )
                    phase.status = "failed"
                    state.transition("failed")
                    state.save(self.project_path)
                    console.print(
                        f"[bold red]DEADLOCK:[/bold red] {len(still_pending)} task(s) in "
                        f"phase '{phase.name}' have unresolvable dependencies. "
                        f"Skipped IDs: {stuck_ids}"
                    )
                return

            if self.config.review_mode == "none":
                # Yolo — run worker only, auto-approve
                ready.status = "in_progress"
                ready.attempts = 1
                state.save(self.project_path)
                console.print(
                    f"\n  [cyan]Task:[/cyan] {ready.title} [dim](yolo — no review)[/dim]"
                )
                if self.event_bus:
                    self.event_bus.task_started(ready.id, ready.title, ready.agent_type, 1)
                completed_summary = self._completed_tasks_summary(state)
                await worker.run(
                    task=ready,
                    completed_tasks_summary=completed_summary,
                    retry_feedback=None,
                )
                ready.status = "completed"
                if self.event_bus:
                    self.event_bus.task_completed(ready.id, ready.title)
                state.save(self.project_path)
            else:
                # Full review — worker → reviewer loop
                await self._execute_task(state, ready, worker, reviewer)

            pending = [t for t in phase.tasks if t.status == "pending"]

    async def _execute_phase_milestones(
        self,
        state: ProjectState,
        phase: "Phase",
        worker: WorkerAgent,
        reviewer: ReviewerAgent,
    ) -> None:
        """Execute tasks with batch review at halfway and end of phase.

        Strategy:
        1. Run first half of tasks (workers only, no individual review).
        2. Batch-review the completed half. Re-run rejected tasks once.
        3. Run second half of tasks.
        4. Batch-review the second half. Re-run rejected tasks once.
        """
        tasks = phase.tasks
        if not tasks:
            return

        midpoint = max(1, len(tasks) // 2)
        first_half = tasks[:midpoint]
        second_half = tasks[midpoint:]

        async def _run_workers_for(batch: list) -> bool:
            """Run workers for all tasks in batch (topological order within batch)."""
            pending_ids = {t.id for t in batch if t.status == "pending"}
            while pending_ids:
                # Find ready task within this batch
                ready = None
                for t in batch:
                    if t.status != "pending":
                        continue
                    deps_done = all(
                        self._task_completed_in_state(state, dep_id)
                        for dep_id in t.dependencies
                    )
                    if deps_done:
                        ready = t
                        break

                if ready is None:
                    # Deadlock within batch
                    stuck = [t for t in batch if t.status == "pending"]
                    stuck_ids = [t.id for t in stuck]
                    stuck_deps = {
                        t.id: [d for d in t.dependencies if not
                               self._task_completed_in_state(state, d)]
                        for t in stuck
                    }
                    logger.error(
                        "DEADLOCK in milestone batch: %d tasks stuck. IDs: %s  Deps: %s",
                        len(stuck), stuck_ids, stuck_deps,
                    )
                    if self.event_bus:
                        self.event_bus.emit(
                            "phase.deadlock",
                            phase_id=phase.id,
                            phase_name=phase.name,
                            stuck_tasks=stuck_ids,
                            missing_deps=stuck_deps,
                        )
                    for t in stuck:
                        t.status = "rejected"
                        t.review_feedback = (
                            f"DEADLOCK: blocked by unresolvable dependencies: "
                            f"{stuck_deps.get(t.id, [])}"
                        )
                    phase.status = "failed"
                    state.transition("failed")
                    state.save(self.project_path)
                    console.print(
                        f"[bold red]DEADLOCK:[/bold red] {len(stuck)} task(s) have "
                        f"unresolvable dependencies. Skipped: {stuck_ids}"
                    )
                    return False

                ready.status = "in_progress"
                ready.attempts = (ready.attempts or 0) + 1
                state.save(self.project_path)

                console.print(
                    f"\n  [cyan]Task:[/cyan] {ready.title} [dim]({ready.agent_type})[/dim]"
                )
                if self.event_bus:
                    self.event_bus.task_started(ready.id, ready.title, ready.agent_type, ready.attempts)

                completed_summary = self._completed_tasks_summary(state)
                await worker.run(
                    task=ready,
                    completed_tasks_summary=completed_summary,
                    retry_feedback=ready.review_feedback if ready.attempts > 1 else None,
                )

                ready.status = "completed"
                if self.event_bus:
                    self.event_bus.task_completed(ready.id, ready.title)
                state.save(self.project_path)

                pending_ids = {t.id for t in batch if t.status == "pending"}
            return True

        async def _batch_review_and_retry(batch: list, batch_label: str) -> bool:
            """Run batch reviewer on a set of tasks; retry rejected ones once."""
            completed_batch = [t for t in batch if t.status == "completed"]
            if not completed_batch:
                return True

            console.print(
                f"\n  [bold yellow]Batch review:[/bold yellow] {batch_label} "
                f"({len(completed_batch)} tasks)"
            )

            results = await reviewer.run_batch(completed_batch, batch_label)

            rejected: list = []
            for task in completed_batch:
                res = results.get(task.id)
                if res is None:
                    continue
                task.review_feedback = res.feedback
                if res.approved:
                    console.print(f"  [green]APPROVED[/green] {task.title}")
                else:
                    console.print(f"  [red]REJECTED[/red] {task.title}: {res.feedback[:80]}")
                    task.status = "pending"
                    if self.event_bus:
                        self.event_bus.task_rejected(task.id, task.title, res.feedback)
                    rejected.append(task)

            state.save(self.project_path)

            if rejected:
                console.print(
                    f"\n  [yellow]Re-running {len(rejected)} rejected task(s) with feedback...[/yellow]"
                )
                rerun_ok = await _run_workers_for(rejected)
                if not rerun_ok:
                    return False

                # One more batch review pass on the retried tasks
                retry_results = await reviewer.run_batch(rejected, f"{batch_label}_retry")
                still_rejected: list[Task] = []
                for task in rejected:
                    res = retry_results.get(task.id)
                    if res is not None:
                        task.review_feedback = res.feedback
                    if res and res.approved:
                        task.status = "completed"
                        if self.event_bus:
                            self.event_bus.task_completed(task.id, task.title)
                        continue
                    if res and not res.approved:
                        console.print(
                            f"  [yellow]Still failing after retry — approving '{task.title}' to unblock.[/yellow]"
                        )
                    task.status = "rejected"
                    if self.event_bus:
                        self.event_bus.task_rejected(
                            task.id,
                            task.title,
                            task.review_feedback or "Task still failed after retry.",
                        )
                    still_rejected.append(task)
                    phase.status = "failed"
                    state.transition("failed")
                    state.save(self.project_path)
                    console.print(
                        f"[bold red]Batch review failed after retry.[/bold red] "
                        f"Stopping phase '{phase.name}' because '{task.title}' still failed."
                    )
                    return False

                state.save(self.project_path)

        # ── Run first half ──
        console.print(f"\n  [dim]Running first half ({len(first_half)} tasks)...[/dim]")
        first_half_ok = await _run_workers_for(first_half)
        if first_half_ok is False:
            return
        first_review_ok = await _batch_review_and_retry(first_half, f"{phase.id}_mid")
        if first_review_ok is False:
            return

        # ── Run second half ──
        if second_half:
            console.print(f"\n  [dim]Running second half ({len(second_half)} tasks)...[/dim]")
            second_half_ok = await _run_workers_for(second_half)
            if second_half_ok is False:
                return
            second_review_ok = await _batch_review_and_retry(second_half, f"{phase.id}_end")
            if second_review_ok is False:
                return

    def _validate_stage_outputs(self, required_files: list[str]) -> list[str]:
        """Check that required .ateam/ files exist and are non-empty. Returns list of missing."""
        ateam_dir = self.project_path / ".ateam"
        missing = []
        for filename in required_files:
            f = ateam_dir / filename
            if not f.exists() or f.stat().st_size == 0:
                missing.append(filename)
        return missing

    def _execution_consistency_issues(self, state: ProjectState) -> list[str]:
        """Validate that execution state, plan.json, and architecture docs still agree."""
        issues: list[str] = []
        issues.extend(self._plan_state_mismatch_issues(state))
        issues.extend(self._stack_drift_issues(state))
        return issues

    def _plan_state_mismatch_issues(self, state: ProjectState) -> list[str]:
        plan_file = self.project_path / ".ateam" / "plan.json"
        if not plan_file.exists() or not state.phases:
            return []

        try:
            plan_phases = PlannerAgent.parse_plan(self.project_path)
        except Exception as exc:
            return [f"plan.json could not be parsed before execution: {exc}"]

        if self._normalize_phase_signature(plan_phases) != self._normalize_phase_signature(state.phases):
            return [
                "state.phases does not match .ateam/plan.json. This usually means the project "
                "was re-run/reset without clearing execution state."
            ]
        return []

    def _normalize_phase_signature(self, phases: list[Phase]) -> list[dict]:
        return [
            {
                "id": phase.id,
                "name": phase.name,
                "description": phase.description,
                "tasks": [
                    {
                        "id": task.id,
                        "title": task.title,
                        "description": task.description,
                        "agent_type": task.agent_type,
                        "dependencies": list(task.dependencies),
                    }
                    for task in phase.tasks
                ],
            }
            for phase in phases
        ]

    def _stack_drift_issues(self, state: ProjectState) -> list[str]:
        """Detect obvious framework/version/style drift across docs, plan, and state."""
        sources: list[tuple[str, str]] = []
        ateam_dir = self.project_path / ".ateam"
        for name in ["architecture.md", "standards.md", "design.md", "tech_stack.md"]:
            path = ateam_dir / name
            if path.exists():
                sources.append((name, path.read_text(encoding="utf-8")))

        plan_file = ateam_dir / "plan.json"
        if plan_file.exists():
            sources.append(("plan.json", plan_file.read_text(encoding="utf-8")))

        sources.append(("state.json", state.model_dump_json(indent=2)))

        issues: list[str] = []
        version_map: dict[str, str] = {}
        style_map: dict[str, str] = {}

        for label, text in sources:
            versions = self._extract_angular_versions(text)
            if len(versions) > 1:
                issues.append(f"{label} mentions multiple Angular versions: {', '.join(sorted(versions))}")
            elif len(versions) == 1:
                version_map[label] = next(iter(versions))

            styles = self._extract_style_formats(text)
            if len(styles) > 1:
                issues.append(f"{label} mixes CSS and SCSS conventions.")
            elif len(styles) == 1:
                style_map[label] = next(iter(styles))

        unique_versions = sorted(set(version_map.values()))
        if len(unique_versions) > 1:
            details = ", ".join(f"{label}={value}" for label, value in sorted(version_map.items()))
            issues.append(f"Angular version drift detected across docs/plan/state: {details}")

        unique_styles = sorted(set(style_map.values()))
        if len(unique_styles) > 1:
            details = ", ".join(f"{label}={value}" for label, value in sorted(style_map.items()))
            issues.append(f"Style-format drift detected across docs/plan/state: {details}")

        return issues

    def _extract_angular_versions(self, text: str) -> set[str]:
        pattern = re.compile(r"\bAngular(?:\s+CLI)?\s*(?:[~v]|version)?\s*(\d{2})(?:\.\d+)?", re.IGNORECASE)
        return {match.group(1) for match in pattern.finditer(text)}

    def _extract_style_formats(self, text: str) -> set[str]:
        styles = set()
        if re.search(r"--style=scss|styles\.scss|\.component\.scss\b|\bSCSS\b", text, re.IGNORECASE):
            styles.add("scss")
        if re.search(
            r"--style=css|styles\.css|\.component\.css\b|CSS styling \(no SCSS\)|\bno SCSS\b",
            text,
            re.IGNORECASE,
        ):
            styles.add("css")
        return styles

    def _next_ready_task(self, phase: "Phase") -> "Task | None":
        """Return the first pending task whose dependencies are all completed."""
        completed_ids = {t.id for t in phase.tasks if t.status == "completed"}
        for task in phase.tasks:
            if task.status != "pending":
                continue
            if all(dep in completed_ids for dep in task.dependencies):
                return task
        return None

    def _task_completed_in_state(self, state: ProjectState, task_id: str) -> bool:
        """Check if a task is completed anywhere in the state (cross-phase dependency)."""
        for phase in state.phases:
            for task in phase.tasks:
                if task.id == task_id and task.status == "completed":
                    return True
        return False

    async def _execute_task(
        self,
        state: ProjectState,
        task: Task,
        worker: WorkerAgent,
        reviewer: ReviewerAgent,
    ) -> None:
        """Execute a single task with the worker -> reviewer loop."""
        max_retries = self.config.max_review_retries

        for attempt in range(max_retries):
            task.status = "in_progress"
            task.attempts = attempt + 1
            state.save(self.project_path)

            retry_label = f" (attempt {attempt + 1}/{max_retries})" if attempt > 0 else ""
            console.print(
                f"\n  [cyan]Task:[/cyan] {task.title}{retry_label} "
                f"[dim]({task.agent_type})[/dim]"
            )
            if self.event_bus:
                self.event_bus.task_started(task.id, task.title, task.agent_type, attempt + 1)

            # Build summary of completed tasks for context
            completed_summary = self._completed_tasks_summary(state)

            # Run worker
            worker_result = await worker.run(
                task=task,
                completed_tasks_summary=completed_summary,
                retry_feedback=task.review_feedback if attempt > 0 else None,
            )

            console.print(
                f"  [green]Worker done.[/green] "
                f"({worker_result.tool_calls_made} tool calls, {worker_result.total_tokens} tokens)"
            )

            # Run reviewer
            task.status = "review"
            state.save(self.project_path)
            console.print("  Running reviewer...")

            review = await reviewer.run(task)

            if review.approved:
                console.print(f"  [bold green]APPROVED[/bold green]: {review.feedback[:100]}")
                task.status = "completed"
                task.review_feedback = review.feedback
                if self.event_bus:
                    self.event_bus.task_completed(task.id, task.title)
                return
            else:
                console.print(f"  [bold red]REJECTED[/bold red]: {review.feedback[:100]}")
                task.status = "rejected"
                task.review_feedback = review.feedback
                if self.event_bus:
                    self.event_bus.task_rejected(task.id, task.title, review.feedback)

                if attempt < max_retries - 1:
                    console.print("  Sending back to worker with feedback...")

        # Exhausted retries — mark as completed anyway to unblock
        console.print(
            f"  [yellow]Max retries reached for '{task.title}'. "
            f"Marking task and project as failed.[/yellow]"
        )
        task.status = "rejected"
        state.transition("failed")
        state.save(self.project_path)

    async def _checkpoint(
        self, checkpoint_type: str, summary: str, files: list[Path]
    ) -> bool:
        """Request human approval at a checkpoint."""
        if checkpoint_type not in self.config.human_checkpoints:
            return True  # Checkpoint disabled, auto-approve

        if self.event_bus:
            self.event_bus.checkpoint(checkpoint_type, summary)

        approved = True
        if self.checkpoint_callback:
            approved = await self.checkpoint_callback(checkpoint_type, summary, files)

        if self.event_bus:
            self.event_bus.checkpoint_resolved(checkpoint_type, approved)

        return approved

    def _completed_tasks_summary(self, state: ProjectState) -> str:
        """Build a summary of all completed tasks across phases."""
        summaries = []
        for phase in state.phases:
            for task in phase.tasks:
                if task.status == "completed":
                    summaries.append(f"- [{phase.name}] {task.title}: {task.description[:100]}")
        return "\n".join(summaries) if summaries else ""

    def _print_plan(self, phases: list[Phase]) -> None:
        """Print the plan as a formatted table."""
        table = Table(title="Project Plan")
        table.add_column("Phase", style="bold")
        table.add_column("Task", style="cyan")
        table.add_column("Agent", style="magenta")
        table.add_column("Dependencies", style="dim")

        for phase in phases:
            for i, task in enumerate(phase.tasks):
                phase_label = phase.name if i == 0 else ""
                deps = ", ".join(task.dependencies) if task.dependencies else "-"
                table.add_row(phase_label, task.title, task.agent_type, deps)

        console.print(table)

    def _print_usage(self) -> None:
        """Print total token usage."""
        usage = self.llm_client.total_usage
        if usage.total_tokens > 0:
            console.print(
                f"\n[dim]Total tokens used: {usage.total_tokens:,} "
                f"(prompt: {usage.prompt_tokens:,}, completion: {usage.completion_tokens:,})[/dim]"
            )
