# A-TEAM: Agentic Development System

## Context
Multi-agent system where an orchestrator spawns specialized AI agents (architect, planner, workers, reviewer, intervention) to collaboratively build software projects from a high-level user request. LLM calls go through OpenRouter API. Agents communicate via files. Human checkpoints at key stages. A real-time web dashboard provides live visibility and control.

## Architecture

### Agent Flow
```
User Request -> Orchestrator (Python state machine, NOT an LLM)
  -> Architect Agent (writes architecture.md, standards.md, design.md, tech_stack.md)
  -> CHECKPOINT: human approval
  -> Planner Agent (reads docs, writes plan.json with phases/tasks)
  -> CHECKPOINT: human approval
  -> For each phase:
       For each task (topological order):
         -> Worker Agent (frontend/backend/database/devops)
         -> Reviewer Agent (APPROVE or REJECT)
         -> If REJECT: loop back to worker with feedback (max 3 retries)
       -> CHECKPOINT: human approval between phases
  -> Done

At any point: Intervention Agent can be triggered to pause the pipeline,
  accept a repair instruction, and resume.
```

### Key Design Decisions
- **Orchestrator is deterministic Python**, not an LLM agent -- predictable, cheap, debuggable
- **File-based state** -- resumable, inspectable, human-editable
- **Single BaseAgent class** -- all agents share the same tool-calling loop, differ only in prompt + allowed tools
- **OpenRouter with OpenAI-compatible format** -- easy to swap providers later
- **Event bus** -- all agent activity emitted to `.ateam/events.jsonl`; dashboard streams these via SSE

## Project Structure
```
A-TEAM/
├── pyproject.toml
├── config.toml
├── .env                        # OPENROUTER_API_KEY
├── .gitignore
├── PLAN.md                     # This file
├── ateam/
│   ├── __init__.py
│   ├── __main__.py             # python -m ateam
│   ├── cli.py                  # CLI interface
│   ├── config.py               # Config loading (env + toml + CLI)
│   ├── events.py               # EventBus — writes structured events to events.jsonl
│   ├── intervention.py         # Intervention state helpers
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── base.py             # LLMClient protocol
│   │   ├── openrouter.py       # OpenRouter implementation (streams events via EventBus)
│   │   └── message_types.py    # Message, ToolCall, LLMResponse dataclasses
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── base.py             # BaseAgent (agentic tool-calling loop)
│   │   ├── orchestrator.py     # State machine driving the pipeline
│   │   ├── architect.py        # Architect agent
│   │   ├── planner.py          # Planner agent
│   │   ├── worker.py           # Worker agent (parameterized by specialty)
│   │   ├── reviewer.py         # Reviewer agent
│   │   └── intervention.py     # Intervention agent (repair + resume)
│   ├── prompts/
│   │   ├── architect.md
│   │   ├── planner.md
│   │   ├── worker_frontend.md
│   │   ├── worker_backend.md
│   │   ├── worker_database.md
│   │   ├── worker_devops.md
│   │   ├── reviewer.md
│   │   ├── reviewer_batch.md
│   │   └── intervention.md
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── base.py             # Tool protocol + ToolRegistry
│   │   ├── file_ops.py         # read_file, write_file, list_directory
│   │   ├── search.py           # search_files, search_content
│   │   ├── shell.py            # run_command (sandboxed, timeout)
│   │   └── web.py              # fetch_url, web_search
│   ├── state/
│   │   ├── __init__.py
│   │   ├── project_state.py    # ProjectState (JSON-backed persistence)
│   │   └── phase.py            # Phase, Task dataclasses
│   └── dashboard/
│       ├── __init__.py
│       ├── server.py           # FastAPI server — project mgmt + SSE event streaming
│       └── index.html          # Single-file SPA dashboard
└── workspaces/                 # Generated projects go here
    └── <project>/
        ├── .ateam/             # Metadata: state.json, events.jsonl, logs/, docs/
        └── <generated source>
```

## Dashboard

Web UI served by `dashboard/server.py` (FastAPI). Run alongside the CLI or standalone.

### Features
- **Projects panel** — list, create, launch, delete projects; live status chips
- **Plan tab** — phase/task tree with status icons and agent-type badges
- **Live Activity feed** — real-time SSE stream of all agent events (tool calls, LLM requests, task progress, checkpoints)
- **Visual Office mode** — toggle from the Live Activity header; renders a top-down 2D office floor:
  - Dark tiled floor background; fixed desk row for Architect / Planner / Reviewer, dynamic worker bay below
  - Each station: person avatar (head + shoulders, colored by role) sitting at a wood-grain desk with monitor + keyboard
  - Idle → avatar fades out; Working → avatar bounces (typing), monitor glows + scanlines, status pip pulses; Thinking → thought bubble floats above avatar; Done → avatar dims
  - Tool-call flash: desk briefly lights up in the agent's color
  - Click any desk to open an agent log modal with that agent's recent events in feed format
- **Checkpoint banner** — approve/reject pipeline checkpoints inline
- **Intervention modal** — send repair instructions; view intervention history; resume pipeline
- **Process manager** — view and kill background processes per project
- **Token tracker** — running prompt/completion/total token counts in the footer

### Event flow
```
Agent/LLM code  -->  EventBus.emit()  -->  .ateam/events.jsonl
                                                  |
                                    server.py tails file (150ms poll)
                                                  |
                                         SSE stream  -->  browser handleEvent()
                                                              |
                                              feed entries + visual office state
```

## Tool Access Per Agent
| Agent        | read_file | write_file | list_directory | search_files | search_content | run_command | fetch_url | web_search |
|--------------|-----------|------------|----------------|--------------|----------------|-------------|-----------|------------|
| Architect    | x         | x          | x              |              |                |             |           |            |
| Planner      | x         | x          | x              |              |                |             |           |            |
| Workers      | x         | x          | x              | x            | x              | x           | x         | x          |
| Reviewer     | x         | x          | x              | x            | x              |             |           |            |
| Intervention | x         | x          | x              | x            | x              | x           |           |            |
