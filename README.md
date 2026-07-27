# AIPod

**A human-governed operating framework for AI software agents.**

Humans define the architecture, component contracts, execution boundaries, and
review requirements. AIPod encodes those decisions as a project protocol so AI
agents can inspect, build, validate, run, and report on software without freely
operating on an unstructured codebase.

> **Human defines governance. AIPod encodes it. AI operates within it.**

## Why AIPod?

Most AI coding tools give an agent broad freedom to edit a repository. AIPod
gives it a durable operating model instead:

```text
Human
  → defines architecture, permissions, and review rules
AIPod
  → encodes component contracts, project memory, pipelines, validation, and traces
AI Agent
  → inspects → builds → validates → runs → reports
```

AIPod is not a prompt that tells an AI how to code. It is the framework the
project adopts. Its Bean Pool, component contracts, deterministic Pipeline
runtime, JSON Agent Project Model, and execution traces are the shared facts an
AI agent must work with.

## What AIPod Governs

- **Component boundaries.** Everything is a `provider` or `service`, with an
  explicit dependency and data contract.
- **Reusable project memory.** Human-written and AI-generated components join
  the Bean Pool, which agents inspect before creating new capability.
- **Deterministic orchestration.** Pipelines compose `service` components; the
  DI runtime, not the AI, assembles and executes them.
- **Validation and recovery.** Generated code is checked before registration;
  invalid output is never silently added to the system.
- **Observable execution.** Every `aipod run` attempt writes a redacted trace
  that humans and agents can inspect.
- **Machine-readable governance.** Agents use JSON contracts, state, changes,
  and diagnostics rather than inferring intent from terminal prose.

This lets a system accumulate capability without allowing an agent to improvise
its architecture on every task.


## Quick Start

```bash
# 1. Install (Python 3.10+)
pip install aipodcli

# 2. Configure once (global, shared across all projects)
aipod config set OPENAI_API_KEY sk-your-key
aipod config set OPENAI_BASE_URL https://api.openai.com/v1
aipod config set OPENAI_MODEL deepseek-chat

# 3. Create a project
mkdir my-app && cd my-app
aipod init
aipod pod "a CLI todo app with SQLite storage, add/list/complete/delete"

# 4. Run it
python main.py add "Buy groceries"
python main.py list
```

Review the generated code and authorize execution when it is appropriate for
your project. AIPod keeps the resulting components, routes, and execution
evidence inspectable for later work.

Want to inspect the runtime before connecting an LLM? Run the checked-in
[Todo CLI example](examples/todo_cli/README.md); it uses the same Bean Pool,
DI container, and PipelineRunner without making an API call.

## What Just Happened

```
aipod init
  → modules/, pipelines/, config.toml, routes.toml, beans_config.json

aipod pod "a CLI todo app..."
  → AI decomposes requirement into components
  → AI generates 4 components (TodoStore, AddTodo, ListTodo, CompleteTodo)
  → AI composes 3 pipelines (add, list, complete)
  → AI generates entry point (main.py with argparse)
  → Registers everything in routes.toml and beans_config.json
```

## The Growing System

Every component you create makes the system smarter:

```
Round 1:  aipod create --name SqliteStore --desc "SQLite storage"
          → Bean Pool: [ConfigStore, SqliteStore]

Round 2:  aipod create --name DataCollector --desc "generates sales data"
          → Bean Pool: [..., DataCollector]

Round 3:  aipod create --name DataWriter --desc "depends on SqliteStore, writes to DB"
          → AI sees SqliteStore in the pool, auto-wires it as dependency

Compose:  aipod compose "collect sales and write to SQLite"
          → AI picks [DataCollector, DataWriter] from the pool
          → Generates pipeline: (S(DataCollector) | S(DataWriter)).execute_all(ctx)
```

**The Bean Pool grows with every `create`.** Agents see more approved
components, reuse them in richer pipelines, and work from a shared project
model rather than a one-shot prompt.

## Commands

