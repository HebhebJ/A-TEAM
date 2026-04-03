"""Shell command tool with sandboxing."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

from .base import Tool, _resolve_safe_path

logger = logging.getLogger(__name__)


# Commands that are explicitly ALLOWED even if they might match server patterns
# (project scaffolding, one-time setup commands that complete execution)
_ALLOWED_PATTERNS = [
    r"\bnpm\s+create\b",
    r"\bnpx\s+create-",
    r"\byarn\s+create\b",
    r"\bcreate-vite\b",
    r"\bcreate-react-app\b",
    r"\bcreate-next-app\b",
    r"\bdegit\b",
    r"\bgit\s+clone\b",
]

# Patterns that indicate long-running server/watch processes that never exit
_SERVER_PATTERNS = [
    r"\bnpm\s+run\s+dev\b",
    r"\bnpm\s+start\b",
    r"\bvite\s+(dev|serve|preview)?\s*$",
    r"\bnext\s+dev\b",
    r"\bnuxt\s+dev\b",
    r"\bng\s+serve\b",
    r"\buvicorn\b",
    r"\bflask\s+run\b",
    r"\bpython\s+-m\s+http\.server\b",
    r"\bpython\s+manage\.py\s+runserver\b",
    r"\bnodemon\b",
    r"\bwatchman\b",
    r"\b--watch\b",
    r"\b--hot\b",
    r"\b--live-reload\b",
    r"\b--hmr\b",
]

_DANGEROUS_PATTERNS = [
    r"\brmdir\s+/s\s+/q\b",
    r"\brm\s+-rf\b",
    r"\bRemove-Item\b.*\b-Recurse\b.*\b-Force\b",
    r"\brobocopy\b.*\s/mir\b",
    r"\btaskkill\b",
    r"\bStop-Process\b",
    r"\bnpm\s+(?:install|i)\s+-g\b",
    r"\bpnpm\s+add\s+-g\b",
    r"\byarn\s+global\b",
]


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "Execute a shell command in the project directory. "
        "Use for installing deps, building, running tests, type-checking, linting, etc. "
        "DO NOT run long-running dev servers (npm run dev, npm start, vite, etc.) — "
        "they never exit. Use 'npm run build' to verify a project works instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "working_dir": {
                "type": "string",
                "description": "Working directory relative to project root (default '.')",
                "default": ".",
            },
        },
        "required": ["command"],
    }

    def __init__(self, timeout: int = 30):
        self._timeout = timeout

    async def execute(self, arguments: dict[str, Any], project_path: Path) -> str:
        command = arguments["command"]
        working_dir = arguments.get("working_dir", ".")
        cwd = _resolve_safe_path(working_dir, project_path)

        if not cwd.is_dir():
            return f"Error: Working directory not found: {working_dir}"

        # Allow project scaffolding commands even if they might match server patterns
        for pattern in _ALLOWED_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                logger.info("Command allowed by allowlist: %s", command)
                break
        else:
            # Reject commands that are known to run forever (dev servers, watchers)
            for pattern in _SERVER_PATTERNS:
                if re.search(pattern, command, re.IGNORECASE):
                    return (
                        "Error: This command starts a long-running server/watcher that never exits "
                        "and cannot be used here. To verify the project builds correctly, "
                        "use 'npm run build' (or equivalent) instead."
                    )

        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return (
                    "Error: This command is blocked because it is destructive or mutates the "
                    "global environment. Do not delete large directories, kill processes, "
                    "mirror-delete files, or install global packages from an agent run."
                )

        logger.info("Running command: %s (cwd: %s)", command, cwd)

        try:
            # CI=true tells npm/yarn/pnpm and most CLI tools to skip interactive prompts
            env = {**os.environ, "CI": "true"}
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return f"Error: Command timed out after {self._timeout}s"

            output_parts = []
            if stdout:
                decoded = stdout.decode("utf-8", errors="replace")
                if len(decoded) > 50_000:
                    decoded = decoded[:50_000] + "\n... [truncated]"
                output_parts.append(decoded)
            if stderr:
                decoded = stderr.decode("utf-8", errors="replace")
                if len(decoded) > 50_000:
                    decoded = decoded[:50_000] + "\n... [truncated]"
                output_parts.append(f"STDERR:\n{decoded}")

            exit_info = f"[exit code: {process.returncode}]"
            output = "\n".join(output_parts) if output_parts else "(no output)"

            return f"{output}\n{exit_info}"

        except Exception as e:
            return f"Error running command: {e}"
