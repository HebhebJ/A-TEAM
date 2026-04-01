# A-TEAM: Agentic Development System

## Context
Building a multi-agent system where an orchestrator spawns specialized AI agents (architect, planner, workers, reviewer) to collaboratively build software projects from a high-level user request. LLM calls go through OpenRouter API. Agents communicate via files. Human checkpoints at key stages.

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
```

### Key Design Decisions
- **Orchestrator is deterministic Python**, not an LLM agent -- predictable, cheap, debuggable
- **File-based state** -- resumable, inspectable, human-editable
- **Single BaseAgent class** -- all agents share the same tool-calling loop, differ only in prompt + allowed tools
- **OpenRouter with OpenAI-compatible format** -- easy to swap providers later

## Project Structure
```
A-TEAM/
├── pyproject.toml
├── config.toml
├── .env                        # OPENROUTER_API_KEY
├── .gitignore
├── ateam/
│   ├── __init__.py
│   ├── __main__.py             # python -m ateam
│   ├── cli.py                  # CLI interface
│   ├── config.py               # Config loading (env + toml + CLI)
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── base.py             # LLMClient protocol
│   │   ├── openrouter.py       # OpenRouter implementation
│   │   └── message_types.py    # Message, ToolCall, LLMResponse dataclasses
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── base.py             # BaseAgent (agentic tool-calling loop)
│   │   ├── orchestrator.py     # State machine driving the pipeline
│   │   ├── architect.py        # Architect agent
│   │   ├── planner.py          # Planner agent
│   │   ├── worker.py           # Worker agent (parameterized by specialty)
│   │   └── reviewer.py         # Reviewer agent
│   ├── prompts/
│   │   ├── architect.md
│   │   ├── planner.md
│   │   ├── worker_frontend.md
│   │   ├── worker_backend.md
│   │   ├── worker_database.md
│   │   ├── worker_devops.md
│   │   └── reviewer.md
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── base.py             # Tool protocol + ToolRegistry
│   │   ├── file_ops.py         # read_file, write_file, list_directory
│   │   ├── search.py           # search_files, search_content
│   │   └── shell.py            # run_command (sandboxed, timeout)
│   └── state/
│       ├── __init__.py
│       ├── project_state.py    # ProjectState (JSON-backed persistence)
│       └── phase.py            # Phase, Task dataclasses
└── workspaces/                 # Generated projects go here
    └── <project>/
        ├── .ateam/             # Metadata, state, docs, logs, reviews
        └── src/                # Generated source code
```

## Build Phases (incremental)

### Phase 1: Core foundation
- `config.py` -- load API key from env/.env/config.toml
- `llm/message_types.py` -- Message, ToolCall, LLMResponse dataclasses
- `llm/base.py` -- LLMClient protocol
- `llm/openrouter.py` -- chat completions with tool calling support
- `tools/base.py` -- Tool protocol + ToolRegistry
- `tools/file_ops.py` -- read_file, write_file, list_directory
- `tools/search.py` -- search_files, search_content
- `tools/shell.py` -- run_command

### Phase 2: Agent system
- `agents/base.py` -- BaseAgent with agentic tool-calling loop
- `state/phase.py` -- Phase, Task dataclasses
- `state/project_state.py` -- JSON persistence + state transitions
- `agents/orchestrator.py` -- state machine
- Prompts for all agent types

### Phase 3: All agents + CLI
- `agents/architect.py`, `planner.py`, `worker.py`, `reviewer.py`
- `cli.py` -- CLI with checkpoints (approve/reject/modify)
- `__main__.py` -- entry point
- Logging (JSONL per agent in .ateam/logs/)

### Phase 4: Polish
- Resume from interrupted state
- Better CLI output (progress, colors)
- Error handling refinement

## Tool Access Per Agent
| Agent     | read_file | write_file | list_directory | search_files | search_content | run_command |
|-----------|-----------|------------|----------------|--------------|----------------|-------------|
| Architect | x         | x          | x              |              |                |             |
| Planner   | x         | x          | x              |              |                |             |
| Workers   | x         | x          | x              | x            | x              | x           |
| Reviewer  | x         | x          | x              | x            | x              |             |

## Verification
- Phase 1: Unit test OpenRouter client with a simple prompt
- Phase 2: Run architect agent on "make a cat website", verify it creates .md files in workspace
- Phase 3: Full end-to-end run with checkpoints
- Phase 4: Interrupt mid-run, resume, verify state consistency
