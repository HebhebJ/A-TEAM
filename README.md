# A-TEAM

An agentic development system that builds software from a single sentence.

Tell it what you want to build. It spins up a team of specialized AI agents — architect, planner, developers, and a reviewer — that design, plan, and implement the project together.

```
$ ateam "a REST API for a cat adoption service"
```

Or launch and manage everything from the web dashboard:

```
$ ateam dashboard
```

---

## How It Works

A-TEAM runs a pipeline of agents, each with a specific role:

```
Your request
    │
    ▼
┌─────────────┐
│  Architect  │  Analyzes the request, chooses the stack, writes
│             │  architecture.md, standards.md, design.md, tech_stack.md
└──────┬──────┘
       │  ← checkpoint (standard mode)
       ▼
┌─────────────┐
│   Planner   │  Reads the docs, breaks the project into phases
│             │  and tasks, writes plan.json
└──────┬──────┘
       │  ← checkpoint (standard mode)
       ▼
┌──────────────────────────────────────────────┐
│  For each phase → for each task:             │
│                                              │
│   Worker Agent  →  Reviewer Agent            │
│   (implements)     (APPROVE or REJECT)       │
│        ↑                  │ rejected         │
│        └──────────────────┘  (with feedback) │
└──────────────────────────────────────────────┘
       │  ← checkpoint (standard mode)
       ▼
    Done — your project is in workspaces/<name>/
```

The **orchestrator** is a deterministic Python state machine — not an LLM. It drives the pipeline, handles checkpoints, manages retries, and saves state after every step so runs are always resumable.

---

## Agents

| Agent | Role |
|---|---|
| **Architect** | Decides the tech stack, writes architecture/design docs |
| **Planner** | Reads the docs, creates phases and tasks for worker agents |
| **Frontend Worker** | UI components, pages, routing, client state |
| **Backend Worker** | API endpoints, server logic, middleware, business logic |
| **Database Worker** | Schemas, migrations, models, seed data |
| **DevOps Worker** | Project setup, Docker, CI/CD, build configuration |
| **Reviewer** | Reviews each completed task — approves or rejects with feedback |

---

## Setup

**Requirements:** Python 3.11+

```bash
# 1. Clone and install
git clone <repo>
cd A-TEAM
pip install -e .

# 2. Add your OpenRouter API key
cp .env.example .env
# Edit .env: OPENROUTER_API_KEY=your-key-here

# 3. (Optional) Adjust config
# Edit config.toml to change model, timeouts, etc.
```

