# AIPod — AI Agent Skill Reference

## What Is This

AIPod is an **AI-native application framework** where AI builds software and a dependency-injected runtime executes it.

AI generates **reusable components** (not one-off scripts) and **execution pipelines** (not glue code). The runtime assembles and executes them deterministically.

**Core principle: generation and execution are separated.** AI generates → developer reviews → runtime executes.

## Installation

```bash
# Python 3.10+; install the runtime dependencies explicitly for now.
pip install aipodcli openai injector python-dotenv tomlkit
```

CLI entry point: `aipod`

## Command Decision Tree

```
User wants to...                        → Command
─────────────────────────────────────────────────────
Start a new project                     → aipod init
Create a new component                  → aipod create --category service|provider --name Y --desc "Z"
Register hand-written component         → aipod add --name X --class-path Y --desc "Z"
Generate a pipeline (no execution)      → aipod compose "instruction" --name X
List generated pipelines               → aipod compose --list
Run a pipeline                          → User runs their entry point (e.g. python app.py route_name)
```

## Commands

### `aipod init`

- Creates `modules/`, `pipelines/`, `config.toml`, `routes.toml`, `beans_config.json`
- Does not call AI and does not generate an entry point
- Use `aipod entry "project description"` to generate an entry point, or use `aipod pod "requirement"` for the full flow

### `aipod create --category service --name ComponentName --desc "description"`

- `--category service`: business component with `execute(ctx)`; services can be placed in pipelines
- `--category provider`: infrastructure entity with custom methods (no `execute`); providers are injected into services
- AI auto-selects dependencies from bean pool
- AI auto-suggests config.toml entries via `config_additions`
- AST security check before writing
- Generated to `modules/<name>.py`, registered in `beans_config.json`

### `aipod add --name ClassName --class-path package.module.ClassName --desc "description"`

- Register manually-written components so AI knows about them

### `aipod compose "business instruction" --name pipeline_name`

- AI analyzes instruction, plans component execution chain
- Generates `pipelines/<name>.py` with pipe syntax
- Registers route in `routes.toml`
- **Does NOT execute** — developer runs via entry point

### `aipod compose --list`

- List all saved pipelines

## Code Templates

### Service Component (has `execute`)

```python
from injector import inject
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.config_store import ConfigStore

class MyComponent:
    @inject
    def __init__(self, dep_a: DepA, config_store: ConfigStore):
        self.dep_a = dep_a
        self.setting = config_store.get("section.key", "default")

    def execute(self, ctx: PipelineContext) -> dict:
        value = ctx.params.get("key")
        upstream = ctx.get("upstream_key")
        result = self.dep_a.process(value)
        ctx.set("my_output", result)
        return {"status": "success"}
```

### Provider Component (no `execute`, custom methods)

```python
from injector import inject
from ai_pod_cli.config_store import ConfigStore

class MyProvider:
    @inject
    def __init__(self, config_store: ConfigStore):
        path = config_store.get("my.path", "/default")

    def query(self, ...) -> ...:
        ...
```

### Pipeline File

```python
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import build_container, Pod
from modules.services.component_a import ComponentA
from modules.services.component_b import ComponentB

def run(ctx: PipelineContext):
    beans = load_beans()
    S = Pod(build_container(beans))

    # Pipe syntax: A → B → C
    (S(ComponentA) | S(ComponentB) | S(ComponentC)).execute_all(ctx)

    # Conditional
    if ctx.get("alert_needed"):
        (S(Notifier)).execute_all(ctx)

    return ctx.summary()
```

### Entry Point (using PipelineRunner)

```python
from ai_pod_cli.runner import PipelineRunner

runner = PipelineRunner()  # reads routes.toml
result = runner.run("route_name", {"key": "value"})
```

## Rules — DO and DON'T

### DO

- ✅ Use `@inject` on all `__init__` methods
- ✅ Only component types in `@inject` constructor params
- ✅ Use `ConfigStore` for configuration values
- ✅ Use `ctx.params` for service input parameters
- ✅ Use `ctx.set()` / `ctx.get()` for inter-component data
- ✅ Return `ctx.summary()` from pipeline `run()` functions
- ✅ Import components from correct paths (`modules.xxx` or `ai_pod_cli.xxx`)

### DON'T

- ❌ Put `str`, `int`, `bool` in `@inject` constructor params
- ❌ Use `os.environ.get()` for config (use ConfigStore)
- ❌ Manually instantiate dependencies (let DI handle it)
- ❌ Use `eval()`, `exec()`, `compile()`, `__import__()` (AST blocks these)
- ❌ Access dunder attributes (`__subclasses__`, `__mro__`, `__globals__`)

## Key APIs

| Class | Key Methods |
|-------|------------|
| `PipelineContext` | `ctx.params`, `ctx.set(k,v)`, `ctx.get(k,d)`, `ctx.summary()` |
| `ConfigStore` | `get("section.key", default)`, `get_section("name")`, `sections()`, `reload()` |
| `Pod` | `S = Pod(container)`, `S(Class)`, `(S(A) \| S(B)).execute_all(ctx)` |
| `PipelineRunner` | `PipelineRunner()`, `route_names()`, `run("name", params)` |

## Configuration Files

| File | Purpose | Maintained by |
|------|---------|---------------|
| `.env` | LLM API credentials | Developer |
| `config.toml` | Project settings | Developer + AI |
| `routes.toml` | Pipeline → route mapping | AI + Developer |
| `beans_config.json` | Component registry | AI |

## Architecture

```
Generation (AI)                    Execution (Runtime)
─────────────────                  ─────────────────────
init          → project skeleton    python app.py
create       → components            ↓
compose      → pipeline + route    PipelineRunner
entry "desc" → entry point
                                     ↓
Developer reviews & commits        routes.toml lookup
                                     ↓
                                   load pipeline.run(ctx)
                                     ↓
                                   DI container assembles
                                     ↓
                                   Pipe chain executes
                                     ↓
                                   PipelineContext flows data
```

## Project Structure (user's project)

```
project/
├── app.py / cli.py / consumer.py   ← generated by `entry` or `pod`
├── config.toml                      ← Project config
├── routes.toml                      ← Route → pipeline mapping
├── beans_config.json                ← Component registry
├── .env                             ← LLM API config (not committed)
├── modules/                         ← AI-generated components
│   ├── providers/                   ← infrastructure components
│   └── services/                    ← business components with execute(ctx)
└── pipelines/                       ← AI-generated pipelines
    └── *.py
```

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `TypeError: 'str' cannot be int` | Param value is string, code expects int | Values auto-convert via `json.loads` |
| `KeyError: Route not found` | Pipeline not registered | Run `compose` first |
| `ModuleNotFoundError` | `modules/` not on path | Run from project root |
| Empty `data.db` | `@inject` has `str` param → injector passes empty string | Use ConfigStore, not constructor params |
| AST security block | Code uses eval/exec/dunder | Rewrite to avoid blocked patterns |
