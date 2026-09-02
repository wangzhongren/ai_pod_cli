<p align="center">
  <img src="docs/assets/aipod-icon.png" alt="AIPod" width="128">
</p>

<h1 align="center">AIPod</h1>

<p align="center"><strong>A governed software construction agent and compositional runtime for AI-built Python applications.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/AIPodCli/"><img alt="PyPI" src="https://img.shields.io/pypi/v/AIPodCli"></a>
  <a href="https://pypi.org/project/AIPodCli/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/AIPodCli"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

AIPod lets AI build ordinary Python applications inside a small, explicit architecture.
The model generates one bounded artifact at a time; local code controls ordering,
Contracts, validation, freezing, retries, and repair scope.

```text
Model → Provider → Service → Pipeline → Interface
 data    capability    business    composition    delivery
```

The result is not an opaque AI session. It is a resumable project with typed boundaries,
registered routes, runtime evidence, and generated source code that can be inspected and
maintained with normal tools.

> AIPod is currently alpha software. Review generated code and platform installers before
> production use.

## Quick start

Python 3.10 or newer is required.

```bash
pip install -U AIPodCli

mkdir todo-app
cd todo-app
aipod init
```

Configure an OpenAI-compatible model endpoint once:

```bash
aipod config set OPENAI_API_KEY sk-your-key
aipod config set OPENAI_BASE_URL https://api.openai.com/v1
aipod config set OPENAI_MODEL your-model
```

Build an application:

```bash
aipod pod --yes \
  "Create a local todo application with persistent tasks, add/list/complete routes, and a CLI Adapter."
```

Inspect and test it:

```bash
aipod inspect --summary --json
aipod inspect project --json
aipod interface list
aipod interface smoke <interface-name>
```

Run a generated Interface Adapter with a JSON event:

```bash
aipod interface run <interface-name> \
  --payload '{"action":"list"}'
```

Adapters that expose CLI-style arguments can receive raw arguments after `--`:

```bash
aipod interface run <interface-name> -- \
  --mode once \
  --payload '{"message_id":"m-1","topic":"orders","payload":{}}'
```

## Why AIPod

Large AI-generated applications usually fail at their boundaries:

- one component produces `shipment_count` while another expects `shipments_count`;
- a Model is accidentally injected as an infrastructure dependency;
- generated code imports symbols from the wrong package;
- one Service injects and directly executes another Service, bypassing Pipeline governance;
- a downstream failure causes an upstream working file to be rewritten;
- a Pipeline exists but no verified user-facing Interface reaches it;
- syntax checks pass while imports or dependency injection fail at runtime.

AIPod addresses these failures with five rules:

1. **Build in dependency order.** Earlier layers are completed before downstream layers.
2. **Make boundaries machine-readable.** IDs, dependencies, Contracts, routes, lifecycle,
   permissions, and verification commands are stored as project state.
3. **Freeze accepted upstream work.** A downstream failure may retry or repair its own
   scope, but does not silently reopen stable layers.
4. **Require evidence before completion.** Generated artifacts must pass local structural
   and disposable runtime checks before a stage can freeze.
5. **Keep orchestration out of Services.** A Service can see its Contract, Models, and
   Providers, but never another Service. Composition belongs exclusively to Pipelines.

## The five layers

### Model

Models are shared typed data. Runtime value objects and persistent SQLModel entities are
both supported.

```python
from ai_pod_cli import Model


class Message(Model):
    message_id: str
    topic: str
    payload: dict
```

Models are imported as data types. They are never injected.

### Provider

Providers expose infrastructure capabilities such as files, databases, HTTP clients, or
message transports. They may be injected into Services.

Built-in Service-visible Providers include:

- `ConfigStore`
- `ModelRepository`

`PipelineRunner` is a reserved Runtime capability used behind Interface and CLI route
boundaries. It is not a way for one Service to reach another Service.

### Service

Services implement business transformations through `execute(ctx)`.

```python
from ai_pod_cli.context import PipelineContext


class MessageProcessingService:
    def execute(self, ctx: PipelineContext) -> dict:
        message_id = ctx.get("message_id")
        result = {"message_id": message_id, "status": "processed"}
        ctx.set("result", result)
        return result
```

A Service has a deliberately narrow capability view:

| Visible to a Service | Hidden from a Service |
|---|---|
| Its input/output Contract | Other Services |
| Frozen Models as imported data types | `modules.services.*` imports |
| Providers declared as DI dependencies | Service construction and `execute()` calls |
| `PipelineContext` data | Pipeline scheduling, loops, parallelism, retries between Services |

