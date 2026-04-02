"""CLI interface for A-TEAM."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import logging
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from .agents.orchestrator import Orchestrator
from .agents.intervention import InterventionAgent
from .config import Config
from .events import EventBus
from .intervention import write_intervention_state
from .llm.openrouter import OpenRouterClient
from .tools.base import ToolRegistry
from .tools.file_ops import ReadFileTool, WriteFileTool, ListDirectoryTool
from .tools.search import SearchFilesTool, SearchContentTool
from .tools.shell import RunCommandTool
from .tools.web import WebSearchTool, FetchUrlTool

console = Console()


async def file_checkpoint_handler(
    checkpoint_type: str, summary: str, files: list[Path], project_path: Path
) -> bool:
    """File-based checkpoint handler for dashboard-launched runs.

    Writes a pending checkpoint to .ateam/checkpoint.json and polls
    until the dashboard user approves or rejects it (or 1 hour passes).
    """
    import functools

    cp_file = project_path / ".ateam" / "checkpoint.json"
    cp_file.write_text(
        __import__("json").dumps({
            "type": checkpoint_type,
            "summary": summary,
            "files": [str(f) for f in files],
            "status": "pending",
        }),
        encoding="utf-8",
    )

    deadline = 3600  # 1 hour timeout
    elapsed = 0
    while elapsed < deadline:
        await asyncio.sleep(2)
        elapsed += 2
        try:
            data = __import__("json").loads(cp_file.read_text(encoding="utf-8"))
            if data.get("status") == "approved":
                return True
            if data.get("status") == "rejected":
                return False
        except Exception:
            pass

    # Timed out — auto-approve to unblock
    console.print(f"[yellow]Checkpoint '{checkpoint_type}' timed out — auto-approving.[/yellow]")
    return True


async def checkpoint_handler(
    checkpoint_type: str, summary: str, files: list[Path]
) -> bool:
    """Interactive checkpoint handler — prompts user for approval."""
    console.print()
    console.print(
        Panel(
            f"[bold yellow]Checkpoint: {checkpoint_type}[/bold yellow]\n\n{summary}",
            title="[bold]Human Review Required[/bold]",
        )
    )

    # Show file contents
    for file_path in files:
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            name = file_path.name
            ext = file_path.suffix.lstrip(".")
            lexer = "json" if ext == "json" else "markdown"

            console.print(f"\n[bold]{name}:[/bold]")
            if len(content) > 5000:
                console.print(Syntax(content[:5000] + "\n... [truncated]", lexer))
            else:
                console.print(Syntax(content, lexer))

    # Prompt for decision
    while True:
        console.print()
        console.print("[bold]Options:[/bold]")
        console.print("  [green]a[/green] - Approve and continue")
        console.print("  [red]r[/red] - Reject (re-run this phase)")
        console.print("  [yellow]q[/yellow] - Quit")

        try:
            choice = input("\nYour choice [a/r/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Interrupted.[/yellow]")
            sys.exit(1)

        if choice == "a":
            console.print("[green]Approved![/green]")
            return True
        elif choice == "r":
            console.print("[red]Rejected.[/red]")
            return False
        elif choice == "q":
            console.print("[yellow]Quitting.[/yellow]")
            sys.exit(0)
        else:
            console.print("[dim]Please enter 'a', 'r', or 'q'[/dim]")


def slugify(text: str) -> str:
    """Convert text to a simple slug for project naming."""
    import re
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:50].strip("-")


def _run_dashboard(project_name: str | None, port: int, workspace: str | None) -> None:
    """Start the dashboard server. project_name is optional — shows workspace if omitted."""
    try:
        import uvicorn
        import ateam.dashboard.server as srv
        from .dashboard.server import app
    except ImportError:
        console.print("[red]Dashboard requires extra deps. Run: pip install fastapi uvicorn sse-starlette[/red]")
        sys.exit(1)

    config = Config.load(cli_overrides={"workspace_dir": Path(workspace)} if workspace else {})
    ws = config.workspace_dir.resolve()
    ws.mkdir(parents=True, exist_ok=True)

    srv.WORKSPACE_DIR = ws

    if project_name:
        project_path = ws / project_name
        if not project_path.exists():
            console.print(f"[red]Project '{project_name}' not found in {ws}[/red]")
            sys.exit(1)
        srv.DEFAULT_PROJECT = project_name
        srv.PROJECT_PATH = project_path  # backward compat

    info = f"[bold]Workspace:[/bold] {ws}\n"
    if project_name:
        info += f"[bold]Project:[/bold] {project_name}\n"
    info += f"[bold]Dashboard:[/bold] [link=http://localhost:{port}]http://localhost:{port}[/link]"

    console.print(Panel(info, title="[bold blue]A-TEAM Dashboard[/bold blue]"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


def main() -> None:
    """Main CLI entry point."""
    argv = sys.argv[1:]
    if argv and argv[0] == "dashboard":
        dash_parser = argparse.ArgumentParser(
            prog="ateam dashboard",
            description="Open the A-TEAM web dashboard",
        )
        dash_parser.add_argument("project", nargs="?", help="Project name to pre-select (optional)")
        dash_parser.add_argument("--port", type=int, default=7842, help="Port (default: 7842)")
        dash_parser.add_argument("--workspace", help="Workspace directory")
        dash_args = dash_parser.parse_args(argv[1:])
        _run_dashboard(dash_args.project, dash_args.port, dash_args.workspace)
        return

    parser = argparse.ArgumentParser(
        prog="ateam",
        description="A-TEAM: Agentic development system",
    )

    parser.add_argument(
        "request",
        nargs="?",
        help="What to build (e.g., 'a website for cats')",
    )
    parser.add_argument(
        "--name",
        help="Project name (auto-generated from request if not provided)",
    )
    parser.add_argument(
        "--model",
        help="Override the default LLM model",
    )
    parser.add_argument(
        "--mode",
        choices=["standard", "auto", "light", "yolo"],
        default=None,
        help=(
            "standard: checkpoints + review every task (default) | "
            "auto: no checkpoints + review every task | "
            "light: no checkpoints + batch review at halfway+end of each phase | "
            "yolo: no checkpoints, no review"
        ),
    )
    parser.add_argument(
        "--no-checkpoints",
        action="store_true",
        help="Disable human checkpoints (shorthand for --mode auto)",
    )
    parser.add_argument(
        "--workspace",
        help="Workspace directory (default: ./workspaces)",
    )
    parser.add_argument(
        "--resume",
        help="Resume an existing project by name",
    )
    parser.add_argument(
        "--intervene",
        help="Run the intervention agent against an existing project by name",
    )
    parser.add_argument(
        "--instruction",
        help="Operator instruction for intervention mode",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Use file-based checkpoint handler (for dashboard-launched runs)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args(argv)

    # Set up logging
    log_level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Load config
    cli_overrides = {}
    if args.model:
        cli_overrides["default_model"] = args.model
    if args.workspace:
        cli_overrides["workspace_dir"] = Path(args.workspace)
    if args.mode:
        cli_overrides["mode"] = args.mode
    elif args.no_checkpoints:
        cli_overrides["mode"] = "auto"

    config = Config.load(cli_overrides=cli_overrides)

    # Show active mode
    mode_colors = {"standard": "cyan", "auto": "green", "light": "yellow", "yolo": "red"}
    color = mode_colors.get(config.mode, "white")
    console.print(f"[dim]Mode:[/dim] [{color}]{config.mode}[/{color}]  "
                  f"[dim]Review:[/dim] [white]{config.review_mode}[/white]  "
                  f"[dim]Checkpoints:[/dim] [white]{config.human_checkpoints or 'none'}[/white]")

    # Validate API key
    if not config.openrouter_api_key:
        console.print(
            "[red]Error: OPENROUTER_API_KEY not set.[/red]\n"
            "Set it in .env file or as an environment variable."
        )
        sys.exit(1)

    # Determine project name and request
    if args.intervene:
        project_name = args.intervene
        project_path = config.workspace_dir / project_name
        if not project_path.exists():
            console.print(f"[red]Project '{project_name}' not found in {config.workspace_dir}[/red]")
            sys.exit(1)

        instruction = (args.instruction or args.request or "").strip()
        if not instruction:
            console.print(
                Panel(
                    "[bold]Intervention Mode[/bold]\n\nDescribe the repair or maintenance action you want A-TEAM to perform.",
                    title="[bold yellow]A-TEAM Intervention[/bold yellow]",
                )
            )
            try:
                instruction = input("\nIntervention instruction > ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[yellow]Goodbye![/yellow]")
                sys.exit(0)

        if not instruction:
            console.print("[red]No intervention instruction provided.[/red]")
            sys.exit(1)

        console.print(f"[yellow]Intervening on project '{project_name}'...[/yellow]")
        async def _run_intervention_once():
            llm_client = OpenRouterClient(
                api_key=config.openrouter_api_key,
                base_url=config.api_base_url,
                default_model=config.default_model,
            )
            tool_registry = ToolRegistry()
            tool_registry.register(ReadFileTool())
            tool_registry.register(WriteFileTool())
            tool_registry.register(ListDirectoryTool())
            tool_registry.register(SearchFilesTool())
            tool_registry.register(SearchContentTool())
            tool_registry.register(RunCommandTool(timeout=config.command_timeout))
            tool_registry.register(WebSearchTool())
            tool_registry.register(FetchUrlTool())
            event_bus = EventBus(project_path)
            intervention_pid = project_path / ".ateam" / "intervention.pid"

            write_intervention_state(
                project_path,
                {
                    "status": "running",
                    "active": True,
                    "pid": os.getpid(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "finished_at": "",
                    "last_instruction": instruction,
                    "error": "",
                },
            )

            try:
                agent = InterventionAgent(
                    llm_client=llm_client,
                    tool_registry=tool_registry,
                    project_path=project_path,
                    config=config,
                    event_bus=event_bus,
                )
                result = await agent.run(instruction)
                write_intervention_state(
                    project_path,
                    {
                        "status": "completed",
                        "active": False,
                        "pid": None,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "summary": result.content[:4000],
                        "log_file": result.log_file,
                    },
                )
                if intervention_pid.exists():
                    intervention_pid.unlink()
                return result
            except Exception as exc:
                event_bus.emit("intervention.failed", error=str(exc))
                write_intervention_state(
                    project_path,
                    {
                        "status": "failed",
                        "active": False,
                        "pid": None,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "error": str(exc),
                    },
                )
                if intervention_pid.exists():
                    intervention_pid.unlink()
                raise
            finally:
                try:
                    await llm_client.close()
                except Exception as close_exc:
                    logging.getLogger(__name__).warning("Intervention cleanup close failed: %s", close_exc)

        try:
            result = asyncio.run(_run_intervention_once())
            console.print(
                Panel(
                    result.content,
                    title="[bold yellow]Intervention Complete[/bold yellow]",
                )
            )
            return
        except KeyboardInterrupt:
            write_intervention_state(
                project_path,
                {
                    "status": "failed",
                    "active": False,
                    "pid": None,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": "Intervention interrupted by user",
                },
            )
            intervention_pid = project_path / ".ateam" / "intervention.pid"
            if intervention_pid.exists():
                intervention_pid.unlink()
            console.print("\n[yellow]Intervention interrupted.[/yellow]")
            sys.exit(1)
    elif args.resume:
        project_name = args.resume
        # Load existing request from state, falling back to launch.json
        project_path = config.workspace_dir / project_name
        if not project_path.exists():
            console.print(f"[red]Project '{project_name}' not found in {config.workspace_dir}[/red]")
            sys.exit(1)
        from .state.project_state import ProjectState
        state = ProjectState.load(project_path)
        user_request = state.user_request
        if not user_request:
            # Fallback: read from launch.json (dashboard-created projects)
            import json as _json
            launch_file = project_path / ".ateam" / "launch.json"
            if launch_file.exists():
                try:
                    user_request = _json.loads(launch_file.read_text(encoding="utf-8")).get("request", "")
                except Exception:
                    pass
        if not user_request:
            console.print(f"[red]Cannot resume '{project_name}': no user request found in state or launch.json[/red]")
            sys.exit(1)
        console.print(f"[cyan]Resuming project '{project_name}'...[/cyan]")
    elif args.request:
        user_request = args.request
        project_name = args.name or slugify(user_request)
    else:
        # Interactive mode
        console.print(
            Panel(
                "[bold]Welcome to A-TEAM![/bold]\n\n"
                "Describe what you want to build:",
                title="[bold blue]A-TEAM[/bold blue]",
            )
        )
        try:
            user_request = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Goodbye![/yellow]")
            sys.exit(0)

        if not user_request:
            console.print("[red]No request provided.[/red]")
            sys.exit(1)

        project_name = args.name or slugify(user_request)

    # Set up checkpoint handler
    use_dashboard_mode = getattr(args, "dashboard", False)
    if not config.human_checkpoints:
        callback = None
    elif use_dashboard_mode:
        # File-based handler for dashboard-launched subprocesses
        _project_path_for_cb = config.workspace_dir / project_name

        async def _dashboard_cb(checkpoint_type, summary, files):
            return await file_checkpoint_handler(
                checkpoint_type, summary, files, _project_path_for_cb
            )

        callback = _dashboard_cb
    else:
        callback = checkpoint_handler

    # Run orchestrator
    orchestrator = Orchestrator(
        config=config,
        project_name=project_name,
        checkpoint_callback=callback,
    )

    try:
        asyncio.run(orchestrator.run(user_request))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. State has been saved — use --resume to continue.[/yellow]")
        sys.exit(1)
