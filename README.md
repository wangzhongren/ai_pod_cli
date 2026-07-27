# AIPod

**AI-native application framework where AI builds software and a dependency-injected runtime executes it.**

Describe your system in natural language. AI generates reusable components, plans execution pipelines, and the runtime assembles and executes them via dependency injection.

## Why AIPod?

AI coding tools today fall into two camps:

| Tool | What it does | The problem |
|------|-------------|-------------|
| Copilot, Cursor | Autocomplete lines as you type | You still write the architecture |
| v0, Bolt, Lovable | Generate entire projects from a prompt | One-shot, no memory. You take over after generation. |

**Both leave AI out of the system's evolution.** After the initial generation, AI is gone. The codebase grows, but AI doesn't grow with it.

AIPod is different:

- **AI has memory.** Every component you build (hand-written OR AI-generated) joins a shared Bean Pool. AI sees the pool and reuses components in future work.
- **You and AI share a platform.** The DI container, pipeline DSL, and component contracts are the common language. AI generates code that follows the same rules as your hand-written code.
- **The system accumulates capability.** Round 1 you create a SqliteStore. Round 3 you compose a pipeline that uses SqliteStore, DataCollector, and a hand-written AnalyticsEngine — seamlessly.

**It's not AI writing code for you. It's AI building the system WITH you, on a shared platform.**


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

That's it. **Zero code written by you.**

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

**The bean pool grows with every `create`. AI sees more components, builds richer pipelines.** This is not a one-shot code generator — it's a system that accumulates capability.

## Commands

| Command | What it does | Needs AI |
|---------|-------------|----------|
| `aipod init` | Create project skeleton | ❌ |
| `aipod config set KEY VALUE` | Set global config (once, shared everywhere) | ❌ |
| `aipod config list` | Show global config | ❌ |
| `aipod entry "desc"` | AI generates entry point file | ✅ |
| `aipod visualize` | Generate an interactive component and Pipeline graph | ❌ |
| `aipod inspect --json` | Read the Agent Project Model (components, pipelines, validation) | ❌ |
| `aipod create --name X --desc "..."` | AI generates one component | ✅ |
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
│   You + AI (build time)  │
│                          │
│  aipod init              │  → project skeleton
│  aipod config set ...    │  → global config
│  aipod pod "big req"     │  → components + pipelines + entry
│  aipod create ...        │  → single component (pool grows)
│  aipod compose "..."     │  → pipeline + route
│  aipod entry "desc"      │  → entry point file
│                          │
│  You review the code     │
│  You git commit          │
└──────────────────────────┘
            ↓
┌──────────────────────────┐
│   Runtime (run time)     │
│                          │
│  python main.py cmd      │
│  PipelineRunner loads    │
│  DI container assembles  │
│  Pipeline executes       │
│  Context flows data      │
└──────────────────────────┘
```

**AI never runs your code.** It generates it. You review, commit, and execute when ready.

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
aipod inspect --summary --json
```

The stable JSON model includes component contracts, DI dependencies, statically
parsed Pipeline service chains, and validation issues such as missing
dependencies or pipeline files. `visualize` renders the same model for humans.

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
