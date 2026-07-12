# AIPod

**AI-native application framework where AI builds software and a dependency-injected runtime executes it.**

Describe your system in natural language. AI generates reusable components, plans execution pipelines, and the runtime assembles and executes them via dependency injection.

## Quick Start

```bash
# 1. Install
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
          → Bean Pool: [ConfigStore, DbClient, SmsSender, SqliteStore]

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
aipod create --category entity --name SqliteStore \
    --desc "SQLite storage, reads database.sqlite_path from ConfigStore"

aipod create --category entry --name DataCollector \
    --desc "generates random sales records"

aipod create --category entry --name DataWriter \
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

- **entry** — business logic, has `execute(ctx)` method
- **entity** — infrastructure (DB, cache, HTTP client), has custom methods

### Pipelines

AI generates pipeline files using pipe syntax:

```python
def run(ctx: PipelineContext):
    S = Pod(build_container(load_config()))

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
│   ├── sqlitestore.py
│   ├── datacollector.py
│   └── datawriter.py
└── pipelines/               ← AI-composed pipelines
    └── sales_flow.py
```

## Security

AST validation on all AI-generated code:
- Blocks: `eval()`, `exec()`, `compile()`, `__import__()`, dunder chain access
- Does NOT restrict imports — this runs locally, you own the code

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