Service-to-Service dependencies are rejected independently by the Planner, Canonical
Plan reducer, source validator, and DI Runtime. A Service must not become a hidden
orchestrator:

```python
# Invalid: this bypasses Contract checks, Trace, Failure, retry, and execution policy.
class GameLoopService:
    def __init__(self, physics_service, render_service):
        self.physics_service = physics_service
        self.render_service = render_service

    def execute(self, ctx):
        self.physics_service.execute(ctx)
        return self.render_service.execute(ctx)
```

### Pipeline

Pipelines compose Services in deterministic order and are registered as named routes.

```python
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import Pod, build_container


def run(ctx):
    S = Pod(build_container(load_beans()))
    (S(ValidateMessage) | S(ProcessMessage)).execute_all(ctx)
    return ctx.summary()
```

Interfaces see route names and descriptions, not Service classes.

The same Pipeline Runtime supports governed asynchronous, parallel, repeated, and
streaming execution. Operators are explicit:

| Runtime declaration | Meaning |
|---|---|
| `A \| B` | Deterministic sequential composition |
| `parallel(A, B)` | Isolated concurrent branches with an explicit merge policy |
| `repeat(frame, ...)` | Governed repetition controlled by Context fields |
| `stream(source)` | Bounded asynchronous event processing with backpressure |

Existing synchronous Pipelines remain compatible. AI declares these policies, while the
local Runtime owns scheduling, merging, stopping, cancellation, and Trace.

```python
from ai_pod_cli.container import parallel


async def run(ctx):
    S = Pod(build_container(load_beans()))
    flow = parallel(
        S(QueryInventory),
        S(QueryPrice),
        merge="strict",
        failure_policy="collect_all",
        concurrency=2,
    ) | S(BuildResponse)
    await flow.execute_all_async(ctx)
    return ctx.summary()
```

Repeated workflows such as game frames, workers, polling, and bounded retries remain
visible in the Pipeline instead of being hidden inside a coordinating Service:

```python
from ai_pod_cli.container import Pod, build_container, repeat


def run(ctx):
    S = Pod(build_container(load_beans()))
    frame = (
        S(InputHandlingService)
        | S(SceneUpdateService)
        | S(PhysicsService)
        | S(RenderService)
    )
    repeat(
        frame,
        until_field="quit_requested",
        max_iterations_field="max_frames",
        output_field="executed_frames",
        trace_limit=20,
    ).execute_all(ctx)
    return ctx.summary()
```

The stop condition is a named Context field, not an arbitrary AI-generated callback.
Each iteration uses an isolated Context snapshot, merges successful writes
deterministically, stops on `Failure`, and retains only a bounded number of iteration
traces.

