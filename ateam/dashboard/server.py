"""FastAPI dashboard server — serves the UI and streams events via SSE."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="A-TEAM Dashboard", docs_url=None, redoc_url=None)

# Set by the CLI before starting uvicorn
WORKSPACE_DIR: Path | None = None
DEFAULT_PROJECT: str | None = None   # pre-select if launched with `ateam dashboard <project>`

# Legacy compat — old code set PROJECT_PATH directly
PROJECT_PATH: Path | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _workspace() -> Path:
    # Prefer explicit WORKSPACE_DIR; fall back to PROJECT_PATH.parent for compat
    if WORKSPACE_DIR is not None:
        return WORKSPACE_DIR
    if PROJECT_PATH is not None:
        return PROJECT_PATH.parent
    raise HTTPException(status_code=500, detail="Workspace not configured")


def _proj(name: str) -> Path:
    p = _workspace() / name
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
    return p


def _is_running(project_path: Path) -> bool:
    """Heuristic: project is 'running' if run.log OR events.jsonl was written to in the last 20s.

    run.log is written by dashboard-launched processes.
    events.jsonl is written by both CLI and dashboard processes.
    """
    import time
    threshold = 20
    for candidate in [
        project_path / ".ateam" / "run.log",
        project_path / ".ateam" / "events.jsonl",
    ]:
        if candidate.exists() and (time.time() - candidate.stat().st_mtime) < threshold:
            return True
    return False


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _compat_name() -> str:
    if DEFAULT_PROJECT:
        return DEFAULT_PROJECT
    if PROJECT_PATH:
        return PROJECT_PATH.name
    raise HTTPException(status_code=404, detail="No default project configured")


# ── Static ───────────────────────────────────────────────────────────────────

@app.get("/")
async def dashboard():
    """Serve the dashboard HTML."""
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


# ── Workspace ─────────────────────────────────────────────────────────────────

@app.get("/api/workspace")
async def workspace_info():
    """Return workspace path + default project (if any)."""
    ws = _workspace()
    return {
        "workspace": str(ws),
        "default_project": DEFAULT_PROJECT or (PROJECT_PATH.name if PROJECT_PATH else None),
    }


@app.get("/api/projects")
async def list_projects():
    """List all projects in the workspace with their current status."""
    ws = _workspace()
    projects = []
    if ws.exists():
        dirs = sorted(ws.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for d in dirs:
            if not d.is_dir() or not (d / ".ateam").exists():
                continue
            state = _read_json(d / ".ateam" / "state.json", {})
            launch = _read_json(d / ".ateam" / "launch.json", {})
            projects.append({
                "name": d.name,
                "status": state.get("status", "unknown"),
                "request": state.get("user_request") or launch.get("request", ""),
                "mode": launch.get("mode", "standard"),
                "launched_at": launch.get("launched_at", ""),
                "is_running": _is_running(d),
            })
    return projects


# ── Launch ───────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    request: str
    name: Optional[str] = None
    mode: str = "auto"


@app.post("/api/run")
async def run_project(body: RunRequest):
    """Launch a new (or re-run an existing) project as a background subprocess."""
    ws = _workspace()

    name = body.name
    if not name:
        slug = re.sub(r"[^\w\s-]", "", body.request.lower())
        slug = re.sub(r"[\s_]+", "-", slug)
        name = slug[:50].strip("-") or "project"

    project_path = ws / name
    project_path.mkdir(parents=True, exist_ok=True)
    ateam_dir = project_path / ".ateam"
    ateam_dir.mkdir(exist_ok=True)

    # Persist launch metadata so the dashboard can read mode etc.
    (ateam_dir / "launch.json").write_text(
        json.dumps({
            "request": body.request,
            "name": name,
            "mode": body.mode,
            "launched_at": datetime.datetime.utcnow().isoformat(),
        }, indent=2),
        encoding="utf-8",
    )

    # Clear any stale checkpoint from a previous run
    cp_file = ateam_dir / "checkpoint.json"
    if cp_file.exists():
        cp_file.unlink()

    cmd = [
        sys.executable, "-m", "ateam",
        body.request,
        "--name", name,
        "--mode", body.mode,
        "--workspace", str(ws),
        "--dashboard",   # use file-based checkpoint handler (no terminal input)
    ]

    asyncio.create_task(_spawn(cmd, project_path))
    return {"project_name": name}


def _spawn_detached(cmd: list[str], log_file: Path) -> None:
    """Launch the ateam process fully detached from the dashboard's process group.

    On Windows: DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP ensures the child
    is not killed when uvicorn receives Ctrl+C.
    On Unix: start_new_session=True puts the child in its own session so it
    survives the parent dying.
    """
    import subprocess

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    with open(log_file, "w", encoding="utf-8") as f:
        subprocess.Popen(
            cmd,
            stdout=f,
            stderr=f,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
        # Don't wait — process is detached and owns itself from here


async def _spawn(cmd: list[str], project_path: Path) -> None:
    """Async wrapper: run _spawn_detached in a thread to avoid blocking the event loop."""
    log_file = project_path / ".ateam" / "run.log"
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _spawn_detached, cmd, log_file)
    except Exception as e:
        logger.error("Failed to spawn project process: %s", e)


@app.post("/api/projects/{name}/resume")
async def resume_project(name: str):
    """Resume an interrupted project, using the same mode it was originally launched with."""
    ws = _workspace()
    p = _proj(name)

    if _is_running(p):
        return {"project_name": name, "already_running": True}

    launch = _read_json(p / ".ateam" / "launch.json", {})
    mode = launch.get("mode", "auto")

    # Clear any stale checkpoint so it doesn't immediately block
    cp_file = p / ".ateam" / "checkpoint.json"
    if cp_file.exists():
        cp_file.unlink()

    cmd = [
        sys.executable, "-m", "ateam",
        "--resume", name,
        "--workspace", str(ws),
        "--mode", mode,
        "--dashboard",
    ]

    asyncio.create_task(_spawn(cmd, p))
    return {"project_name": name, "resuming": True, "mode": mode}


class ModeUpdate(BaseModel):
    mode: str


@app.patch("/api/projects/{name}/mode")
async def set_project_mode(name: str, body: ModeUpdate):
    """Update the saved mode for a project (stored in .ateam/launch.json)."""
    valid = {"standard", "auto", "light", "yolo"}
    if body.mode not in valid:
        raise HTTPException(status_code=400, detail=f"mode must be one of {valid}")
    p = _proj(name)
    ateam_dir = p / ".ateam"
    ateam_dir.mkdir(exist_ok=True)
    launch_file = ateam_dir / "launch.json"
    launch = _read_json(launch_file, {})
    launch["mode"] = body.mode
    launch_file.write_text(json.dumps(launch, indent=2), encoding="utf-8")
    return {"ok": True, "mode": body.mode}


# ── Per-project endpoints ─────────────────────────────────────────────────────

@app.get("/api/projects/{name}/state")
async def project_state(name: str):
    """Return the project's state.json."""
    p = _proj(name)
    return _read_json(p / ".ateam" / "state.json", {"status": "not_started", "phases": []})


