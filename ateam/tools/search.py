"""Search tools: find files by pattern, search content by regex."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from .base import Tool, _resolve_safe_path


class SearchFilesTool(Tool):
    name = "search_files"
    description = "Find files matching a glob pattern (e.g., '**/*.py', 'src/**/*.ts')."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match files against",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (relative, default '.')",
                "default": ".",
            },
        },
        "required": ["pattern"],
    }

    async def execute(self, arguments: dict[str, Any], project_path: Path) -> str:
        pattern = arguments["pattern"]
        rel_path = arguments.get("path", ".")
        search_dir = _resolve_safe_path(rel_path, project_path)

        if not search_dir.is_dir():
            return f"Error: Not a directory: {rel_path}"

        matches = []
        for p in sorted(search_dir.rglob(pattern)):
            matches.append(str(p.relative_to(project_path)))
            if len(matches) >= 200:
                matches.append("... [truncated]")
                break

        if not matches:
            return f"No files matching '{pattern}'"

        return "\n".join(matches)


class SearchContentTool(Tool):
    name = "search_content"
    description = "Search file contents for a regex pattern. Returns matching lines with file paths and line numbers."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for in file contents",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (relative, default '.')",
                "default": ".",
            },
            "file_pattern": {
                "type": "string",
                "description": "Optional glob to filter which files to search (e.g., '*.py')",
            },
        },
        "required": ["pattern"],
    }

    async def execute(self, arguments: dict[str, Any], project_path: Path) -> str:
        pattern = arguments["pattern"]
        rel_path = arguments.get("path", ".")
        file_pattern = arguments.get("file_pattern")
        search_dir = _resolve_safe_path(rel_path, project_path)

        if not search_dir.is_dir():
            return f"Error: Not a directory: {rel_path}"

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: Invalid regex '{pattern}': {e}"

        results = []
        glob_pattern = file_pattern or "*"

        for file_path in sorted(search_dir.rglob(glob_pattern)):
            if not file_path.is_file():
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue

            for line_num, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    rel = file_path.relative_to(project_path)
                    results.append(f"{rel}:{line_num}: {line.rstrip()}")
                    if len(results) >= 100:
                        results.append("... [truncated, too many matches]")
                        return "\n".join(results)

        if not results:
            return f"No matches for '{pattern}'"

        return "\n".join(results)