See
[docs/execution.md](https://github.com/wangzhongren/ai_pod_cli/blob/main/docs/execution.md)
for async routes, deterministic branch merging, repetition, stream processing, failure
policies, and Contract behavior.

### Interface

An Interface is a delivery bundle around one AI-generated project Adapter. It can bridge
any external event source to frozen Pipeline routes:

```text
CLI arguments    \
HTTP request      \
queue message      ─→ InterfaceAdapter ─→ context.run_route() ─→ Pipeline
desktop UI event  /
file/timer event /
```

AIPod provides the stable SDK:

```python
from ai_pod_cli.interface import InterfaceAdapter, InterfaceContext
```

AI generates project-specific glue:

```python
class GeneratedInterfaceAdapter(InterfaceAdapter):
    def required_routes(self):
        return ["process_message"]

    def start(self, context: InterfaceContext, payload=None):
        message = receive_external_message(payload)
        return context.run_route("process_message", message)
```

The Adapter cannot import Models, Providers, Services, the DI container, or
`PipelineRunner`. Its only business capability is `InterfaceContext.run_route()`.

## Multi-file Interface Adapters

Complex adapters are split into focused files and generated one file per model call:

```text
interfaces/order-monitor/
├── adapter.py          Adapter entry class
├── queue_consumer.py   message transport
├── window.py           desktop UI
├── event_bridge.py     thread/UI bridge
├── install.ps1         platform lifecycle
└── interface.json      canonical manifest
```

The manifest identifies the entry source and class:

```json
{
  "name": "order-monitor",
  "kind": "windows_desktop_queue",
  "platform": "windows",
  "adapter": {
    "entry_path": "interfaces/order-monitor/adapter.py",
    "class_name": "GeneratedInterfaceAdapter"
  },
  "artifacts": [
    {"path": "interfaces/order-monitor/adapter.py", "role": "adapter_entry"},
    {"path": "interfaces/order-monitor/queue_consumer.py", "role": "adapter_module"},
    {"path": "interfaces/order-monitor/window.py", "role": "adapter_module"}
  ],
  "lifecycle": {
    "run": ["{python}", "-m", "ai_pod_cli", "interface", "run", "order-monitor"]
  },
  "permissions": ["message_queue_connect", "desktop_notification"],
  "verify": [
    {
      "name": "adapter_smoke",
      "kind": "runtime",
      "required": true,
      "command": ["{python}", "-m", "ai_pod_cli", "interface", "smoke", "order-monitor"],
      "timeout": 30
    }
  ]
}
```

All Adapter source files are staged together, loaded as a private Python package so
relative imports work, and smoked in a disposable project. The complete Interface bundle
is committed atomically only after every required check passes.

The Adapter is generated during construction. Running the finished application does not
call AI.

## Pod Agent

`aipod pod` is a resumable local state machine over governed build tools:

```text
Observe → Policy Select → Execute → Validate → Freeze → Observe
```

The stage order is deterministic:

```text
generate_models
generate_providers
generate_services
compose_pipelines
generate_interfaces
verify_application
repair_current_artifact   # only after real failure evidence
```

The model does not choose this order. It decides the contents of the current bounded
artifact.

### Modifying an existing Pod

Studio and `--stage auto` use one focused AI call to classify the earliest layer affected
by a requested change. The local scheduler then freezes upstream and rebuilds that layer
plus downstream:

```bash
aipod pod --stage auto --yes \
  "Add task priority and display it in the desktop window."
```

An explicit stage remains available as a manual override:

```bash
aipod pod --stage interfaces --yes \
  "Replace the CLI Adapter with a desktop and message-queue Adapter."
```

## Progressive verification

Validation happens before freezing, not only at the end:

| Layer | Required evidence |
|---|---|
| Model | isolated import and class construction |
| Provider | isolated import, DI construction, declared-method smoke |
| Service | no Service visibility; DI construction and `execute(ctx)` with Contract-derived input |
| Pipeline | isolated sequential/parallel/repeat/stream execution before route registration |
| Interface | every Artifact validated, Adapter package imported, smoke executed |

After all layers complete, every required Interface verification command runs again.
Optional installation checks remain visible but do not fail runtime proof.

Run structure-only inspection:

```bash
aipod verify --json
```

A structure-only result is `unverified`, not `passed`.

Run a real command:

```bash
aipod verify --json -- python -m unittest
```

Verification records the command, exit code, bounded stdout/stderr, project-local
traceback locations, repair candidates, and a source fingerprint. A stale pass is reset
to `pending` when relevant project files change.

## Contracts

Components publish machine-readable inputs and outputs. AIPod validates:

- required fields;
- scalar and structured types;
- shared Model paths;
- nested schemas;
- Pipeline data flow;
- runtime values at component boundaries;
- Service visibility (`Service → Service` is always invalid);
- deterministic branch merges and bounded repeat traces.

Type, Model, missing-field, and nested-schema conflicts are errors. Similar-but-different
field names are warnings because semantic similarity is heuristic.

## Native Studio

Install Studio support and open a project:

```bash
pip install "AIPodCli[studio]"
aipod studio .
```

<p align="center">
  <img src="docs/assets/aipod-studio.png" alt="AIPod Studio" width="920">
</p>

Studio provides:

- project switching and initialization;
- Model, Provider, Service, Pipeline, and Interface visualization;
- AI component creation and visual Pipeline composition;
- Pod build progress, cancellation, and stage evidence;
- source inspection;
- Interface Adapter, lifecycle, permission, and verification inspection;
- program output and persisted run traces.

## Project structure

```text
project/
├── aipod_plan.json          resumable Plan and public Agent state
├── beans_config.json        Bean registry and Contracts
├── config.toml              project configuration
├── routes.toml              route-to-Pipeline registry
├── requirements.txt         project-specific dependencies
├── modules/
│   ├── models/
│   ├── providers/
│   └── services/
├── pipelines/
├── interfaces/
│   └── <interface-id>/
│       ├── interface.json
│       ├── adapter.py
│       └── additional Adapter modules and lifecycle files
├── docs/aipod/              generated human-readable plans
└── .aipod/runs/             redacted execution traces
```

## CLI reference

| Command | Purpose | Uses AI |
|---|---|:---:|
| `aipod init [--install-deps]` | Initialize a project | No |
| `aipod pod DESC [--file FILE] [--stage auto|LAYER] [--yes]` | Build or modify a complete Pod | Yes |
| `aipod create --category TYPE --name NAME --desc DESC` | Generate one component | Yes |
| `aipod add --category TYPE --name NAME --class-path PATH --desc DESC` | Register existing code | No |
| `aipod compose CMD [--name ROUTE]` | Generate and register a Pipeline | Yes |
| `aipod interface list` | List Interface manifests | No |
| `aipod interface run NAME [--payload JSON] [-- ARGS...]` | Run a frozen Adapter | No |
| `aipod interface smoke NAME` | Execute Adapter smoke | No |
| `aipod interface install/uninstall NAME` | Execute declared lifecycle command | No |
| `aipod run ROUTE --params JSON` | Run one Pipeline route | No |
| `aipod inspect [TARGET] [NAME] --json` | Read project state | No |
| `aipod verify --json -- COMMAND...` | Produce runtime and repair evidence | No |
| `aipod visualize [--output FILE] [--open]` | Export the project graph | No |
| `aipod studio [PATH]` | Open native Studio | No |
| `aipod config set/get/remove/list/path` | Manage model configuration | No |

`aipod entry` remains available for legacy standalone entry generation. New Pod projects
should use Interface Adapters.

## Configuration

Global model configuration is stored outside individual projects. Environment variables
or a local `.env` override saved values:

```text
OPENAI_API_KEY
OPENAI_BASE_URL
OPENAI_MODEL
OPENAI_TIMEOUT_SECONDS
```

The PyPI distribution is named `AIPodCli`; the Python import package is `ai_pod_cli`.

Python and Node.js use the same global model configuration file:

```text
~/.aipod/config.toml

[env]
OPENAI_API_KEY = "..."
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_MODEL = "your-model"
OPENAI_TIMEOUT_SECONDS = "120"
```

They also share project-level `config.toml` and dot-notation `ConfigStore` access. Process
environment variables override project `.env`, which overrides global `[env]` values.

## Security and trust boundary

AIPod provides governance, not hostile-code isolation.

- Generated code is checked structurally and executed in disposable project copies before
  freezing.
- Adapter code can see routes but is prohibited from importing Services or runtime
  internals.
- Service code can see Models and Providers but is prohibited from importing, injecting,
  constructing, or invoking another Service.
- Generated lifecycle files must be reviewed before changing system integration.
- The final application is ordinary Python and runs with the current user's permissions.
- Third-party packages and remote model providers remain separate trust boundaries.

Do not treat generated code as safe for production without review, platform permissions,
and deployment isolation appropriate to the application.

## Current boundaries

- Synchronous code cannot safely force an async Pipeline inside an already-running event
  loop; async callers must use `PipelineRunner.run_async()`.
- Stream processing is in-process and bounded, but durable offsets, distributed workers,
  and exactly-once delivery remain responsibilities of the selected queue/provider.
- Parallel execution isolates Context data, but external side effects still require
  idempotency and transaction design in the application Services.
- `repeat` is an in-process governed loop. Distributed scheduling, durable checkpoints,
  and process supervision remain deployment concerns.
- Contract analysis cannot prove arbitrary Python semantics.
- Synthetic smoke cannot prove access to real external databases, queues, accounts, or
  operating-system permissions.
- Complex platform installation may require explicit manual steps, signing, entitlements,
  or user approval.
- Model providers may time out or truncate large generations; Pod state remains resumable.

## Development

### Node.js subproject

An initial TypeScript implementation lives in [`aipod-node/`](aipod-node/). It provides
the governed Runtime foundation—Service isolation, Contracts, sequential and parallel
Pipelines, `repeat`, async streams, and route dispatch—plus a resumable five-stage AI
construction Agent, complete generated-project semantic type checking, a persistent
HTTP Broker/Worker runtime, CLI, and local browser-based Studio.

```bash
cd aipod-node
npm install
npm test
```

See [`aipod-node/README.md`](aipod-node/README.md) for its current scope and API.

```bash
git clone https://github.com/wangzhongren/ai_pod_cli.git
cd ai_pod_cli
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[studio]"
python -m unittest tests.test_runtime
```

Build and validate the package:

```bash
python -m build
python -m twine check dist/*
```

## License

[MIT](LICENSE)
