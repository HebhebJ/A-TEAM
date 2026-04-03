# A-TEAM — Technical Architecture

This document is for engineers working on the A-TEAM codebase. It covers the system design, data flow, key decisions, and module-by-module breakdown.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Layout](#2-repository-layout)
3. [Core Design Principles](#3-core-design-principles)
4. [The Orchestrator (State Machine)](#4-the-orchestrator-state-machine)
5. [Agent Architecture](#5-agent-architecture)
6. [LLM Client & Tool Calling Loop](#6-llm-client--tool-calling-loop)
7. [Tool System](#7-tool-system)
8. [State & Persistence](#8-state--persistence)
9. [Event Bus & Dashboard Streaming](#9-event-bus--dashboard-streaming)
10. [Run Modes & Review System](#10-run-modes--review-system)
11. [Dashboard Server](#11-dashboard-server)
12. [Configuration System](#12-configuration-system)
13. [Process Lifecycle](#13-process-lifecycle)
14. [Key Data Flows](#14-key-data-flows)
15. [Known Gotchas & Edge Cases](#15-known-gotchas--edge-cases)

---

## 1. System Overview

A-TEAM is a **multi-agent software development pipeline** built on a deterministic Python orchestrator. The system takes a natural language description of a software project, runs it through a sequence of specialized LLM agents, and produces working code on disk.

```
CLI / Dashboard
      │
      ▼
 Orchestrator          ← deterministic Python state machine
 (not an LLM)
      │
      ├──► ArchitectAgent   (1×)   LLM, produces design docs
      ├──► PlannerAgent     (1×)   LLM, produces plan.json
      └──► For each task:
              WorkerAgent   (N×)   LLM, writes code
              ReviewerAgent (N×)   LLM, approves or rejects
```

**Critical design choice:** The orchestrator is NOT an LLM. It is a Python `asyncio` state machine that calls LLM agents as subroutines. This gives us:
- Deterministic sequencing (no LLM deciding what to do next)
- Cheap retries (only re-run the agent that failed, not the whole pipeline)
- Full resumability (state is serialized to disk between every step)
- Testability (mock the LLM, test the orchestration logic)

### Intervention Lane

A-TEAM also supports an operator-summoned **InterventionAgent** for maintenance and repair work. The dashboard can pause the main project run, launch the intervention as a separate detached process, and record its lifecycle in `.ateam/intervention.json` plus `.ateam/intervention_history.jsonl`. Intervention runs are manual, audited, and do not automatically resume the orchestrator when they finish.

---

## 2. Repository Layout

```
A-TEAM/
├── ateam/
│   ├── __init__.py
│   ├── __main__.py              # Entry point: python -m ateam
│   ├── cli.py                   # argparse, mode wiring, checkpoint handlers
│   ├── config.py                # Config dataclass, MODES presets, layered loading
│   ├── events.py                # EventBus — appends to events.jsonl
│   │
│   ├── agents/
│   │   ├── base.py              # BaseAgent — agentic loop, tool dispatch, event emission
│   │   ├── orchestrator.py      # State machine — drives the whole pipeline
│   │   ├── architect.py         # ArchitectAgent
│   │   ├── planner.py           # PlannerAgent
│   │   ├── worker.py            # WorkerAgent (all 4 specialist types)
│   │   └── reviewer.py          # ReviewerAgent (run() + run_batch())
│   │
│   ├── llm/
│   │   ├── base.py              # LLMClient ABC
│   │   ├── openrouter.py        # OpenRouter implementation, retry logic
│   │   └── message_types.py     # Message/ToolCall/ToolResult dataclasses
│   │
│   ├── tools/
│   │   ├── base.py              # Tool ABC, ToolRegistry
│   │   ├── file_ops.py          # read_file, write_file, list_directory
│   │   ├── search.py            # search_files, search_content
│   │   ├── shell.py             # run_command (with blocklist + CI env)
│   │   └── web.py               # web_search (DuckDuckGo), fetch_url
│   │
│   ├── state/
│   │   ├── phase.py             # Phase, Task dataclasses (Pydantic v2)
│   │   └── project_state.py     # ProjectState — load/save, status transitions
│   │
│   ├── prompts/
│   │   ├── architect.md
│   │   ├── planner.md
│   │   ├── reviewer.md
│   │   ├── reviewer_batch.md    # Used by run_batch() in milestones mode
│   │   ├── worker_frontend.md
│   │   ├── worker_backend.md
│   │   ├── worker_database.md
│   │   └── worker_devops.md
│   │
│   └── dashboard/
│       ├── server.py            # FastAPI app — workspace API, SSE, launch, resume
│       └── index.html           # Single-page dashboard (vanilla JS, no build step)
│
├── config.toml                  # User-editable config
├── pyproject.toml               # Package metadata, deps, entry point
├── README.md                    # User-facing docs
└── ARCHITECTURE.md              # This file
```

Recent additions not shown in the tree above:
- `ateam/intervention.py` â€” shared helpers for `.ateam/intervention.json` and `.ateam/intervention_history.jsonl`
- `ateam/agents/intervention.py` â€” operator-summoned maintenance agent
- `ateam/prompts/intervention.md` â€” prompt contract for repair / sync / cleanup work

Intervention-specific runtime artifacts live alongside the existing `.ateam/` files:
- `.ateam/intervention.json` â€” current intervention state / lock / summary
- `.ateam/intervention_history.jsonl` â€” operator instructions and intervention results
- `.ateam/intervention_run.log` â€” detached subprocess stdout/stderr

---

## 3. Core Design Principles

### File-based everything
All state, events, logs, and artifacts live on disk as human-readable files. No database, no in-memory-only state. This enables:
- Resumability after any crash or interruption
- Dashboard observability without shared memory between processes
- Easy debugging — just open the files

### Append-only event log
`events.jsonl` is append-only newline-delimited JSON. The dashboard tails it. Agents never read it back. This keeps the event system simple and crash-safe — a partial write at the end is harmless.

### Process isolation between dashboard and workers
Dashboard processes and build processes are **separate OS processes**, communicating only via the filesystem. The dashboard spawns worker processes with `subprocess.Popen(..., start_new_session=True)` (Unix) or `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` (Windows) so they survive dashboard restarts.

### LLM agents as pure functions (mostly)
Each agent has a `run(task, ...)` method that:
1. Builds a prompt
2. Calls the LLM in a tool-calling loop
3. Returns a result object

Agents do not maintain conversational state between separate `run()` calls. Each call starts fresh with a new message history. This simplifies retries — just call `run()` again.

---

## 4. The Orchestrator (State Machine)

**File:** `ateam/agents/orchestrator.py`

The orchestrator manages a `ProjectState` (persisted as `state.json`) and transitions it through these statuses:

```
initialized
    → architecting
    → architecture_review   (checkpoint — only in standard mode)
    → planning
    → plan_review           (checkpoint — only in standard mode)
    → executing
        → [for each phase]
            → phase_review  (checkpoint — only in standard mode)
    → completed
    → failed                (on unrecoverable error)
```

`ProjectState.transition(new_status)` validates the transition is legal and writes to disk immediately.

### Resume logic

On `--resume`, the orchestrator loads the saved `state.json` and calls `_run_from_state()`:

```python
if state.status in ("architecting", "architecture_review"):
    await self._run_architecture(state)
    # fallthrough...
elif state.status in ("planning", "plan_review"):
    await self._run_planning(state)
    # fallthrough...
elif state.status in ("executing", "phase_review", "failed"):
    if state.status == "failed":
        state.transition("executing")  # reset and retry
    await self._run_execution(state)
```

On resume, any tasks stuck in `in_progress` or `review` status (from a crash mid-task) are reset to `pending` at the top of `_run_execution`.

### Phase execution — three code paths

Controlled by `config.review_mode`:

```python
if self.config.review_mode == "milestones":
    await self._execute_phase_milestones(state, phase, worker, reviewer)
else:
    await self._execute_phase_full(state, phase, worker, reviewer)
    # _execute_phase_full handles both "full" and "none" internally
```

**`_execute_phase_full`** — per-task loop:
- Picks next ready task via `_next_ready_task()` (topological ordering by dependencies)
- If `config.max_parallel > 1`, can run multiple independent ready tasks concurrently in this code path
- `review_mode == "full"`: calls `_execute_task()` which loops worker → reviewer up to `max_review_retries` times
- `review_mode == "none"`: calls worker only, auto-approves

**`_execute_phase_milestones`** — batch review:
- Splits phase tasks at the midpoint
- Runs all workers in the first half (no per-task review), still in topological order
- Calls `reviewer.run_batch()` once on the half — rejected tasks get one worker retry, then a second batch review pass
- Repeats for the second half
- Net result: 2–4 LLM reviewer calls per phase instead of N×retries
- This is the path used by both `light` and `turbo`; today it does not fan out worker execution based on `max_parallel`

**Deadlock detection:** If `_next_ready_task()` returns `None` but there are still pending tasks (circular dependency or missing dependency), the orchestrator logs a warning and force-completes the stuck tasks rather than looping forever.

---

## 5. Agent Architecture

**File:** `ateam/agents/base.py`

All agents extend `BaseAgent`:

```python
class BaseAgent:
    def __init__(self, llm_client, tool_registry, project_path, config, event_bus=None):
        ...

    async def _run_agent_loop(self, messages, tools, agent_type, task_id=None) -> AgentResult:
        """Core agentic loop: LLM → tool calls → execute → repeat."""
```

The loop:
1. Call LLM with current `messages` + available `tools`
2. If response has no tool calls → done, return
3. For each tool call: execute via `tool_registry`, append result to `messages`
4. Emit events: `agent.tool_call`, `agent.tool_result`, `tokens.update`
5. Go to step 1
6. On finish: emit `agent.completed` with iteration/token stats

**Max iterations:** Configurable, default 50. Guards against infinite loops.

### Agent subclasses

| Agent | Key behavior |
|---|---|
| `ArchitectAgent` | Writes 2 docs to `.ateam/` using `write_file`: `blueprint.md` (what to build — tech stack, architecture, data models, API routes, components) and `standards.md` (how to write it — naming, style, error handling, testing). Structured prompt guides stack selection. |
| `PlannerAgent` | Reads `blueprint.md` + `standards.md`, writes `plan.json` (machine-readable) and `plan.md` (human-readable). Output is validated: JSON parsing (with code-fence stripping), schema checks, duplicate task ID detection, dependency integrity, and circular dependency detection. Retries up to `max_planner_retries` times on validation failure. |
| `WorkerAgent` | Dispatches to the correct prompt based on `task.agent_type` (frontend/backend/database/devops). Receives the full completed-tasks summary for context. |
| `ReviewerAgent` | Has two entry points: `run(task)` for individual review and `run_batch(tasks, batch_id)` for milestone review. Both write review docs to `.ateam/reviews/`. |

### Reviewer batch parsing

`run_batch()` instructs the LLM to return a JSON block:
```json
{
  "overall": "summary",
  "tasks": [
    {"id": "task_id", "verdict": "APPROVED|REJECTED", "feedback": "..."}
  ]
}
```
`_parse_batch_review()` extracts this, returns `dict[task_id, ReviewResult]`.

---

## 6. LLM Client & Tool Calling Loop

**File:** `ateam/llm/openrouter.py`

Uses the OpenAI-compatible `/chat/completions` endpoint with `tools` parameter.

### Retry logic

```python
MAX_RETRIES = 5
RETRY_BACKOFF = [2, 4, 8, 16, 32]  # seconds
```

Retries on:
- HTTP 429 (rate limit)
- HTTP 5xx (provider error)
- HTTP 200 with error body — OpenRouter sometimes returns `{"error": {...}}` with status 200 when a provider fails. `_parse_response()` checks for this and raises `LLMAPIError` which triggers a retry.

### Tool call format

Tools are passed as OpenAI-format JSON schemas. The LLM returns tool calls in `response.choices[0].message.tool_calls`. Each tool call is executed by looking up the tool name in `ToolRegistry` and calling `tool.execute(parsed_args)`.

---

## 7. Tool System

**File:** `ateam/tools/base.py`

```python
class Tool(ABC):
    name: str
    description: str
    parameters: dict   # JSON Schema

    @abstractmethod
    async def execute(self, **kwargs) -> str: ...

class ToolRegistry:
    def register(tool: Tool) -> None
    def get(name: str) -> Tool | None
    def all_tools() -> list[Tool]
    def to_openai_format() -> list[dict]  # for LLM API call
```

### Available tools

| Tool | Notes |
|---|---|
| `read_file` | Reads any file in the project workspace |
| `write_file` | Writes/overwrites files. Creates parent dirs. |
| `list_directory` | Directory listing with file sizes |
| `search_files` | Glob pattern search across workspace |
| `search_content` | Full-text grep across workspace |
| `run_command` | Shell execution with timeout + blocklist |
| `web_search` | DuckDuckGo HTML scrape — no API key needed |
| `fetch_url` | httpx GET + HTML-to-text stripping |

### Shell tool safety

`run_command` has two protections:

1. **Server blocklist** — `_SERVER_PATTERNS` regex list. Commands matching `npm run dev`, `npm start`, `vite` (non-build invocation), `nodemon`, `--watch` etc. are rejected immediately with an error message telling the agent to use `npm run build` instead. This prevents agents from starting long-running servers that never exit.

2. **CI environment** — All subprocesses run with `CI=true` in env and `stdin=DEVNULL`. This prevents interactive installers (like `npm create vite@latest`) from prompting and hanging.

### Web tools

`WebSearchTool` scrapes DuckDuckGo HTML results — no API key, no rate limit account needed. Returns the top result snippets. Used by agents when they're stuck on an error or need to look up API docs.

`FetchUrlTool` fetches a URL with `httpx` and strips HTML tags, returning readable text. Used to follow up on search results.

---

## 8. State & Persistence

**Files:** `ateam/state/project_state.py`, `ateam/state/phase.py`

### ProjectState

Pydantic v2 model, serialized as `state.json`:

```python
class ProjectState(BaseModel):
    project_name: str
    user_request: str
    status: str                     # see status transitions above
    phases: list[Phase]
    current_phase_index: int
    tokens: TokenUsage
    created_at: datetime
    updated_at: datetime
```

`state.save(project_path)` writes atomically (write to temp file, rename). `ProjectState.load(project_path)` deserializes it. Any field added to the model is backward-compatible as long as it has a default.

### Phase & Task

```python
class Task(BaseModel):
    id: str
    title: str
    description: str
    agent_type: str          # frontend | backend | database | devops
    dependencies: list[str]  # task IDs that must complete first
    status: str              # pending | in_progress | review | completed | rejected
    attempts: int
    review_feedback: str | None

class Phase(BaseModel):
    id: str
    name: str
    description: str
    status: str
    tasks: list[Task]
```

Dependencies drive topological ordering in `_next_ready_task()`. The planner is instructed to express dependencies as task IDs; the orchestrator enforces them.

---

## 9. Event Bus & Dashboard Streaming

**File:** `ateam/events.py`

`EventBus` appends JSON lines to `.ateam/events.jsonl`. It is instantiated per project and passed into every agent so they can emit real-time events during execution.

### Event types

| Event | Payload |
|---|---|
| `project.started` | project, request |
| `status.change` | old, new |
| `phase.started` | phase_id, phase_name |
| `phase.completed` | phase_id, phase_name |
| `task.started` | task_id, title, agent_type, attempt |
| `task.completed` | task_id, title |
| `task.rejected` | task_id, title, feedback |
| `agent.started` | agent, task_id |
| `agent.tool_call` | agent, tool, args (truncated preview) |
| `agent.tool_result` | agent, tool, result (truncated preview) |
| `agent.completed` | agent, iterations, tool_calls, tokens |
| `tokens.update` | prompt, completion, total (cumulative) |
| `checkpoint` | checkpoint_type, message |
| `checkpoint.resolved` | checkpoint_type, approved |

### Dashboard SSE streaming

The dashboard's `/api/projects/{name}/events` endpoint:
1. Opens `events.jsonl`
2. Seeks to `since` byte offset (0 on first connect, resumed on reconnect)
3. Polls every 150ms for new content
4. Yields each new line as an SSE `data:` event
5. On disconnect, breaks the loop

The browser's `EventSource` auto-reconnects. On reconnect, the client passes back its last byte offset — so it only gets new events, not a full replay.

On initial page load, `GET /api/projects/{name}/state` loads the current `state.json` to populate the phase/task tree before the SSE stream catches up.

---

## 10. Run Modes & Review System

**File:** `ateam/config.py`

### Mode presets

```python
MODES = {
    "standard": {"human_checkpoints": ["architecture", "planning", "phase_complete"], "review_mode": "full",       "max_parallel": 1},
    "auto":     {"human_checkpoints": [],                                              "review_mode": "full",       "max_parallel": 1},
    "light":    {"human_checkpoints": [],                                              "review_mode": "milestones", "max_parallel": 1},
    "turbo":    {"human_checkpoints": [],                                              "review_mode": "milestones", "max_parallel": 3},
    "yolo":     {"human_checkpoints": [],                                              "review_mode": "none",       "max_parallel": 1},
}
```

`config.apply_mode(name)` sets `human_checkpoints`, `review_mode`, and `max_parallel` atomically. In practice, `turbo` currently shares the same milestone-review execution path as `light`; its main behavioral difference is the preset `max_parallel = 3`.

### Checkpoint system

Two implementations of the checkpoint callback — same signature, different mechanisms:

**Interactive (CLI):**
```python
async def checkpoint_handler(checkpoint_type, summary, files) -> bool:
    # Prints summary, prompts: a/r/q
```

**File-based (dashboard-launched processes):**
```python
async def file_checkpoint_handler(checkpoint_type, summary, files, project_path) -> bool:
    # Writes .ateam/checkpoint.json {"status": "pending", ...}
    # Polls every 2s until status changes to "approved" or "rejected"
    # Times out after 1 hour (auto-approves)
```

The dashboard detects `checkpoint` events in the SSE stream, reads `.ateam/checkpoint.json` status, shows the banner with Approve/Reject buttons. Clicking calls `POST /api/projects/{name}/checkpoint` which writes `"approved"` or `"rejected"` to the file. The polling loop in the worker process picks it up within 2s.

### Token efficiency comparison

For a project with 40 tasks across 5 phases (8 tasks/phase):

| Mode | Reviewer calls | Approx overhead |
|---|---|---|
| `full` | 40 + retries | ~40–80 extra LLM calls |
| `milestones` | 10 (2/phase × 5) | ~10–20 extra LLM calls |
| `none` | 0 | 0 |

---

## 11. Dashboard Server

**File:** `ateam/dashboard/server.py`

FastAPI application. Two globals set by the CLI before `uvicorn.run()`:

```python
WORKSPACE_DIR: Path | None = None   # root of all projects
DEFAULT_PROJECT: str | None = None  # pre-select on load (ateam dashboard <name>)
```

### API surface

| Method | Path | Description |
|---|---|---|
| GET | `/` | Serve `index.html` |
| GET | `/api/workspace` | Workspace path + default project |
| GET | `/api/projects` | List all projects (scans workspace for `.ateam/` dirs) |
| POST | `/api/run` | Launch new project subprocess |
| POST | `/api/projects/{name}/resume` | Resume interrupted project |
| POST | `/api/projects/{name}/stop` | Stop the detached runner for a project |
| PATCH | `/api/projects/{name}/mode` | Update mode in `launch.json` |
| GET | `/api/projects/{name}/state` | Read `state.json` |
| GET | `/api/projects/{name}/events` | SSE event stream |
| GET | `/api/projects/{name}/checkpoint` | Get pending checkpoint |
| POST | `/api/projects/{name}/checkpoint` | Approve/reject checkpoint |
| GET | `/api/projects/{name}/processes` | Inspect tracked runner/intervention PIDs for a project |
| POST | `/api/projects/{name}/processes/{pid}/kill` | Kill a specific project-associated PID |
| GET | `/api/projects/{name}/intervention` | Read intervention status and recent intervention history |
| POST | `/api/projects/{name}/intervention` | Pause the main run if needed and start an intervention |
| POST | `/api/projects/{name}/complete` | Manually mark a stopped project as completed |
| DELETE | `/api/projects/{name}` | Delete a stopped project workspace |
| GET | `/api/projects/{name}/logs` | List agent log files |
| GET | `/api/projects/{name}/logs/{filename}` | Read a log file |

Legacy single-project endpoints (`/api/state`, `/api/events`, etc.) are kept for backward compatibility and delegate to the per-project endpoints using `DEFAULT_PROJECT`.

### Process spawning

`POST /api/run` writes `launch.json` then calls `_spawn_detached()` in a thread executor:

```python
# Windows
kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

# Unix
kwargs["start_new_session"] = True
```

This puts the worker process in its own session/process group so it is **not killed when the dashboard stops**. The dashboard and build process communicate only through the filesystem.

### `is_running` heuristic

A project is considered running if `run.log` or `events.jsonl` was modified in the last 20 seconds. This drives the live pulse indicator in the UI. It's a heuristic — a process that has stalled writing events would appear not running after 20s. Good enough in practice.

### Frontend (`index.html`)

Single-file vanilla JS, no build step, no dependencies. Served directly by FastAPI. Key pieces:

Newer dashboard controls sit alongside the original project list / SSE / checkpoint features:
- Process inspector modal for viewing per-project runner/intervention processes and killing a stuck PID
- Intervention modal for paused repair work against project files or `.ateam` metadata
- Manual lifecycle controls for stop, finish, rerun/reset, and delete actions

- **Project list** — polls `/api/projects` every 5s, renders project cards with status chips and mode badges
- **SSE client** — `EventSource` connecting to `/api/projects/{name}/events`, auto-reconnects with exponential backoff, sends byte offset on reconnect
- **Phase tree** — built from both `state.json` (initial load) and incremental SSE events
- **Mode picker** — clicking the mode badge in the header opens a dropdown; selection calls `PATCH /api/projects/{name}/mode`
- **Checkpoint banner** — appears when a `checkpoint` SSE event arrives, polls for pending state, Approve/Reject buttons call the checkpoint API

---

## 12. Configuration System

**File:** `ateam/config.py`

Config is loaded in layers (later layers win):

```
1. Hardcoded defaults in Config dataclass
2. config.toml [llm], [orchestration], [tools] sections
3. Environment variables (OPENROUTER_API_KEY, ATEAM_MODEL, ATEAM_WORKSPACE_DIR)
4. CLI arguments (--mode, --model, --workspace, --name)
```

`Config.load()` handles all four layers. CLI arguments come in as a `cli_overrides` dict. The `mode` key is handled specially — it's popped from the dict and applied last via `apply_mode()` so the preset doesn't overwrite fine-grained CLI overrides.

---

## 13. Process Lifecycle

### CLI launch

```
ateam "build X" --mode light
  → cli.py: parse args, load config, apply mode
  → Orchestrator(config, project_name, checkpoint_callback)
  → asyncio.run(orchestrator.run(user_request))
  → ProjectState created, saved to state.json
  → EventBus created, events.jsonl opened
  → _run_architecture() → ArchitectAgent.run()
  → ... pipeline continues ...
```

### Dashboard launch

```
ateam dashboard
  → cli.py: set srv.WORKSPACE_DIR, start uvicorn
  → Browser opens http://localhost:7842
  → GET /api/projects → lists workspace
  → User clicks "+ New Project", fills form, clicks Launch
  → POST /api/run → _spawn_detached([python, -m, ateam, ..., --dashboard])
  → New OS process starts, writes to events.jsonl
  → Browser switches to project, EventSource connects to /api/projects/{name}/events
  → SSE stream tails events.jsonl, browser renders live feed
```

### Resume flow

```
User clicks "▶ Resume" on a project card
  → POST /api/projects/{name}/resume
  → server reads launch.json for mode
  → _spawn_detached([python, -m, ateam, --resume, name, --mode, mode, --dashboard])
  → New process: cli.py detects --resume, loads state.json
  → state.status is "executing" (or "failed" → reset to "executing")
  → in_progress/review tasks reset to pending
  → execution continues from where it stopped
```

### Intervention flow

```text
User clicks "Fix" / "Intervene" in the dashboard
  -> POST /api/projects/{name}/intervention
  -> server stops the normal runner first if one is active
  -> server writes .ateam/intervention.json with active=true
  -> _spawn_intervention([python, -m, ateam, --intervene, name, --instruction, ...])
  -> detached intervention process writes intervention_history.jsonl + intervention_run.log
  -> dashboard polls /api/projects/{name}/intervention for status/history
  -> operator reviews the result, then decides whether to resume the normal run
```

---

## 14. Key Data Flows

### Task execution (full review mode)

```
orchestrator._execute_task(state, task, worker, reviewer)
  task.status = "in_progress"
  state.save()
  event_bus.task_started()

  worker.run(task, completed_summary, retry_feedback)
    BaseAgent._run_agent_loop(messages, tools)
      → LLM call (with tool schemas)
      → if tool_calls: execute each, append results, loop
      → emit agent.tool_call, agent.tool_result per tool
      → emit agent.completed on finish

  reviewer.run(task)
    → LLM call: read .ateam/ docs + task output, return APPROVED/REJECTED
    → write .ateam/reviews/<task_id>_review.md

  if approved:
    task.status = "completed"
    event_bus.task_completed()
    state.save()
  else:
    task.review_feedback = feedback
    event_bus.task_rejected()
    → retry (back to worker.run with retry_feedback)
```

### Milestone batch review

```
orchestrator._execute_phase_milestones(state, phase, worker, reviewer)
  first_half = tasks[:midpoint]
  second_half = tasks[midpoint:]

  _run_workers_for(first_half)      # workers only, no review
  _batch_review_and_retry(first_half, "{phase_id}_mid")
    → reviewer.run_batch(completed_tasks, batch_id)
         → LLM reviews all tasks at once
         → returns dict[task_id, ReviewResult]
    → rejected tasks → _run_workers_for(rejected) → second batch review pass
    → still-rejected tasks force-approved to unblock

  _run_workers_for(second_half)
  _batch_review_and_retry(second_half, "{phase_id}_end")
```

---

## 15. Known Gotchas & Edge Cases

### OpenRouter 200 + error body
OpenRouter sometimes returns HTTP 200 with `{"error": {"code": ..., "message": ...}}` in the body (instead of a proper 4xx/5xx) when a provider is unavailable. `_parse_response()` explicitly checks for this and raises `LLMAPIError` so the retry loop handles it.

### Task deadlocks
If the planner creates a circular dependency (task A depends on B, B depends on A), `_next_ready_task()` returns `None` forever. The orchestrator detects this, logs a warning, and force-completes the stuck tasks. It's ugly but unblocks the run.

### Windows Ctrl+C propagation
On Windows, `Ctrl+C` sends a signal to the entire console process group. Dashboard-launched worker processes use `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` to escape this. CLI-launched processes (not via dashboard) will still die on Ctrl+C — which is the desired behavior for direct CLI use.

### events.jsonl on resume
`events.jsonl` is never cleared on resume. The dashboard replays all events from the start of the file on initial load, then streams new ones. This means the activity feed always shows the full history of a project, including previous interrupted runs. Events from different run attempts are interleaved by timestamp.

### `launch.json` missing for CLI-started projects
Projects started directly via CLI (not dashboard) have no `launch.json`. The server handles this gracefully — `/api/projects` defaults `mode` to `"auto"` for projects without `launch.json`. The first time such a project is resumed or re-run from the dashboard, a `launch.json` is created.

### File handles on Windows
Windows locks files that are open for writing. `events.jsonl` is opened in append mode per-event (not kept open), so multiple processes reading and writing it simultaneously work correctly. `state.json` is written atomically via rename.

### Dev server blocklist
The shell tool blocks commands matching `_SERVER_PATTERNS` (npm start, vite dev, nodemon, --watch, etc.) because they never exit, causing the tool call to hang until timeout, after which the agent typically retries — creating an infinite loop. If a legitimate command is blocked incorrectly, add an exclusion to `_SERVER_PATTERNS` in `tools/shell.py`.
