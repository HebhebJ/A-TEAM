"""Tool protocol and registry for agent function calling."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Tool(ABC):
    """Base class for tools that agents can call."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for parameters

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], project_path: Path) -> str:
        """Execute the tool with the given arguments.

        Args:
            arguments: Parsed arguments matching self.parameters schema.
            project_path: Root path of the project workspace.

        Returns:
            String result to send back to the LLM.
        """
        ...

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function-calling tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Registry of available tools."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_schemas(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Get OpenAI-format tool schemas, optionally filtered by name."""
        tools = self._tools.values()
        if names is not None:
            tools = [t for t in tools if t.name in names]
        return [t.to_openai_schema() for t in tools]

    async def execute(
        self, name: str, arguments: dict[str, Any], project_path: Path
    ) -> str:
        """Execute a tool by name."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: Unknown tool '{name}'"

        try:
            result = await tool.execute(arguments, project_path)
            logger.debug("Tool %s executed successfully", name)
            return result
        except Exception as e:
            logger.error("Tool %s failed: %s", name, e)
            return f"Error executing {name}: {e}"

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())


def _resolve_safe_path(relative_path: str, project_path: Path) -> Path:
    """Resolve a path safely within the project directory.

    Prevents path traversal attacks (../../etc).
    """
    resolved = (project_path / relative_path).resolve()
    project_resolved = project_path.resolve()

    if not str(resolved).startswith(str(project_resolved)):
        raise ValueError(
            f"Path '{relative_path}' resolves outside project directory"
        )

    return resolved
