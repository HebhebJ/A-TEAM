"""Phase and Task data models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Task(BaseModel):
    """A single task within a phase."""

    id: str = ""
    title: str = ""
    description: str = ""
    agent_type: str = ""  # "frontend", "backend", "database", "devops"
    dependencies: list[str] = Field(default_factory=list)
    status: Literal["pending", "in_progress", "review", "rejected", "completed"] = "pending"
    files_created: list[str] = Field(default_factory=list)
    review_feedback: str | None = None
    attempts: int = 0


class Phase(BaseModel):
    """A phase of work containing multiple tasks."""

    id: str = ""
    name: str = ""
    description: str = ""
    tasks: list[Task] = Field(default_factory=list)
    status: Literal["pending", "in_progress", "completed"] = "pending"

    def next_ready_task(self) -> Task | None:
        """Get the next task whose dependencies are all completed."""
        completed_ids = {t.id for t in self.tasks if t.status == "completed"}
        for task in self.tasks:
            if task.status == "pending":
                if all(dep in completed_ids for dep in task.dependencies):
                    return task
        return None

    @property
    def all_tasks_completed(self) -> bool:
        return all(t.status == "completed" for t in self.tasks)
