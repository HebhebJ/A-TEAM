"""FastAPI dashboard server — serves the UI and streams events via SSE."""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..events import EventBus
from ..intervention import (
    read_intervention_history,
    read_intervention_state,
    write_intervention_state,
)

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
    if _launch_lock_active(project_path):
        return True
    pid = _read_pid_file(project_path)
    if pid is not None:
        return _pid_is_alive(pid)

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


def _read_text_forgiving(path: Path) -> str:
    """Read text files that may contain Windows console bytes or mixed encodings."""
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_pid_text(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _run_powershell(script: str, timeout: int = 10):
    import subprocess

    wrapped = "$ProgressPreference='SilentlyContinue'\n$ErrorActionPreference='Stop'\n" + script
    encoded = base64.b64encode(wrapped.encode("utf-16le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _compat_name() -> str:
    if DEFAULT_PROJECT:
        return DEFAULT_PROJECT
    if PROJECT_PATH:
        return PROJECT_PATH.name
    raise HTTPException(status_code=404, detail="No default project configured")


def _launch_lock_path(project_path: Path) -> Path:
    return project_path / ".ateam" / "launching.json"


def _read_pid_file(project_path: Path) -> int | None:
    pid_file = project_path / ".ateam" / "pid"
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        logger.warning("Invalid PID file for project '%s'", project_path.name)
        return None


def _read_launch_lock(project_path: Path) -> dict | None:
    lock_path = _launch_lock_path(project_path)
    if not lock_path.exists():
        return None
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        logger.warning("Invalid launch lock for project '%s'", project_path.name)
    return None


def _launch_lock_active(project_path: Path, stale_seconds: int = 120) -> bool:
    lock_path = _launch_lock_path(project_path)
    if not lock_path.exists():
        return False
    try:
        age = (
            datetime.datetime.utcnow()
            - datetime.datetime.fromtimestamp(lock_path.stat().st_mtime, datetime.timezone.utc).replace(tzinfo=None)
        ).total_seconds()
    except OSError:
        return False
    if age > stale_seconds:
        try:
            lock_path.unlink()
        except OSError:
            pass
        return False
    return True


def _acquire_launch_lock(project_path: Path, action: str) -> None:
    ateam_dir = project_path / ".ateam"
    ateam_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _launch_lock_path(project_path)

    if _launch_lock_active(project_path):
        lock = _read_launch_lock(project_path) or {}
        detail = lock.get("action") or "starting"
        raise HTTPException(
            status_code=409,
            detail=f"Project '{project_path.name}' is already {detail}. Please wait a moment.",
        )

    payload = {
        "action": action,
        "created_at": datetime.datetime.utcnow().isoformat(),
    }
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise HTTPException(
            status_code=409,
            detail=f"Project '{project_path.name}' is already starting. Please wait a moment.",
        )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def _clear_launch_lock(project_path: Path) -> None:
    try:
        _launch_lock_path(project_path).unlink()
    except OSError:
        pass


def _intervention_pid_path(project_path: Path) -> Path:
    return project_path / ".ateam" / "intervention.pid"


def _intervention_snapshot(project_path: Path) -> dict:
    state = read_intervention_state(project_path)
    pid = state.get("pid") or _read_pid_text(_intervention_pid_path(project_path))
    active = bool(state.get("active"))
    if pid and _pid_is_alive(int(pid)):
        active = True
        state["pid"] = int(pid)
    elif pid:
        state["pid"] = None
        state["active"] = False
        if state.get("status") in {"queued", "running"}:
            state["status"] = "failed"
            state["error"] = state.get("error") or "Intervention process exited unexpectedly."
            state["finished_at"] = state.get("finished_at") or datetime.datetime.utcnow().isoformat()
            write_intervention_state(project_path, state)
        try:
            _intervention_pid_path(project_path).unlink()
        except OSError:
            pass
        return state
    state["active"] = active
    return state


def _intervention_active(project_path: Path) -> bool:
    snap = _intervention_snapshot(project_path)
    return bool(snap.get("active")) and snap.get("status") in {"queued", "running"}


def _find_project_pid(project_path: Path) -> int | None:
    """Best-effort fallback for older runs that have no pid file."""
    processes = _list_project_processes(project_path)
    if processes:
        tracked = [proc for proc in processes if proc.get("tracked")]
        main = [proc for proc in processes if proc.get("kind") == "run"]
        chosen = tracked[0] if tracked else (main[0] if main else processes[0])
        return int(chosen["pid"])
    return None


def _read_project_pid(project_path: Path) -> int | None:
    pid = _read_pid_file(project_path)
    return pid if pid is not None else _find_project_pid(project_path)


def _pid_is_alive(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            import subprocess

            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0 and str(pid) in result.stdout

        import os

        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _tracked_process_count(project_path: Path) -> int:
    """Cheap process presence check for list/status endpoints."""
    pid = _read_pid_file(project_path)
    if pid is None:
        return 0
    return 1 if _pid_is_alive(pid) else 0


def _list_project_processes(project_path: Path) -> list[dict]:
    """List all A-TEAM processes that appear to belong to this project."""
    project_name = project_path.name
    workspace = str(project_path.parent)
    ps_project = project_name.replace("'", "''")
    ps_workspace = workspace.replace("'", "''")
    tracked_pid = _read_pid_file(project_path)
    processes: list[dict] = []

    try:
        if sys.platform == "win32":
            script = f"""
$project = '{ps_project}'
$workspace = '{ps_workspace}'
$items = Get-CimInstance Win32_Process -Filter "name = 'python.exe' OR name = 'pythonw.exe'" | Where-Object {{
  $_.CommandLine -and
  $_.CommandLine -match '(?:^|\\s)-m\\s+ateam(?:\\s|$)' -and
  $_.CommandLine -like "*$project*" -and
  $_.CommandLine -like "*$workspace*" -and
  $_.ProcessId -ne $PID
}} | Select-Object ProcessId, Name, CommandLine, CreationDate
$items | ConvertTo-Json -Compress
"""
            result = _run_powershell(script, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                raw = json.loads(result.stdout)
                rows = raw if isinstance(raw, list) else [raw]
                for row in rows:
                    pid = int(row["ProcessId"])
                    command = row.get("CommandLine", "")
                    processes.append({
                        "pid": pid,
                        "name": row.get("Name", "python"),
                        "command": command,
                        "created_at": row.get("CreationDate", ""),
                        "tracked": tracked_pid == pid,
                        "kind": "intervention" if "--intervene" in command else "run",
                        "alive": _pid_is_alive(pid),
                    })
        else:
            result = subprocess.run(
                ["ps", "-eo", "pid=,comm=,args="],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.strip().split(maxsplit=2)
                    if len(parts) < 3:
                        continue
                    pid_text, name, command = parts
                    if (
                        pid_text.isdigit()
                        and " -m ateam " in f" {command} "
                        and project_name in command
                        and workspace in command
                    ):
                        pid = int(pid_text)
                        processes.append({
                            "pid": pid,
                            "name": name,
                            "command": command,
                            "created_at": "",
                            "tracked": tracked_pid == pid,
                            "kind": "intervention" if "--intervene" in command else "run",
                            "alive": _pid_is_alive(pid),
                        })
    except Exception as exc:
        logger.warning("Failed to list processes for project '%s': %s", project_name, exc)

    seen: set[int] = set()
    unique: list[dict] = []
    for proc in sorted(processes, key=lambda item: item["pid"]):
        if proc["pid"] in seen:
            continue
        seen.add(proc["pid"])
        unique.append(proc)
    return unique


def _kill_process(pid: int) -> None:
    import signal
    import subprocess

    if sys.platform == "win32":
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode not in (0, 128):
            detail = (result.stderr or result.stdout or "").strip()
            raise HTTPException(status_code=500, detail=detail or f"Failed to stop PID {pid}.")
        return

    os.killpg(os.getpgid(pid), signal.SIGTERM)


def _stop_tracked_project_process(project_path: Path) -> int | None:
    pid = _read_project_pid(project_path)
    if pid is None:
        return None
    _kill_process(pid)

    pid_file = project_path / ".ateam" / "pid"
    try:
        if pid_file.exists():
            pid_file.unlink()
    except OSError:
        pass
    return pid


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
            intervention = _intervention_snapshot(d)
            projects.append({
                "name": d.name,
                "status": "intervening" if intervention.get("active") else state.get("status", "unknown"),
                "request": state.get("user_request") or launch.get("request", ""),
                "mode": launch.get("mode", "standard"),
                "launched_at": launch.get("launched_at", ""),
                "is_running": _is_running(d),
                "is_launching": _launch_lock_active(d),
                "process_count": _tracked_process_count(d),
                "is_intervening": bool(intervention.get("active")),
                "intervention_status": intervention.get("status", "idle"),
            })
    return projects


# ── Launch ───────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    request: str
    name: Optional[str] = None
    mode: str = "auto"
    reset_existing: bool = False


def _project_has_history(project_path: Path) -> bool:
    if not project_path.exists():
        return False
    try:
        return any(project_path.iterdir())
    except OSError:
        return True


def _archive_existing_project(project_path: Path) -> Path:
    """Archive an existing project directory before resetting it."""
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    archive_path = project_path.parent / f"{project_path.name}__archived_{stamp}"
    suffix = 1
    while archive_path.exists():
        archive_path = project_path.parent / f"{project_path.name}__archived_{stamp}_{suffix}"
        suffix += 1

    try:
        project_path.rename(archive_path)
    except OSError:
        shutil.copytree(project_path, archive_path)
        shutil.rmtree(project_path)

    return archive_path


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
    if project_path.exists() and _launch_lock_active(project_path):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' is already starting. Please wait a moment.",
        )
    if project_path.exists() and _intervention_active(project_path):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' is currently in intervention mode. Finish the intervention before launching.",
        )
    if project_path.exists() and _is_running(project_path):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' is already running. Stop it or use resume instead.",
        )

    archived_name = None
    if _project_has_history(project_path):
        if not body.reset_existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Project '{name}' already exists. Launch with a new name, use Resume, "
                    "or explicitly re-run/reset this project."
                ),
            )
        archived = _archive_existing_project(project_path)
        archived_name = archived.name

    project_path.mkdir(parents=True, exist_ok=True)
    ateam_dir = project_path / ".ateam"
    ateam_dir.mkdir(exist_ok=True)
    _acquire_launch_lock(project_path, "launching")
    try:
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

        # Clear stale files from a previous run
        for stale in ["checkpoint.json", "events.jsonl", "pid"]:
            sf = ateam_dir / stale
            if sf.exists():
                sf.unlink()

        cmd = [
            sys.executable, "-m", "ateam",
            body.request,
            "--name", name,
            "--mode", body.mode,
            "--workspace", str(ws),
            "--dashboard",   # use file-based checkpoint handler (no terminal input)
        ]

        asyncio.create_task(_spawn(cmd, project_path))
    except Exception:
        _clear_launch_lock(project_path)
        raise
    return {"project_name": name, "archived_project_name": archived_name}


def _spawn_detached(cmd: list[str], log_file: Path, pid_file: Path) -> None:
    """Launch the ateam process fully detached from the dashboard's process group.

    On Windows: DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP ensures the child
    is not killed when uvicorn receives Ctrl+C.
    On Unix: start_new_session=True puts the child in its own session so it
    survives the parent dying.

    Writes the child PID to pid_file so the dashboard can stop the process.
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
        proc = subprocess.Popen(
            cmd,
            stdout=f,
            stderr=f,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
        # Write PID so the dashboard can stop it later
        pid_file.write_text(str(proc.pid), encoding="utf-8")


async def _spawn(cmd: list[str], project_path: Path) -> None:
    """Async wrapper: run _spawn_detached in a thread to avoid blocking the event loop."""
    log_file = project_path / ".ateam" / "run.log"
    pid_file = project_path / ".ateam" / "pid"
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _spawn_detached, cmd, log_file, pid_file)
    except Exception as e:
        logger.error("Failed to spawn project process: %s", e)
    finally:
        _clear_launch_lock(project_path)


async def _spawn_intervention(cmd: list[str], project_path: Path) -> None:
    log_file = project_path / ".ateam" / "intervention_run.log"
    pid_file = _intervention_pid_path(project_path)
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _spawn_detached, cmd, log_file, pid_file)
        pid = _read_pid_text(pid_file)
        write_intervention_state(
            project_path,
            {
                "status": "queued",
                "active": True,
                "pid": pid,
            },
        )
    except Exception as e:
        logger.error("Failed to spawn intervention process: %s", e)
        write_intervention_state(
            project_path,
            {
                "status": "failed",
                "active": False,
                "pid": None,
                "finished_at": datetime.datetime.utcnow().isoformat(),
                "error": str(e),
            },
        )


@app.post("/api/projects/{name}/resume")
async def resume_project(name: str):
    """Resume an interrupted project, using the same mode it was originally launched with."""
    ws = _workspace()
    p = _proj(name)

    if _launch_lock_active(p):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' is already starting. Please wait a moment.",
        )
    if _intervention_active(p):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' is currently in intervention mode. Finish the intervention before resuming.",
        )
    if _is_running(p):
        return {"project_name": name, "already_running": True}

    launch = _read_json(p / ".ateam" / "launch.json", {})
    mode = launch.get("mode", "auto")

    # Clear stale files so dashboard doesn't show old state
    for stale in ["checkpoint.json", "pid"]:
        sf = p / ".ateam" / stale
        if sf.exists():
            sf.unlink()
    _acquire_launch_lock(p, "resuming")
    try:
        cmd = [
            sys.executable, "-m", "ateam",
            "--resume", name,
            "--workspace", str(ws),
            "--mode", mode,
            "--dashboard",
        ]

        asyncio.create_task(_spawn(cmd, p))
    except Exception:
        _clear_launch_lock(p)
        raise
    return {"project_name": name, "resuming": True, "mode": mode}


@app.get("/api/projects/{name}/status")
async def project_run_status(name: str):
    """Return whether a project is running, and if it crashed, the last error."""
    p = _proj(name)
    running = _is_running(p)
    pid = _read_pid_file(p)

    # Check if process is actually alive (not just recently modified)
    alive = False
    if pid:
        alive = _pid_is_alive(pid)

    # If not alive and we have a PID, it crashed — read last lines of run.log
    error_tail = ""
    if pid and not alive:
        log_file = p / ".ateam" / "run.log"
        if log_file.exists():
            try:
                lines = _read_text_forgiving(log_file).splitlines()
                # Grab last 30 lines for error context
                error_tail = "\n".join(lines[-30:])
            except OSError:
                pass

    return {
        "is_running": running,
        "is_launching": _launch_lock_active(p),
        "is_intervening": _intervention_active(p),
        "alive": alive,
        "pid": pid,
        "process_count": 1 if (pid and alive) else 0,
        "crashed": bool(pid and not alive),
        "error_tail": error_tail,
    }


@app.get("/api/projects/{name}/processes")
async def project_processes(name: str):
    """Return process and launch-lock info for a project."""
    p = _proj(name)
    return {
        "project_name": name,
        "is_launching": _launch_lock_active(p),
        "launch_lock": _read_launch_lock(p),
        "pid_file": _read_pid_file(p),
        "processes": _list_project_processes(p),
    }


@app.post("/api/projects/{name}/processes/{pid}/kill")
async def kill_project_process(name: str, pid: int):
    """Kill a specific process that belongs to a project."""
    p = _proj(name)
    known = {int(proc["pid"]) for proc in _list_project_processes(p)}
    tracked_pid = _read_pid_file(p)
    if pid not in known and pid != tracked_pid:
        raise HTTPException(status_code=404, detail=f"PID {pid} is not associated with project '{name}'.")

    try:
        _kill_process(pid)
    except ProcessLookupError:
        pass
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Failed to kill PID %d: %s", pid, e)
        raise HTTPException(status_code=500, detail=f"Failed to stop PID {pid}: {e}")

    pid_file = p / ".ateam" / "pid"
    intervention = _intervention_snapshot(p)
    if tracked_pid == pid:
        try:
            if pid_file.exists():
                pid_file.unlink()
        except OSError:
            pass
    if intervention.get("pid") == pid:
        try:
            if _intervention_pid_path(p).exists():
                _intervention_pid_path(p).unlink()
        except OSError:
            pass
        write_intervention_state(
            p,
            {
                "status": "failed",
                "active": False,
                "pid": None,
                "finished_at": datetime.datetime.utcnow().isoformat(),
                "error": "Intervention process was stopped manually.",
            },
        )

    return {"ok": True, "pid": pid, "stopped": True}


@app.post("/api/projects/{name}/stop")
async def stop_project(name: str):
    """Stop a running project by killing its process."""
    import signal

    p = _proj(name)
    pid_file = p / ".ateam" / "pid"
    discovered_pid = _read_project_pid(p)
    if discovered_pid:
        try:
            if sys.platform == "win32":
                import subprocess
                result = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(discovered_pid)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode not in (0, 128):
                    detail = (result.stderr or result.stdout or "").strip()
                    raise HTTPException(
                        status_code=500,
                        detail=detail or f"Failed to stop PID {discovered_pid}.",
                    )
            else:
                import os
                os.killpg(os.getpgid(discovered_pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Failed to kill PID %d: %s", discovered_pid, e)
            raise HTTPException(status_code=500, detail=f"Failed to stop PID {discovered_pid}: {e}")

        try:
            if pid_file.exists():
                pid_file.unlink()
        except OSError:
            pass

        return {"ok": True, "pid": discovered_pid, "stopped": True}

    if not pid_file.exists():
        raise HTTPException(status_code=400, detail="No PID file — project not launched from dashboard")

    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="Invalid PID file")

    try:
        if sys.platform == "win32":
            # Windows: use taskkill to kill the process tree
            import subprocess
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
            )
        else:
            # Unix: send SIGTERM to the process group
            import os
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        pass  # already dead
    except Exception as e:
        logger.warning("Failed to kill PID %d: %s", pid, e)

    # Clean up PID file
    try:
        pid_file.unlink()
    except OSError:
        pass

    return {"ok": True, "pid": pid, "stopped": True}


@app.post("/api/projects/{name}/complete")
async def complete_project(name: str):
    """Force-mark a stopped project as completed."""
    p = _proj(name)
    if _launch_lock_active(p):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' is still starting. Wait for launch to finish first.",
        )
    if _intervention_active(p):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' is currently in intervention mode. Finish the intervention first.",
        )
    if _is_running(p):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' still has a running process. Stop it before marking it finished.",
        )

    state_file = p / ".ateam" / "state.json"
    state = _read_json(state_file)
    if not isinstance(state, dict) or not state:
        raise HTTPException(status_code=400, detail="Project state is missing or invalid.")

    old_status = str(state.get("status", "unknown"))
    phases = state.get("phases")
    if not isinstance(phases, list):
        raise HTTPException(status_code=400, detail="Project phases are missing or invalid.")

    completed_tasks: list[tuple[str, str]] = []
    completed_phases: list[tuple[str, str]] = []

    for phase in phases:
        if not isinstance(phase, dict):
            continue
        tasks = phase.get("tasks")
        if isinstance(tasks, list):
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                if task.get("status") != "completed":
                    task["status"] = "completed"
                    completed_tasks.append(
                        (str(task.get("id", "")), str(task.get("title", "Task completed manually")))
                    )
        if phase.get("status") != "completed":
            phase["status"] = "completed"
            completed_phases.append(
                (str(phase.get("id", "")), str(phase.get("name", "Phase completed manually")))
            )

    if phases:
        state["current_phase_index"] = max(len(phases) - 1, 0)
        last_phase = phases[-1] if isinstance(phases[-1], dict) else {}
        last_tasks = last_phase.get("tasks") if isinstance(last_phase, dict) else []
        state["current_task_index"] = max(len(last_tasks) - 1, 0) if isinstance(last_tasks, list) and last_tasks else 0
    else:
        state["current_phase_index"] = 0
        state["current_task_index"] = 0

    state["status"] = "completed"
    state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    event_bus = EventBus(p)
    for task_id, title in completed_tasks:
        if task_id:
            event_bus.task_completed(task_id, title)
    for phase_id, phase_name in completed_phases:
        if phase_id:
            event_bus.phase_completed(phase_id, phase_name)
    if old_status != "completed":
        event_bus.status_change(old_status, "completed")
    event_bus.emit(
        "project.manual_completed",
        project=name,
        previous_status=old_status,
        completed_tasks=len(completed_tasks),
        completed_phases=len(completed_phases),
    )

    return {
        "ok": True,
        "project_name": name,
        "previous_status": old_status,
        "status": "completed",
        "completed_tasks": len(completed_tasks),
        "completed_phases": len(completed_phases),
    }


@app.delete("/api/projects/{name}")
async def delete_project(name: str):
    """Delete a project directory from the workspace."""
    p = _proj(name)
    if _launch_lock_active(p):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' is still starting. Wait for launch to finish first.",
        )
    if _intervention_active(p):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' is currently in intervention mode. Finish the intervention first.",
        )
    if _is_running(p):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' still has a running process. Stop it before deleting.",
        )

    try:
        shutil.rmtree(p)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete project '{name}': {exc}")

    return {"ok": True, "project_name": name, "deleted": True}


class ModeUpdate(BaseModel):
    mode: str


class InterventionRequest(BaseModel):
    instruction: str


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


@app.get("/api/projects/{name}/intervention")
async def project_intervention(name: str):
    """Return intervention status and recent chat history for a project."""
    p = _proj(name)
    return {
        "project_name": name,
        "state": _intervention_snapshot(p),
        "history": read_intervention_history(p, limit=60),
    }


@app.post("/api/projects/{name}/intervention")
async def start_intervention(name: str, body: InterventionRequest):
    """Pause the main run if needed and spawn an intervention agent run."""
    ws = _workspace()
    p = _proj(name)
    instruction = body.instruction.strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="Instruction cannot be empty.")
    if _launch_lock_active(p):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' is still starting. Wait for launch to finish before intervening.",
        )
    if _intervention_active(p):
        raise HTTPException(
            status_code=409,
            detail=f"Project '{name}' already has an active intervention run.",
        )

    interrupted_pid = None
    if _is_running(p):
        try:
            interrupted_pid = _stop_tracked_project_process(p)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to pause project before intervention: {exc}")

    write_intervention_state(
        p,
        {
            "status": "queued",
            "active": True,
            "pid": None,
            "requested_at": datetime.datetime.utcnow().isoformat(),
            "started_at": "",
            "finished_at": "",
            "last_instruction": instruction,
            "last_result": "",
            "summary": "",
            "error": "",
            "log_file": None,
            "interrupted_run": interrupted_pid is not None,
            "interrupted_pid": interrupted_pid,
        },
    )

    cmd = [
        sys.executable, "-m", "ateam",
        "--intervene", name,
        "--instruction", instruction,
        "--workspace", str(ws),
        "--dashboard",
    ]
    asyncio.create_task(_spawn_intervention(cmd, p))
    return {
        "ok": True,
        "project_name": name,
        "interrupted_run": interrupted_pid is not None,
        "interrupted_pid": interrupted_pid,
    }


# ── Per-project endpoints ─────────────────────────────────────────────────────

@app.get("/api/projects/{name}/state")
async def project_state(name: str):
    """Return the project's state.json."""
    p = _proj(name)
    data = _read_json(p / ".ateam" / "state.json", {"status": "not_started", "phases": []})
    intervention = _intervention_snapshot(p)
    if intervention.get("active"):
        data["status"] = "intervening"
    data["intervention"] = intervention
    return data


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
    for line in _read_text_forgiving(log_file).splitlines():
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
