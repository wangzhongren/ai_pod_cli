# AIPodCli Skill Reference

## What Is This

AIPodCli is an AI-native Python development framework. AI generates components, plans pipelines, and the framework handles DI assembly and execution at runtime.

## Framework Commands

### `init` — Initialize Project

```bash
python -m ai_pod_cli init "project description"
```

- Creates project skeleton: `modules/`, `pipelines/`, `config.toml`, `routes.toml`, `beans_config.json`
- If description is provided, AI decides project type (Flask/CLI/RabbitMQ/Kafka/APScheduler...) and generates the entry point file
- Entry point uses `PipelineRunner` to dispatch to pipelines

### `create` — AI Generate Component

```bash
python -m ai_pod_cli create --category entry --name ComponentName --desc "description"
```

- `--category entry`: business component with `execute(ctx)` method
- `--category entity`: infrastructure entity with custom methods (no `execute` required)
- AI auto-selects dependencies from the bean pool
- AI auto-suggests new config.toml entries via `config_additions`
- AST security check before writing
- Generated code goes to `modules/<name>.py`
- Registered in `beans_config.json`

### `add` — Register Human-Written Component

```bash
python -m ai_pod_cli add --name ClassName --class-path package.module.ClassName --desc "description"
```

- For components you write manually
- Registers in `beans_config.json` so AI can see it during `create` and `build`

### `build` — AI Generate Pipeline

```bash
python -m ai_pod_cli build "business instruction" --name pipeline_name
python -m ai_pod_cli build --list
```

- AI analyzes instruction, plans component execution chain
- Generates `pipelines/<name>.py` with pipe syntax
- Registers route in `routes.toml`
- Does NOT execute — developer runs via their entry point

## Code Generation Rules

### Component Structure (entry)

```python
from injector import inject
from ai_pod_cli.context import PipelineContext

class MyComponent:
    @inject
    def __init__(self, dep_a: DepA, config_store: ConfigStore):
        self.dep_a = dep_a
        self.config_value = config_store.get("section.key", "default")

    def execute(self, ctx: PipelineContext) -> dict:
        # Read input
        value = ctx.params.get("key")
        upstream_data = ctx.get("upstream_key")

        # Process
        result = self.dep_a.do_something(value)

        # Write output for downstream
        ctx.set("my_output", result)

        return {"status": "success"}
```

### Component Structure (entity)

```python
from injector import inject
from ai_pod_cli.config_store import ConfigStore

class MyEntity:
    @inject
    def __init__(self, config_store: ConfigStore):
        path = config_store.get("my.path", "/default")
        # Initialize with config

    def query(self, ...) -> ...:
        # Custom business methods (no execute required)
```

### Pipeline Structure

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

    # Pipe syntax: execute A then B then C
    (S(ComponentA) | S(ComponentB) | S(ComponentC)).execute_all(ctx)

    # Conditional branching
    if ctx.get("some_condition"):
        (S(Notifier)).execute_all(ctx)

    return ctx.summary()
```

### Constructor Rules

- `@inject` constructor parameters: ONLY component types from bean pool
- NO primitive types (`str`, `int`, `bool`) as constructor params
- Configuration values: use `ConfigStore` (injected via DI)
- No dependencies: `@inject def __init__(self): pass`

## Key Classes

### `PipelineContext` (`ai_pod_cli.context`)

Shared data pipeline between components:
- `ctx.params` — entry parameters (dict)
- `ctx.set(key, value)` — write to shared data pool
- `ctx.get(key, default)` — read from shared data pool
- `ctx.record_step(component_id, result)` — record execution trace
- `ctx.summary()` — return execution summary dict

### `ConfigStore` (`ai_pod_cli.config_store`)

Centralized TOML config reader:
- `config_store.get("section.key", default)` — dot-notation access
- `config_store.get_section("section")` — get entire section as dict
- `config_store.sections()` — list all section names
- `config_store.reload()` — reload config.toml

### `Pod` (`ai_pod_cli.container`)

Pipe-chainable component wrapper:
- `S = Pod(container)` — create wrapper
- `S(ComponentClass)` — get pipeable reference
- `(S(A) | S(B)).execute_all(ctx)` — chain and execute

### `PipelineRunner` (`ai_pod_cli.runner`)

Pipeline loader and executor:
- `runner = PipelineRunner()` — reads routes.toml
- `runner.route_names()` — list all routes
- `runner.run("route_name", params_dict)` — execute pipeline

## Configuration Files

| File | Purpose | Who Maintains |
|------|---------|---------------|
| `.env` | LLM API config | Developer |
| `config.toml` | Project config (DB paths, ports, etc.) | Developer + AI suggestions |
| `routes.toml` | Pipeline route mapping | AI auto-registers + developer edits |
| `beans_config.json` | Component registry | AI maintains |

## Security

AST-based validation on all AI-generated code:
- Blocks: `eval()`, `exec()`, `compile()`, `__import__()`
- Blocks: dunder chain access (`__subclasses__`, `__mro__`, `__globals__`, `__builtins__`)
- Does NOT restrict imports (this is a local dev framework)

## Built-in Entities

| Entity | Methods | Description |
|--------|---------|-------------|
| `ConfigStore` | `get()`, `get_section()`, `sections()`, `reload()` | TOML config reader |
| `DbClient` | `query(sql)` | Mock database client |
| `SmsSender` | `send(phone, msg)` | Mock SMS service |

## Dependencies

| Package | Purpose |
|---------|---------|
| `openai` | LLM API client |
| `injector` | IoC/DI container |
| `python-dotenv` | `.env` loading |
| `tomlkit` | TOML read/write (preserves formatting) |

## LLM Retry

All AI calls use `call_llm()` with built-in retry:
- Default 3 retries with exponential backoff
- Retries on: network errors, invalid JSON, empty `code` field
- Raises `RuntimeError` after exhaustion
