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