Get an API key at [openrouter.ai](https://openrouter.ai).

---

## Usage

### CLI

```bash
# Basic
ateam "a website for cats"

# Custom project name
ateam "a REST API for a cat adoption service" --name cat-api

# Choose a run mode (see Modes section)
ateam "a todo app" --mode light

# Override the LLM model
ateam "a blog" --model "openai/gpt-4o"

# Resume an interrupted run
ateam --resume cat-api

# Interactive prompt
ateam
```

### Dashboard

```bash
# Open the workspace dashboard (all projects)
ateam dashboard

# Pre-select a specific project
ateam dashboard my-project

# Custom port or workspace
ateam dashboard --port 8080 --workspace /path/to/workspaces
```

The dashboard runs at `http://localhost:7842` and lets you:
- Launch new projects with a visual form
- Watch live agent activity, tool calls, and token usage
- Browse all projects and switch between them
- Resume interrupted projects with one click
- Approve or reject checkpoints without touching the terminal
- Change the run mode per project

---

## Modes

Modes control how much human oversight and review happens during a run. Set with `--mode` on the CLI or in the dashboard when launching.

| Mode | Checkpoints | Review | Best for |
|---|---|---|---|
| `standard` | arch + plan + each phase | every task | careful projects, first runs |
| `auto` | none | every task | autonomous runs, still validates output |
| `light` | none | batch at mid + end of each phase | long projects, token-efficient |
| `yolo` | none | none | fast prototyping, throwaway code |

**Review modes explained:**
- `full` — the Reviewer agent runs after every single task, approves or rejects with feedback. Rejected tasks are re-run with that feedback (up to `max_review_retries` times).
- `milestones` — no per-task review. The Reviewer runs once at the halfway point of each phase and once at the end, reviewing the whole batch. Rejected tasks get one retry pass.
- `none` — workers run unreviewed. Tasks are auto-approved.

Modes can be changed between runs via the dashboard mode badge (click it).

---

## Project Workspace

Each generated project lives in `workspaces/<name>/`:

```
workspaces/my-project/
├── .ateam/                    # A-TEAM metadata
│   ├── state.json             # Full orchestration state (resumable)
│   ├── events.jsonl           # Event stream (dashboard reads this)
│   ├── launch.json            # Launch params: request, mode, timestamp
│   ├── run.log                # stdout/stderr from dashboard-launched runs
│   ├── checkpoint.json        # Active checkpoint (file-based, for dashboard)
│   ├── architecture.md        # Architect output
│   ├── standards.md           # Coding standards chosen by architect
│   ├── design.md              # Component-level design
│   ├── tech_stack.md          # Technology choices and rationale
│   ├── plan.json              # Machine-readable phases + tasks
│   ├── plan.md                # Human-readable plan summary
│   ├── logs/                  # Full agent conversation logs (JSONL)
│   └── reviews/               # Reviewer feedback per task / batch
└── <generated project code>
```

All intermediate artifacts are on disk and human-readable. You can edit any `.ateam/*.md` file between checkpoints to steer the agents.

---

## Configuration

`config.toml` at the project root:

```toml
[llm]
api_key_env = "OPENROUTER_API_KEY"
base_url = "https://openrouter.ai/api/v1"
default_model = "anthropic/claude-sonnet-4"

[llm.agent_models]
# Per-agent model overrides — good for cost vs quality tradeoffs
# architect = "anthropic/claude-opus-4"
# worker    = "anthropic/claude-haiku-4-5"
# reviewer  = "anthropic/claude-sonnet-4"

[orchestration]
mode = "standard"         # default mode: standard | auto | light | yolo
max_review_retries = 3    # max times a rejected task is re-run
command_timeout = 120     # seconds per shell command

[tools]
command_timeout = 120
```

Environment variables (take precedence over config.toml):

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | Your OpenRouter API key (required) |
| `ATEAM_MODEL` | Default model override |
| `ATEAM_WORKSPACE_DIR` | Where to create project workspaces |

---

## CLI Reference

```
ateam [request] [options]

Arguments:
  request               What to build (optional — prompts interactively if omitted)

Options:
  --name NAME           Project name (default: slugified from request)
  --mode MODE           Run mode: standard | auto | light | yolo  (default: standard)
  --model MODEL         LLM model to use (any OpenRouter model string)
  --workspace DIR       Workspace directory (default: ./workspaces)
  --resume NAME         Resume an interrupted project by name
  --no-checkpoints      Shorthand for --mode auto
  --dashboard           Use file-based checkpoints (set automatically by dashboard)
  -v, --verbose         Verbose logging
  -h, --help            Show help

Subcommands:
  ateam dashboard [project] [--port PORT] [--workspace DIR]
      Start the web dashboard. project is optional — shows all projects if omitted.
```

---

## Checkpoints (standard mode)

A-TEAM pauses at three points in standard mode:

1. **Architecture** — After the architect writes the docs. Edit them before continuing to steer the design.
2. **Planning** — After the planner creates the phase/task breakdown. Edit `plan.json` to change scope.
3. **Phase complete** — After each phase. Good time to run the code and verify it works.

**CLI:** `a` = approve, `r` = reject (reruns that stage), `q` = quit (resumable)

**Dashboard:** Approve/Reject buttons appear inline in the activity feed when a checkpoint is hit.
