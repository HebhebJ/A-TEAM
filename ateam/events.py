"""Event bus for real-time dashboard updates.

Events are written to .ateam/events.jsonl as newline-delimited JSON.
The dashboard server tails this file and streams events to the browser via SSE.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class EventBus:
    """Writes structured events to .ateam/events.jsonl for dashboard consumption."""

    def __init__(self, project_path: Path):
        self.project_path = project_path
        self._events_file = project_path / ".ateam" / "events.jsonl"
        self._events_file.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: str, **data: Any) -> None:
        """Emit an event to the events file."""
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **data,
        }
        try:
            with open(self._events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception as e:
            logger.debug("EventBus write failed: %s", e)

    # --- Convenience methods ---

    def project_started(self, project_name: str, request: str) -> None:
        self.emit("project.started", project=project_name, request=request)

    def phase_started(self, phase_id: str, phase_name: str) -> None:
        self.emit("phase.started", phase_id=phase_id, phase_name=phase_name)

    def phase_completed(self, phase_id: str, phase_name: str) -> None:
        self.emit("phase.completed", phase_id=phase_id, phase_name=phase_name)

    def task_started(self, task_id: str, title: str, agent_type: str, attempt: int) -> None:
        self.emit("task.started", task_id=task_id, title=title, agent_type=agent_type, attempt=attempt)

    def task_completed(self, task_id: str, title: str) -> None:
        self.emit("task.completed", task_id=task_id, title=title)

    def task_rejected(self, task_id: str, title: str, feedback: str) -> None:
        self.emit("task.rejected", task_id=task_id, title=title, feedback=feedback[:300])

    def agent_started(self, agent_type: str, task_id: str | None = None) -> None:
        self.emit("agent.started", agent=agent_type, task_id=task_id)

    def agent_tool_call(self, agent_type: str, tool_name: str, args_preview: str) -> None:
        self.emit("agent.tool_call", agent=agent_type, tool=tool_name, args=args_preview)

    def agent_tool_result(self, agent_type: str, tool_name: str, result_preview: str) -> None:
        self.emit("agent.tool_result", agent=agent_type, tool=tool_name, result=result_preview)

    def agent_completed(self, agent_type: str, iterations: int, tool_calls: int, tokens: int) -> None:
        self.emit(
            "agent.completed",
            agent=agent_type,
            iterations=iterations,
            tool_calls=tool_calls,
            tokens=tokens,
        )

    def tokens_update(self, prompt: int, completion: int, total: int) -> None:
        self.emit("tokens.update", prompt=prompt, completion=completion, total=total)

    def checkpoint(self, checkpoint_type: str, message: str) -> None:
        self.emit("checkpoint", checkpoint_type=checkpoint_type, message=message)

    def checkpoint_resolved(self, checkpoint_type: str, approved: bool) -> None:
        self.emit("checkpoint.resolved", checkpoint_type=checkpoint_type, approved=approved)

    def status_change(self, old_status: str, new_status: str) -> None:
        self.emit("status.change", old=old_status, new=new_status)

    # --- LLM-level events for visibility into API calls ---

    def llm_request_started(self, model: str, messages: int, tools: int) -> None:
        self.emit("llm.request_started", model=model, messages=messages, tools=tools)

    def llm_request_completed(self, model: str, tokens: int, finish_reason: str) -> None:
        self.emit("llm.request_completed", model=model, tokens=tokens, finish_reason=finish_reason)

    def llm_retry(self, error: str, wait: float, attempt: int) -> None:
        self.emit("llm.retry", error=error[:200], wait=wait, attempt=attempt)

    def llm_throttled(self, wait: float) -> None:
        self.emit("llm.throttled", wait=wait)

    # --- Progress / ETA events ---

    def progress_update(
        self,
        total_tasks: int,
        completed_tasks: int,
        current_phase: str,
        current_task: str | None = None,
        eta_seconds: float | None = None,
        avg_task_seconds: float | None = None,
    ) -> None:
        self.emit(
            "progress.update",
            total_tasks=total_tasks,
            completed_tasks=completed_tasks,
            current_phase=current_phase,
            current_task=current_task,
            eta_seconds=eta_seconds,
            avg_task_seconds=avg_task_seconds,
        )

    def project_completed(
        self,
        project_name: str,
        total_tasks: int,
        completed_tasks: int,
        total_phases: int,
        completed_phases: int,
        total_tokens: int,
        prompt_tokens: int,
        completion_tokens: int,
        total_tool_calls: int,
        total_iterations: int,
        duration_seconds: float,
        avg_task_seconds: float,
        mode: str,
    ) -> None:
        self.emit(
            "project.completed",
            project=project_name,
            total_tasks=total_tasks,
            completed_tasks=completed_tasks,
            total_phases=total_phases,
            completed_phases=completed_phases,
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tool_calls=total_tool_calls,
            total_iterations=total_iterations,
            duration_seconds=duration_seconds,
            avg_task_seconds=avg_task_seconds,
            mode=mode,
        )