| Command | What it does | Needs AI |
|---------|-------------|----------|
| `aipod init` | Create project skeleton | ❌ |
| `aipod config set KEY VALUE` | Set global config (once, shared everywhere) | ❌ |
| `aipod config list` | Show global config | ❌ |
| `aipod entry "desc"` | AI generates entry point file | ✅ |
| `aipod visualize` | Generate an interactive component and Pipeline graph | ❌ |
| `aipod inspect --json` | Read the Agent Project Model (components, pipelines, validation) | ❌ |
| `aipod run ROUTE --params JSON --json` | Run a route and persist a structured execution trace | ❌ |
| `aipod create --category service\|provider --name X --desc "..."` | AI generates one component | ✅ |
| `aipod add --name X --class-path Y` | Register hand-written component | ❌ |
| `aipod compose "instruction"` | AI generates pipeline | ✅ |
| `aipod pod "requirement"` | **AI generates components + pipelines + entry** | ✅ |
| `aipod pod --file req.md` | Same, reads from file | ✅ |

## Two Ways to Build

### Fast: `pod` (one-shot)

```bash
aipod init
aipod pod "e-commerce order system with inventory, payment, and notifications"
python main.py
```

AI generates everything: components, pipelines, entry point, config.

### Step-by-step: `create` → `compose`

```bash
aipod init

# Build component pool incrementally
aipod create --category provider --name SqliteStore \
    --desc "SQLite storage, reads database.sqlite_path from ConfigStore"

aipod create --category service --name DataCollector \
    --desc "generates random sales records"

aipod create --category service --name DataWriter \
    --desc "depends on SqliteStore, writes records to database"

# Compose pipelines from the pool
aipod compose "collect sales data and write to SQLite" --name sales_flow

# Generate entry point
aipod entry "a CLI data processing tool"

# Run
python main.py sales_flow
```

## How It Works

### The Bean Pool

Every component is registered in `beans_config.json`:

```json
{
  "id": "DataWriter",
  "class_path": "modules.datawriter.DataWriter",
  "dependencies": ["SqliteStore"],
  "inputs": {"raw_sales": "list — sales records"},
  "outputs": {"written_count": "int — rows written"}
}
```

AI reads the pool when generating new components and composing pipelines. **The pool is the memory of your system.**

### Components

AI generates classes with constructor injection:

```python
class DataWriter:
    @inject
    def __init__(self, sqlite_store: SqliteStore, config_store: ConfigStore):
        self.sqlite_store = sqlite_store           # Auto-injected by runtime
        self.batch_size = config_store.get("writer.batch_size", 100)

    def execute(self, ctx: PipelineContext) -> dict:
        records = ctx.get("raw_sales", [])
        for r in records:
            self.sqlite_store.insert("sales", r)
        ctx.set("written_count", len(records))
        return {"status": "success"}
```

- **service** — business logic, has `execute(ctx)` and can be placed in a pipeline
- **provider** — infrastructure (DB, cache, HTTP client), exposes custom methods and is injected into services

### Pipelines

AI generates pipeline files using pipe syntax:

```python
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import build_container, Pod
from modules.services.datacollector import DataCollector
from modules.services.datacleaner import DataCleaner
from modules.services.datawriter import DataWriter
from modules.services.notifier import Notifier

def run(ctx: PipelineContext):
    beans = load_beans()
    S = Pod(build_container(beans))

    # Chain: DataCollector → DataCleaner → DataWriter
    (S(DataCollector) | S(DataCleaner) | S(DataWriter)).execute_all(ctx)

    # Branching
    if ctx.get("alert_needed"):
        (S(Notifier)).execute_all(ctx)

    return ctx.summary()
```

### Global Configuration

Set once, use everywhere:

```bash
aipod config set OPENAI_API_KEY sk-xxx       # stored in ~/.aipod/config.toml
aipod config set OPENAI_BASE_URL https://...
```

Components read project config through injected `ConfigStore`:

```toml
# config.toml (per-project)
[database]
sqlite_path = "data.db"    # AI suggested this when creating SqliteStore
```

```python
config_store.get("database.sqlite_path", "data.db")
```

### Generation → Execution

```
┌──────────────────────────┐
│ Human governance + agent │
│                          │
│  aipod init              │  → project skeleton
│  aipod inspect --json    │  → machine-readable project state
│  aipod config set ...    │  → global config
│  aipod pod "big req"     │  → components + pipelines + entry
│  aipod create ...        │  → single component (pool grows)
│  aipod compose "..."     │  → pipeline + route
│  aipod entry "desc"      │  → entry point file
│                          │
│  Human reviews & authorizes │
│  Agent receives JSON state │
└──────────────────────────┘
            ↓
┌──────────────────────────┐
│   Runtime (run time)     │
│                          │
│  aipod run ROUTE --json  │  → explicit, traceable execution
│  PipelineRunner loads    │
│  DI container assembles  │
│  Pipeline executes       │
│  Context flows data      │
│  inspect runs --json     │  → result and execution trace
└──────────────────────────┘
```

