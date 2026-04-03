"""Unit tests for plan.json validation in PlannerAgent."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from ateam.agents.planner import (
    PlannerAgent,
    _strip_code_fences,
    _validate_phase_schema,
    _check_no_cycles,
)
from ateam.state.phase import Phase, Task


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temporary project directory with .ateam/."""
    ateam = tmp_path / ".ateam"
    ateam.mkdir()
    return tmp_path


# ── _strip_code_fences ────────────────────────────────────────────────────────

class TestStripCodeFences:
    def test_plain_json_unchanged(self):
        text = '{"phases": []}'
        assert _strip_code_fences(text) == text

    def test_json_fence_stripped(self):
        text = '```json\n{"phases": []}\n```'
        result = _strip_code_fences(text)
        assert result == '{"phases": []}'

    def test_plain_fence_stripped(self):
        text = '```\n{"phases": []}\n```'
        result = _strip_code_fences(text)
        assert result == '{"phases": []}'

    def test_no_trailing_newline(self):
        text = '```json\n{"x": 1}\n```'
        result = _strip_code_fences(text)
        assert result == '{"x": 1}'

    def test_partial_fence_unchanged(self):
        text = '```json\n{"x": 1}'
        assert _strip_code_fences(text) == text


# ── _validate_phase_schema ────────────────────────────────────────────────────

class TestValidatePhaseSchema:
    def test_valid_phase(self):
        _validate_phase_schema({"id": "p1", "name": "Setup", "tasks": [{"id": "t1"}]}, 0)

    def test_missing_id(self):
        with pytest.raises(ValueError, match="missing required keys"):
            _validate_phase_schema({"name": "Setup", "tasks": []}, 0)

    def test_missing_name(self):
        with pytest.raises(ValueError, match="missing required keys"):
            _validate_phase_schema({"id": "p1", "tasks": []}, 0)

    def test_missing_tasks(self):
        with pytest.raises(ValueError, match="missing required keys"):
            _validate_phase_schema({"id": "p1", "name": "Setup"}, 0)

    def test_tasks_not_list(self):
        with pytest.raises(ValueError, match="'tasks' must be an array"):
            _validate_phase_schema({"id": "p1", "name": "Setup", "tasks": "bad"}, 0)

    def test_empty_tasks(self):
        with pytest.raises(ValueError, match="has no tasks"):
            _validate_phase_schema({"id": "p1", "name": "Setup", "tasks": []}, 0)


# ── _check_no_cycles ──────────────────────────────────────────────────────────

class TestCheckNoCycles:
    def test_no_cycles(self):
        tasks = [
            Task(id="t1", title="A", description="", agent_type="backend", dependencies=[]),
            Task(id="t2", title="B", description="", agent_type="backend", dependencies=["t1"]),
        ]
        phases = [Phase(id="p1", name="P", description="", tasks=tasks)]
        _check_no_cycles({"t1", "t2"}, phases)  # should not raise

    def test_self_cycle(self):
        tasks = [
            Task(id="t1", title="A", description="", agent_type="backend", dependencies=["t1"]),
        ]
        phases = [Phase(id="p1", name="P", description="", tasks=tasks)]
        with pytest.raises(ValueError, match="Circular dependency"):
            _check_no_cycles({"t1"}, phases)

    def test_two_node_cycle(self):
        tasks = [
            Task(id="t1", title="A", description="", agent_type="backend", dependencies=["t2"]),
            Task(id="t2", title="B", description="", agent_type="backend", dependencies=["t1"]),
        ]
        phases = [Phase(id="p1", name="P", description="", tasks=tasks)]
        with pytest.raises(ValueError, match="Circular dependency"):
            _check_no_cycles({"t1", "t2"}, phases)

    def test_three_node_cycle(self):
        tasks = [
            Task(id="t1", title="A", description="", agent_type="backend", dependencies=["t3"]),
            Task(id="t2", title="B", description="", agent_type="backend", dependencies=["t1"]),
            Task(id="t3", title="C", description="", agent_type="backend", dependencies=["t2"]),
        ]
        phases = [Phase(id="p1", name="P", description="", tasks=tasks)]
        with pytest.raises(ValueError, match="Circular dependency"):
            _check_no_cycles({"t1", "t2", "t3"}, phases)


# ── PlannerAgent.parse_plan ───────────────────────────────────────────────────

VALID_PLAN = {
    "phases": [
        {
            "id": "phase_1",
            "name": "Setup",
            "description": "Project setup",
            "tasks": [
                {
                    "id": "phase1_task1",
                    "title": "Init project",
                    "description": "Create project structure",
                    "agent_type": "devops",
                    "dependencies": [],
                }
            ],
        }
    ]
}


