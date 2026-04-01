"""File operation tools: read, write, list directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tool, _resolve_safe_path


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read the contents of a file. Returns the file content as text."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file within the project",
            }
        },
        "required": ["path"],
    }

    async def execute(self, arguments: dict[str, Any], project_path: Path) -> str:
        file_path = _resolve_safe_path(arguments["path"], project_path)

        if not file_path.exists():
            return f"Error: File not found: {arguments['path']}"
        if not file_path.is_file():
            return f"Error: Not a file: {arguments['path']}"

        try:
            content = file_path.read_text(encoding="utf-8")
            if len(content) > 100_000:
                return content[:100_000] + "\n\n... [truncated, file too large]"
            return content
        except UnicodeDecodeError:
            return f"Error: Cannot read binary file: {arguments['path']}"


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or overwrite a file with the given content. Creates parent directories if needed."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file within the project",
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file",
            },
        },
        "required": ["path", "content"],
    }

    async def execute(self, arguments: dict[str, Any], project_path: Path) -> str:
        file_path = _resolve_safe_path(arguments["path"], project_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(arguments["content"], encoding="utf-8")
        return f"File written: {arguments['path']} ({len(arguments['content'])} chars)"


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List files and directories at a given path. Use recursive=true to list all nested contents."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the directory (use '.' for project root)",
                "default": ".",
            },
            "recursive": {
                "type": "boolean",
                "description": "List contents recursively",
                "default": False,
            },
        },
    }

    async def execute(self, arguments: dict[str, Any], project_path: Path) -> str:
        rel_path = arguments.get("path", ".")
        recursive = arguments.get("recursive", False)
        dir_path = _resolve_safe_path(rel_path, project_path)

        if not dir_path.exists():
            return f"Error: Directory not found: {rel_path}"
        if not dir_path.is_dir():
            return f"Error: Not a directory: {rel_path}"

        entries = []
        if recursive:
            for p in sorted(dir_path.rglob("*")):
                rel = p.relative_to(project_path)
                prefix = "d " if p.is_dir() else "f "
                entries.append(prefix + str(rel))
                if len(entries) >= 500:
                    entries.append("... [truncated, too many entries]")
                    break
        else:
            for p in sorted(dir_path.iterdir()):
                rel = p.relative_to(project_path)
                prefix = "d " if p.is_dir() else "f "
                entries.append(prefix + str(rel))

        if not entries:
            return "(empty directory)"

        return "\n".join(entries)
