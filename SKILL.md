# AIPodCli — AI Agent Skill Reference

## Identity

You are working with **AIPodCli**, an AI-native Python development framework.
Your role is to help developers use this framework to build applications where AI generates components and pipelines, and the framework handles DI assembly and execution.

## Installation

```bash
pip install aipodcli
```

This installs the `ai_pod_cli` package and the `ai-pod` CLI entry point. All dependencies (openai, injector, python-dotenv, tomlkit) are installed automatically.

## Quick Decision Tree

```
User wants to...                        → Use this
─────────────────────────────────────────────────────
Start a new project                     → init "description"
Create a new component                  → create --category X --name Y --desc "Z"
Register hand-written component         → add --name X --class-path Y --desc "Z"
Generate a pipeline (no execution)      → build "instruction" --name X
List generated pipelines               → build --list
Run a pipeline                          → User runs their entry point (e.g. python cli.py route_name)
```

## Commands

### `init` — Initialize Project

```bash
python -m ai_pod_cli init "project description"
python -m ai_pod_cli init --install-deps
```

**What it does:**
1. Creates `modules/`, `pipelines/` directories
2. Creates `config.toml`, `routes.toml`, `beans_config.json`
3. If description given: AI decides project type (Flask/CLI/RabbitMQ/Kafka/APScheduler/WebSocket...) and generates an entry point file
4. Entry point uses `PipelineRunner` from `ai_pod_cli.runner`

**Output files:**
- Entry point: `app.py` / `main.py` / `consumer.py` / `cli.py` (AI decides)
- `config.toml` — empty, developer adds sections
- `routes.toml` — empty, `build` command auto-registers pipelines
- `beans_config.json` — pre-loaded with ConfigStore, DbClient, SmsSender

### `create` — AI Generate Component

```bash
python -m ai_pod_cli create --category entry --name StockChecker --desc "checks stock and alerts if zero"
python -m ai_pod_cli create --category entity --name RedisStore --desc "Redis client with get/set methods"
```

**What AI does:**
1. Reads `beans_config.json` to see existing components
2. Reads `config.toml` key names (not values) to know available config
3. Selects dependencies from the bean pool
4. Generates code following framework conventions
5. Runs AST security check
6. Writes to `modules/<name>.py`
7. Registers in `beans_config.json` with inputs/outputs contract
8. If new config needed, suggests additions via `config_additions` → auto-appended to `config.toml` using tomlkit

### `add` — Register Human-Written Component

```bash
python -m ai_pod_cli add --name MyService --class-path mypackage.service.MyService --desc "does X"
```

**When to use:** Developer wrote a component manually and wants AI to know about it.

### `build` — AI Generate Pipeline (Does NOT Execute)

```bash
python -m ai_pod_cli build "collect sales data and write to SQLite" --name sales_pipeline
python -m ai_pod_cli build --list
```

**What AI does:**
1. Reads bean pool + config.toml keys + component import paths
2. Plans which components to chain
3. Generates `pipelines/<name>.py` with pipe syntax `(S(A) | S(B)).execute_all(ctx)`
4. Runs AST security check
5. Registers route in `routes.toml`
6. Prints instructions for running via entry point

**Important:** `build` does NOT execute. Developer runs via their entry point.

## Code Templates

### Entry Component (has `execute` method)

```python
from injector import inject
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.config_store import ConfigStore
# import dependencies from their correct paths

class MyComponent:
    """Description"""

    @inject
    def __init__(self, dep_a: DepA, config_store: ConfigStore):
        self.dep_a = dep_a
        self.setting = config_store.get("section.key", "default")

    def execute(self, ctx: PipelineContext) -> dict:
        # Read from params or upstream
        value = ctx.params.get("key")
        upstream = ctx.get("upstream_key")

        # Process using injected dependencies
        result = self.dep_a.process(value)

        # Write for downstream components
        ctx.set("my_output", result)
        return {"status": "success", "data": result}
```

### Entity (no `execute`, custom methods)

```python
from injector import inject
from ai_pod_cli.config_store import ConfigStore

class MyEntity:
    """Infrastructure component"""

    @inject
    def __init__(self, config_store: ConfigStore):
        host = config_store.get("my.host", "localhost")
        port = config_store.get("my.port", 5432)
        # Initialize connection...

    def query(self, sql: str) -> list:
        ...

    def insert(self, table: str, data: dict) -> int:
        ...
```

### Pipeline File