class TestParsePlan:
    def test_valid_plan(self, tmp_project: Path):
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text(json.dumps(VALID_PLAN), encoding="utf-8")

        phases = PlannerAgent.parse_plan(tmp_project)
        assert len(phases) == 1
        assert phases[0].id == "phase_1"
        assert len(phases[0].tasks) == 1
        assert phases[0].tasks[0].id == "phase1_task1"

    def test_valid_plan_with_code_fences(self, tmp_project: Path):
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text(f'```json\n{json.dumps(VALID_PLAN)}\n```', encoding="utf-8")

        phases = PlannerAgent.parse_plan(tmp_project)
        assert len(phases) == 1

    def test_missing_file(self, tmp_project: Path):
        with pytest.raises(FileNotFoundError, match="plan.json not found"):
            PlannerAgent.parse_plan(tmp_project)

    def test_invalid_json(self, tmp_project: Path):
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text("{bad json", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            PlannerAgent.parse_plan(tmp_project)

    def test_missing_phases_key(self, tmp_project: Path):
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text('{"other": []}', encoding="utf-8")
        with pytest.raises(ValueError, match="must have a 'phases' key"):
            PlannerAgent.parse_plan(tmp_project)

    def test_empty_phases(self, tmp_project: Path):
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text('{"phases": []}', encoding="utf-8")
        with pytest.raises(ValueError, match="'phases' is empty"):
            PlannerAgent.parse_plan(tmp_project)

    def test_duplicate_task_ids(self, tmp_project: Path):
        plan = {
            "phases": [
                {
                    "id": "p1",
                    "name": "P1",
                    "description": "",
                    "tasks": [
                        {"id": "t1", "title": "A", "description": "d", "agent_type": "backend", "dependencies": []},
                    ],
                },
                {
                    "id": "p2",
                    "name": "P2",
                    "description": "",
                    "tasks": [
                        {"id": "t1", "title": "B", "description": "d", "agent_type": "frontend", "dependencies": []},
                    ],
                },
            ]
        }
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(ValueError, match="Duplicate task ID"):
            PlannerAgent.parse_plan(tmp_project)

    def test_invalid_agent_type(self, tmp_project: Path):
        plan = {
            "phases": [
                {
                    "id": "p1",
                    "name": "P1",
                    "description": "",
                    "tasks": [
                        {"id": "t1", "title": "A", "description": "d", "agent_type": "magic", "dependencies": []},
                    ],
                }
            ]
        }
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(ValueError, match="invalid agent_type"):
            PlannerAgent.parse_plan(tmp_project)

    def test_broken_dependency(self, tmp_project: Path):
        plan = {
            "phases": [
                {
                    "id": "p1",
                    "name": "P1",
                    "description": "",
                    "tasks": [
                        {"id": "t1", "title": "A", "description": "d", "agent_type": "backend", "dependencies": ["nonexistent"]},
                    ],
                }
            ]
        }
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(ValueError, match="does not exist"):
            PlannerAgent.parse_plan(tmp_project)

    def test_circular_dependency(self, tmp_project: Path):
        plan = {
            "phases": [
                {
                    "id": "p1",
                    "name": "P1",
                    "description": "",
                    "tasks": [
                        {"id": "t1", "title": "A", "description": "d", "agent_type": "backend", "dependencies": ["t2"]},
                        {"id": "t2", "title": "B", "description": "d", "agent_type": "backend", "dependencies": ["t1"]},
                    ],
                }
            ]
        }
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(ValueError, match="Circular dependency"):
            PlannerAgent.parse_plan(tmp_project)

    def test_default_agent_type(self, tmp_project: Path):
        plan = {
            "phases": [
                {
                    "id": "p1",
                    "name": "P1",
                    "description": "",
                    "tasks": [
                        {"id": "t1", "title": "A", "description": "d", "dependencies": []},
                    ],
                }
            ]
        }
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        phases = PlannerAgent.parse_plan(tmp_project)
        assert phases[0].tasks[0].agent_type == "backend"

    def test_multi_phase_plan(self, tmp_project: Path):
        plan = {
            "phases": [
                {
                    "id": "p1",
                    "name": "Setup",
                    "description": "",
                    "tasks": [
                        {"id": "t1", "title": "Init", "description": "d", "agent_type": "devops", "dependencies": []},
                    ],
                },
                {
                    "id": "p2",
                    "name": "Backend",
                    "description": "",
                    "tasks": [
                        {"id": "t2", "title": "API", "description": "d", "agent_type": "backend", "dependencies": ["t1"]},
                    ],
                },
            ]
        }
        plan_file = tmp_project / ".ateam" / "plan.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        phases = PlannerAgent.parse_plan(tmp_project)
        assert len(phases) == 2
        assert phases[0].tasks[0].id == "t1"
        assert phases[1].tasks[0].id == "t2"