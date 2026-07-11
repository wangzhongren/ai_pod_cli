# AIPod

**AI-native application framework where AI builds software and a dependency-injected runtime executes it.**

Describe components in natural language. AI generates production-ready modules, assembles dependency graphs, plans execution pipelines, and the runtime executes them deterministically.

```
Natural Language
      │
      ▼
  AI Planner
      │
      ▼
Component Graph          ← AI generates reusable components, not one-off scripts
      │
      ▼
Pipeline File            ← AI compiles workflows, not just code
      │
      ▼
  Runtime                ← DI container assembles, PipelineRunner executes
      │
      ▼
   Execution
```

## What makes AIPod different

| | AI Code Generators | AIPod |
|---|-------------------|-------|
| Output | One-off scripts | Reusable, dependency-injected components |
| Orchestration | You write glue code | AI plans execution pipelines |
| Execution | Prompt → Run | **Generate → Review → Commit → Runtime** |
| Configuration | You write config code | AI suggests config, auto-appends to TOML |
| Project setup | You pick the stack | AI decides (Flask/CLI/RabbitMQ/Kafka...) |

The core innovation is **generation-execution separation**: AI generates code, you review and commit it, then the runtime assembles and executes it deterministically. This makes it suitable for real engineering workflows, not just prototyping.

## Quick Start

### Install

```bash
pip install aipodcli
```

### Initialize

```bash
aipod init "a REST API for inventory management"
```

AI decides the tech stack, generates the entry point (`app.py` / `main.py` / `consumer.py`...), and creates the project skeleton:

```
project/
├── app.py              ← AI-generated entry point
├── config.toml         ← Project configuration
├── routes.toml         ← Pipeline route mapping
├── beans_config.json   ← Component registry
├── .env                ← LLM API config
├── modules/            ← AI-generated components
└── pipelines/          ← AI-generated pipelines
```

### Create Components

```bash
aipod create --category entity --name SqliteStore \
    --desc "SQLite storage, reads database.sqlite_path from config"

aipod create --category entry --name DataCollector \
    --desc "Generates random sales records"

aipod create --category entry --name DataWriter \
    --desc "Depends on SqliteStore, writes records to database"
```

Each `create` call: AI analyzes the component pool, selects dependencies, generates DI-wired code, runs security checks, and registers the component.

### Compose Pipelines

```bash
aipod compose "collect sales data and write to SQLite" --name sales_flow
```

AI plans the execution chain, generates a pipeline file, and registers it in `routes.toml`. **No execution happens** — you review the code first.

### Run

```bash
python app.py sales_flow                    # Via your entry point
```

## Commands

| Command | Purpose |
|---------|---------|
| `aipod init "desc"` | Initialize project, AI generates entry point |
| `aipod create --name X --desc "..."` | AI generates a component |
| `aipod add --name X --class-path Y` | Register a hand-written component |
| `aipod compose "instruction"` | AI plans and generates a pipeline |
| `aipod compose --list` | List all saved pipelines |

## Architecture

### Components, not scripts

AI generates **components** — reusable classes with constructor injection and well-defined input/output contracts:

```python
class DataWriter:
    @inject
    def __init__(self, sqlite_store: SqliteStore, config_store: ConfigStore):
        self.sqlite_store = sqlite_store
        self.batch_size = config_store.get("writer.batch_size", 100)

    def execute(self, ctx: PipelineContext) -> dict:
        records = ctx.get("clean_records", [])
        for r in records:
            self.sqlite_store.insert("sales", r)
        ctx.set("written_count", len(records))
        return {"status": "success"}
```

The runtime automatically resolves and injects `SqliteStore` and `ConfigStore` — you never manually wire dependencies.

### Pipelines, not glue code

The pipe syntax chains components with automatic data flow:

```python
def run(ctx: PipelineContext):
    S = Pod(build_container(load_config()))

    # DataCollector → DataCleaner → DataWriter
    (S(DataCollector) | S(DataCleaner) | S(DataWriter)).execute_all(ctx)

    # Conditional branching
    if ctx.get("alert_needed"):
        (S(Notifier)).execute_all(ctx)

    return ctx.summary()
```

`PipelineContext` flows data between components — each reads from `ctx.get()`, writes to `ctx.set()`.

### Configuration, not code

Components read config through injected `ConfigStore`:

```toml
# config.toml — developer and AI co-maintain this
[database]
sqlite_path = "data.db"    # AI auto-suggests when creating SqliteStore

[writer]
batch_size = 100
```

```python
batch_size = config_store.get("writer.batch_size", 100)
```

### Generation-execution separation

```
┌──────────────────────────┐
│    Generation (AI)       │
│                          │
│  init    → entry point   │
│  create  → components    │
│  compose → pipelines     │
│                          │
│  Developer reviews code  │
│  Developer commits       │
└──────────────────────────┘
           ↓
┌──────────────────────────┐
│    Execution (Runtime)   │
│                          │
│  Entry point dispatches  │
│  PipelineRunner loads    │
│  DI container assembles  │
│  Pipeline executes       │
│  Context flows data      │
└──────────────────────────┘
```

## Key APIs

### PipelineContext — data flow between components

| Method | Purpose |
|--------|---------|
| `ctx.params` | Entry parameters dict |
| `ctx.set(key, value)` | Write to shared data pool |
| `ctx.get(key, default)` | Read from shared data pool |
| `ctx.summary()` | Execution summary |

### ConfigStore — centralized TOML config

| Method | Purpose |
|--------|---------|
| `get("section.key", default)` | Dot-notation access |
| `get_section("section")` | Entire section as dict |
| `sections()` | List section names |

### Pod — pipe-chainable component wrapper

```python
S = Pod(container)
(S(A) | S(B) | S(C)).execute_all(ctx)
```

### PipelineRunner — pipeline loader and executor

```python
runner = PipelineRunner()          # reads routes.toml
runner.run("sales_flow", params)   # execute pipeline
```

## Security

AST-based validation on all AI-generated code:
- Blocks: `eval()`, `exec()`, `compile()`, `__import__()`
- Blocks: dunder chain access (`__subclasses__`, `__mro__`, `__globals__`)
- Does NOT restrict imports — this runs locally, you own the code

## Configuration

| File | Purpose | Maintained by |
|------|---------|---------------|
| `.env` | LLM API credentials | Developer |
| `config.toml` | Project settings | Developer + AI |
| `routes.toml` | Pipeline routing | AI + Developer |
| `beans_config.json` | Component registry | AI |

## Roadmap

- [ ] Component contract validation (typed inputs/outputs)
- [ ] Pipeline static type checking
- [ ] Component versioning
- [ ] Visual pipeline graph
- [ ] Incremental generation (AI reuses existing components)
- [ ] Multi-language component support

## License

MIT