```python
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.config import load_config
from ai_pod_cli.container import build_container, Pod
from modules.component_a import ComponentA
from modules.component_b import ComponentB

def run(ctx: PipelineContext):
    config = load_config()
    container = build_container(config)
    S = Pod(container)

    # Chain execution with pipe syntax
    (S(ComponentA) | S(ComponentB)).execute_all(ctx)

    # Conditional branching
    if ctx.get("some_flag"):
        (S(ComponentC)).execute_all(ctx)

    return ctx.summary()
```

### Entry Point (using PipelineRunner)

```python
from ai_pod_cli.runner import PipelineRunner

runner = PipelineRunner()  # reads routes.toml

# CLI example
result = runner.run("my_route", {"key": "value"})

# Flask example
@app.route("/execute/<route>", methods=["POST"])
def execute(route):
    params = request.get_json() or {}
    return jsonify(runner.run(route, params))
```

## Rules — DO and DON'T

### DO

- ✅ Use `@inject` on all `__init__` methods
- ✅ Put ONLY component types in `@inject` constructor params
- ✅ Use `ConfigStore` for all configuration values
- ✅ Use `ctx.params` for entry parameters
- ✅ Use `ctx.set()` / `ctx.get()` for inter-component data
- ✅ Use `ctx.record_step()` to track execution
- ✅ Return `ctx.summary()` from pipeline `run()` functions
- ✅ Import components from their correct paths (`modules.xxx` or `ai_pod_cli.xxx`)

### DON'T

- ❌ Put `str`, `int`, `bool` etc. in `@inject` constructor params
- ❌ Use `os.environ.get()` for config (use ConfigStore instead)
- ❌ Manually instantiate dependencies (let DI container do it)
- ❌ Use `eval()`, `exec()`, `compile()`, `__import__()` (AST blocks these)
- ❌ Access dunder attributes: `__subclasses__`, `__mro__`, `__globals__`, `__builtins__`
- ❌ Execute pipelines directly from `build` (it only generates)

## Key APIs

### PipelineContext

| Method | Purpose |
|--------|---------|
| `ctx.params` | Entry parameters dict |
| `ctx.set(key, value)` | Write to shared data pool |
| `ctx.get(key, default)` | Read from shared data pool |
| `ctx.record_step(id, result)` | Record execution step |
| `ctx.summary()` | Return execution summary dict |

### ConfigStore

| Method | Purpose |
|--------|---------|
| `get("section.key", default)` | Dot-notation config access |
| `get_section("section")` | Get entire section as dict |
| `sections()` | List all section names |
| `reload()` | Reload config.toml from disk |

### Pod (pipe wrapper)

| Method | Purpose |
|--------|---------|
| `S = Pod(container)` | Create wrapper |
| `S(ComponentClass)` | Get pipeable reference |
| `(S(A) \| S(B)).execute_all(ctx)` | Chain and execute |

### PipelineRunner

| Method | Purpose |
|--------|---------|
| `PipelineRunner()` | Create, reads routes.toml |
| `route_names()` | List all route names |
| `run("name", params)` | Execute pipeline by route |

## Architecture

```
Generation Time (AI)              Runtime (Developer)
─────────────────────              ─────────────────────
init "desc"  → entry file         python app.py
create       → components            ↓
build "cmd"  → pipeline + route    PipelineRunner
                                      ↓
                                   routes.toml lookup
                                      ↓
                                   load pipeline.run(ctx)
                                      ↓
                                   DI container assembles
                                      ↓
                                   Pipe chain executes
                                      ↓
                                   PipelineContext flows data
```

## File Structure

```
project/
├── app.py / cli.py / consumer.py   ← AI-generated entry point
├── config.toml                      ← Project config (developer + AI)
├── routes.toml                      ← Route → pipeline mapping
├── beans_config.json                ← Component registry (AI maintains)
├── .env                             ← LLM API config (not committed)
├── modules/                         ← AI-generated components
│   └── *.py
└── pipelines/                       ← AI-generated pipelines
    └── *.py
```

Framework source (`ai_pod_cli/`) is installed via pip, not part of the user's project.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `TypeError: 'str' cannot be interpreted as int` | `--param` value is string, code expects int | Values auto-convert via `json.loads`, pass `--param count=5` (no quotes) |
| `KeyError: Route not found` | Pipeline not registered | Run `build` first, or check `routes.toml` |
| `ModuleNotFoundError` for generated module | `modules/` not on sys.path | Ensure `os.getcwd()` is in sys.path, or run from project root |
| Empty `data.db` (0 bytes) | `@inject` constructor has `str` param, injector passes empty string | Remove primitive types from `@inject` constructor, use ConfigStore |
| AST security block | Generated code uses eval/exec/dunder | Rewrite component to avoid blocked patterns |