**Execution is explicit and observable.** Humans decide when an agent may run a
route; AIPod records the result as a redacted trace for later review.

## Key APIs

| API | Methods |
|-----|---------|
| **PipelineContext** | `ctx.params`, `ctx.set(k,v)`, `ctx.get(k,d)`, `ctx.summary()` |
| **ConfigStore** | `get("section.key", default)`, `get_section("name")`, `sections()` |
| **Pod** | `S = Pod(container)`, `S(Class)`, `(S(A) \| S(B)).execute_all(ctx)` |
| **PipelineRunner** | `PipelineRunner()`, `route_names()`, `run("name", params)` |

## Project Structure

```
project/
├── main.py                  ← AI-generated entry point
├── config.toml              ← Project config (you + AI)
├── routes.toml              ← Pipeline routes (compose/pod auto-registers)
├── beans_config.json        ← Component pool (AI maintains)
├── modules/                 ← Your component pool
│   ├── providers/
│   │   └── sqlitestore.py
│   └── services/
│       ├── datacollector.py
│       └── datawriter.py
└── pipelines/               ← AI-composed pipelines
    └── sales_flow.py
```

## Security

All AI-generated code receives static validation before it is written or registered:
- Blocks: `eval()`, `exec()`, `compile()`, `__import__()`, dunder chain access
- Checks syntax plus the required component/Pipeline entry-point contract
- If validation fails, shows the exact errors and asks before sending them to the LLM for a correction attempt (at most three attempts)
- Does NOT sandbox imports, filesystem access, networking, or process execution. Review generated code before running it locally.

## Visualize Your System

Generate a standalone interactive graph of the current Bean Pool, dependency
edges, routes, and statically detected Pipeline service chains:

```bash
aipod visualize
# writes aipod-graph.html

aipod visualize --open
```

Click a component to inspect its class path, contract, dependencies, and
description. The command only reads project metadata and Python source; it never
imports or executes generated components.

## Agent Project Model

`SKILL.md` tells an AI agent how to operate AIPod. `inspect --json` tells it
what currently exists in the project, without requiring it to parse terminal
text, HTML, or source files:

```bash
aipod inspect --json
aipod inspect components --json
aipod inspect component SqliteStore --json
aipod inspect pipeline sales_flow --json
aipod inspect runs --json
aipod inspect run RUN_ID --json
aipod inspect --summary --json
```

The stable JSON model includes component contracts, DI dependencies, statically
parsed Pipeline service chains, and validation issues such as missing
dependencies or pipeline files. `visualize` renders the same model for humans.

For mutating Agent operations, use `--json`. AIPod emits one JSON envelope with
the command status, exit code, structured project changes, and diagnostics; an
Agent never needs to parse terminal decorations. A mutating command that makes
no state change returns `status: "no_change"`:

```bash
aipod create --category service --name ImportOrders --desc "..." --json
aipod compose "import orders" --name import_orders --json
aipod pod "an order import CLI" --yes --json
```

## Agent Run and Trace

Run a registered route without depending on an AI-generated entry file. Every
attempt, including failures, is persisted as a redacted JSON trace under
`.aipod/runs/`:

```bash
aipod run sales_flow --params '{"month":"2026-07"}' --json
aipod inspect runs --json
aipod inspect run run_20260727T120000Z_abcdef12 --json
```

Traces include the route, parameters, result or error, total duration, and
per-component durations recorded by the Pipeline runtime. Fields whose names
look like secrets, passwords, or tokens are redacted before persistence.

## Install

```bash
pip install aipodcli
```

## Roadmap

- [ ] Component contract validation (typed inputs/outputs)
- [ ] Pipeline static type checking
- [ ] Component versioning
- [ ] Visual pipeline graph
- [ ] Incremental generation (AI reuses existing components)
- [ ] Multi-language component support

## License

MIT