@app.get("/api/projects/{name}/events")
async def project_events(request: Request, name: str, since: int = 0):
    """Stream events from events.jsonl via Server-Sent Events."""
    p = _proj(name)
    events_file = p / ".ateam" / "events.jsonl"

    async def gen():
        offset = since
        yield {"event": "init", "data": json.dumps({"offset": offset, "project": name})}
        while True:
            if await request.is_disconnected():
                break
            if events_file.exists():
                try:
                    with open(events_file, "r", encoding="utf-8") as f:
                        f.seek(offset)
                        chunk = f.read(65536)
                        if chunk:
                            for line in chunk.splitlines():
                                line = line.strip()
                                if line:
                                    try:
                                        json.loads(line)
                                        yield {"data": line}
                                    except json.JSONDecodeError:
                                        pass
                            offset = f.tell()
                except OSError:
                    pass
            await asyncio.sleep(0.15)

    return EventSourceResponse(gen())


@app.get("/api/projects/{name}/checkpoint")
async def get_checkpoint(name: str):
    """Return the current checkpoint state for a project."""
    p = _proj(name)
    cp = _read_json(p / ".ateam" / "checkpoint.json")
    return cp or {"status": "none"}


class CheckpointResolve(BaseModel):
    approved: bool


@app.post("/api/projects/{name}/checkpoint")
async def resolve_checkpoint(name: str, body: CheckpointResolve):
    """Approve or reject the pending checkpoint for a project."""
    p = _proj(name)
    cp_file = p / ".ateam" / "checkpoint.json"
    cp = _read_json(cp_file)
    if not cp or cp.get("status") != "pending":
        raise HTTPException(status_code=400, detail="No pending checkpoint")
    cp["status"] = "approved" if body.approved else "rejected"
    cp_file.write_text(json.dumps(cp), encoding="utf-8")
    return {"ok": True}


@app.get("/api/projects/{name}/logs")
async def project_logs(name: str):
    """List agent log files for a project."""
    p = _proj(name)
    logs_dir = p / ".ateam" / "logs"
    if not logs_dir.exists():
        return []
    return sorted(f.name for f in logs_dir.glob("*.jsonl"))


@app.get("/api/projects/{name}/logs/{filename}")
async def project_log(name: str, filename: str):
    """Return a specific log file as a list of parsed entries."""
    p = _proj(name)
    log_file = p / ".ateam" / "logs" / filename
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log not found")
    entries = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


# ── Backward-compat (old single-project API) ─────────────────────────────────

@app.get("/api/state")
async def compat_state():
    return await project_state(_compat_name())


@app.get("/api/events")
async def compat_events(request: Request, since: int = 0):
    return await project_events(request, _compat_name(), since)


@app.get("/api/logs")
async def compat_logs():
    return await project_logs(_compat_name())


@app.get("/api/logs/{filename}")
async def compat_log(filename: str):
    return await project_log(_compat_name(), filename)
