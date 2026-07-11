# AIPod

**AI-native application framework where AI builds software and a dependency-injected runtime executes it.**

## The Growing System

Every component you create makes the system smarter:

```
Round 1:  aipod create --name SqliteStore --desc "SQLite storage"
          → Bean Pool: [ConfigStore, DbClient, SmsSender, SqliteStore]

Round 2:  aipod create --name DataCollector --desc "generates sales data"
          → Bean Pool: [ConfigStore, DbClient, SmsSender, SqliteStore, DataCollector]

Round 3:  aipod create --name DataWriter --desc "depends on SqliteStore, writes to DB"
          → AI sees SqliteStore in the pool, auto-wires it as dependency
          → Bean Pool: [..., DataWriter]

Compose:  aipod compose "collect sales and write to SQLite"
          → AI picks [DataCollector, DataWriter] from the pool
          → Generates pipeline: (S(DataCollector) | S(DataWriter)).execute_all(ctx)
```

**The bean pool grows with every `create`. AI sees more components, builds richer pipelines.** This is not a one-shot code generator — it's a system that accumulates capability.

## 5-Minute Workflow

```bash
# 1. Install
pip install aipodcli

# 2. Start a project — AI picks the tech stack
aipod init "a CLI tool for data processing tasks"
# → Generates cli.py with PipelineRunner, creates config.toml, routes.toml

# 3. Build your component pool
aipod create --category entity --name SqliteStore \
    --desc "SQLite storage, reads database.sqlite_path from ConfigStore"
# → AI sees ConfigStore in pool → injects it → suggests [database] config → writes to config.toml

aipod create --category entry --name DataCollector \
    --desc "generates random sales records, writes to ctx.set('raw_sales')"

aipod create --category entry --name DataWriter \
    --desc "depends on SqliteStore, reads raw_sales from ctx, inserts into sales table"
# → AI sees SqliteStore → auto-selects it as dependency

# 4. Compose a pipeline — AI plans the chain from your pool
aipod compose "collect sales data and write to SQLite" --name sales_flow
# → AI picks [DataCollector → DataWriter] from pool
# → Generates pipelines/sales_flow.py
# → Registers route in routes.toml
# → Does NOT execute — you review first

# 5. Run it
python cli.py sales_flow
# → PipelineRunner loads routes.toml → finds sales_flow → executes pipeline
# → DI container injects SqliteStore into DataWriter → pipe chain runs → data flows through ctx
```

**What just happened:**
- You described 3 components in natural language
- AI generated all the code, wired the dependencies, managed the config
- AI planned the execution pipeline from your component pool
- The runtime assembled everything via DI and executed it
- **You wrote zero code**

## Commands

| Command | What it does |
|---------|-------------|
| `aipod init "desc"` | Create project skeleton + AI generates entry point |
| `aipod create --name X --desc "..."` | AI generates component, adds to pool |
| `aipod add --name X --class-path Y` | Register hand-written component to pool |
| `aipod compose "instruction"` | AI picks from pool, generates pipeline |
| `aipod compose --list` | List saved pipelines |

## How It Works

### The Bean Pool

Every component (AI-generated or hand-written) is registered in `beans_config.json`:

```json
{
  "id": "DataWriter",
  "class_path": "modules.datawriter.DataWriter",
  "dependencies": ["SqliteStore"],
  "inputs": {"raw_sales": "list — sales records"},
  "outputs": {"written_count": "int — number of rows written"},
  "description": "..."
}
```

When you run `create`, AI reads the entire pool, picks dependencies, and generates code that fits in. When you run `compose`, AI reads the pool again to plan which components to chain.

**The pool is the memory of your system.** It grows with every `create` and `add`.

### Components

AI generates classes with constructor injection and data contracts:

```python
class DataWriter:
    @inject
    def __init__(self, sqlite_store: SqliteStore, config_store: ConfigStore):
        self.sqlite_store = sqlite_store           # Auto-injected by runtime
        self.batch_size = config_store.get("writer.batch_size", 100)  # From config.toml

    def execute(self, ctx: PipelineContext) -> dict:
        records = ctx.get("raw_sales", [])          # Read from upstream
        for r in records:
            self.sqlite_store.insert("sales", r)
        ctx.set("written_count", len(records))      # Write for downstream
        return {"status": "success"}
```

Two types of components:
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

`PipelineContext` is the data bus — each component reads inputs via `ctx.get()`, writes outputs via `ctx.set()`.

### Configuration

AI suggests config entries when creating components. They go into `config.toml`:

```toml
[database]
sqlite_path = "data.db"    # AI suggested this when creating SqliteStore

[writer]
batch_size = 100            # You added this manually
```

Components read config through injected `ConfigStore`:
```python
config_store.get("database.sqlite_path", "data.db")
```

### Generation → Execution

```
┌─────────────────────────┐
│   You + AI (build time) │
│                         │
│  aipod init "desc"      │  → entry point
│  aipod create ...       │  → components (pool grows)
│  aipod compose "..."    │  → pipeline + route
│                         │
│  You review the code    │
│  You git commit         │
└─────────────────────────┘
            ↓
┌─────────────────────────┐
│   Runtime (run time)    │
│                         │
│  python cli.py route    │
│  PipelineRunner loads   │
│  DI container assembles │
│  Pipeline executes      │
│  Context flows data     │
└─────────────────────────┘
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
├── cli.py / app.py          ← AI-generated entry (init decides the type)
├── config.toml              ← Config (you + AI maintain)
├── routes.toml              ← Pipeline routes (compose auto-registers)
├── beans_config.json        ← Component pool (AI maintains)
├── .env                     ← LLM API config
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
