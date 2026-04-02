"""Shared intervention state and history helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def intervention_state_path(project_path: Path) -> Path:
    return project_path / ".ateam" / "intervention.json"


def intervention_history_path(project_path: Path) -> Path:
    return project_path / ".ateam" / "intervention_history.jsonl"


def default_intervention_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "active": False,
        "pid": None,
        "requested_at": "",
        "started_at": "",
        "finished_at": "",
        "last_instruction": "",
        "last_result": "",
        "error": "",
        "summary": "",
        "log_file": None,
        "interrupted_run": False,
        "interrupted_pid": None,
        "updated_at": "",
    }


def read_intervention_state(project_path: Path) -> dict[str, Any]:
    path = intervention_state_path(project_path)
    if not path.exists():
        return default_intervention_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            state = default_intervention_state()
            state.update(data)
            return state
    except Exception:
        pass
    return default_intervention_state()


def write_intervention_state(project_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    state = read_intervention_state(project_path)
    state.update(updates)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = intervention_state_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def append_intervention_history(
    project_path: Path,
    role: str,
    content: str,
    *,
    kind: str = "message",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "kind": kind,
        "content": content,
    }
    if meta:
        entry["meta"] = meta

    path = intervention_history_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, default=str) + "\n")
    return entry


def read_intervention_history(project_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    path = intervention_history_path(project_path)
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    if limit > 0:
        return entries[-limit:]
    return entries
