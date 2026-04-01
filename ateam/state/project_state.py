"""Project state persistence — JSON-backed."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .phase import Phase


class ProjectState(BaseModel):
    """Full state of a project being built by A-TEAM."""

    project_name: str = ""
    user_request: str = ""
    status: Literal[
        "initialized",
        "architecting",
        "architecture_review",
        "planning",
        "plan_review",
        "executing",
        "phase_review",
        "completed",
        "failed",
    ] = "initialized"
    current_phase_index: int = 0
    current_task_index: int = 0
    phases: list[Phase] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def transition(self, new_status: str) -> None:
        """Transition to a new state and update timestamp."""
        self.status = new_status  # type: ignore[assignment]
        self.updated_at = datetime.now(timezone.utc).isoformat()

    @property
    def current_phase(self) -> Phase | None:
        if 0 <= self.current_phase_index < len(self.phases):
            return self.phases[self.current_phase_index]
        return None

    def save(self, project_path: Path) -> None:
        """Save state to .ateam/state.json."""
        state_dir = project_path / ".ateam"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / "state.json"
        state_file.write_text(
            self.model_dump_json(indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, project_path: Path) -> ProjectState:
        """Load state from .ateam/state.json, or return a new state."""
        state_file = project_path / ".ateam" / "state.json"
        if state_file.exists():
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return cls.model_validate(data)
        return cls()

    @classmethod
    def create(cls, project_name: str, user_request: str) -> ProjectState:
        """Create a new project state."""
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            project_name=project_name,
            user_request=user_request,
            status="initialized",
            created_at=now,
            updated_at=now,
        )
