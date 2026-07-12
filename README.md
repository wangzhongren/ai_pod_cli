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

## Two Ways to Build

### Fast: one-shot with `pod`

Describe an entire system, AI generates everything:

```bash
# Write requirements to a file
cat > requirements.md << 'EOF'
# E-commerce Order System
1. OrderService - handle orders, validate stock
2. InventoryManager - depends on SqliteStore, manage stock
3. PaymentProcessor - depends on ConfigStore, process payments
4. NotificationSender - depends on SmsSender, send alerts
EOF

# One command: components + pipelines + entry point
aipod pod --file requirements.md
```

```
📋 [拆解方案] ecommerce_order
   组件: 4 个  |  Pipeline: 3 条

   📦 组件:
      1. OrderService (entry) ← depends: InventoryManager, PaymentProcessor
      2. InventoryManager (entry) ← depends: SqliteStore
      3. PaymentProcessor (entry) ← depends: ConfigStore
      4. NotificationSender (entry) ← depends: SmsSender

   🔗 Pipeline:
      1. create_order → 用户下单，校验库存并处理支付
      2. check_inventory → 查询商品库存
      3. send_notification → 发送订单通知

确认生成 4 个组件 + 3 条 pipeline？[Y/n] Y

🤖 [1/4] 生成 OrderService...    ✅
🤖 [2/4] 生成 InventoryManager... ✅
🤖 [3/4] 生成 PaymentProcessor... ✅
🤖 [4/4] 生成 NotificationSender... ✅

🔗 [生成 Pipeline] 3 条
   [1/3] create_order...    💾 已保存  📋 已注册
   [2/3] check_inventory... 💾 已保存  📋 已注册
   [3/3] send_notification... 💾 已保存  📋 已注册

🚀 [入口生成] AI 正在生成项目入口...
   ✍️  app.py

==================================================
🧩 [Pod 生成完毕]
   ✅ 组件: 4 个
   🔗 Pipeline: 3 条
   🚀 入口: app.py

   运行: python app.py
==================================================
```

### Step-by-step: `init` → `create` → `compose`

For more control, build incrementally:

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
```

**What just happened:**
- You described 3 components in natural language
- AI generated all the code, wired the dependencies, managed the config
- AI planned the execution pipeline from your component pool
- The runtime assembles everything via DI and executes it
- **You wrote zero code**

## Commands

| Command | What it does |
|---------|-------------|
| `aipod init "desc"` | Create project skeleton + AI generates entry point |
| `aipod pod "desc"` | **AI decomposes requirement → components + pipelines + entry** |
| `aipod pod --file req.md` | Same as above, reads full requirements from file |
| `aipod create --name X --desc "..."` | AI generates one component, adds to pool |
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
┌──────────────────────────┐
│   You + AI (build time)  │
│                          │
│  aipod init "desc"       │  → entry point + skeleton
│  aipod pod "big req"     │  → components + pipelines + entry
│  aipod create ...        │  → single component (pool grows)
│  aipod compose "..."     │  → pipeline + route
│                          │
│  You review the code     │
│  You git commit          │
└──────────────────────────┘
            ↓
┌──────────────────────────┐
│   Runtime (run time)     │
│                          │
│  python app.py route     │
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
├── app.py / cli.py          ← AI-generated entry (init or pod decides the type)
├── config.toml              ← Config (you + AI maintain)
├── routes.toml              ← Pipeline routes (compose / pod auto-registers)
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
