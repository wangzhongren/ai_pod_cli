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
    {"path": "interfaces/order-monitor/window.py", "role": "adapter_module"},
    {"path": "interfaces/order-monitor/test_behavior.py", "role": "behavior_test", "format": "python"}
  ],
  "lifecycle": {
    "run": ["{python}", "-m", "ai_pod_cli", "interface", "run", "order-monitor"]
  },
  "permissions": ["message_queue_connect", "desktop_notification"],
  "verify": [
    {
      "name": "adapter_smoke",
      "kind": "smoke",
      "required": true,
      "command": ["{python}", "-m", "ai_pod_cli", "interface", "smoke", "order-monitor"],
      "timeout": 30
    },
    {
      "name": "order_behavior",
      "kind": "behavior",
      "required": true,
      "command": ["{python}", "-m", "ai_pod_cli.behavior_tests", "interfaces/order-monitor/test_behavior.py"],
      "cases": [{"test": "OrderBehavior.test_process_message", "requirement": "A supplied order message produces the expected order state"}],
      "timeout": 60
    }
  ]
}
```

All Adapter source files are staged together, loaded as a private Python package so
relative imports work, and smoked in a disposable project. The complete Interface bundle
is committed atomically after artifact and Adapter checks. Application completion
additionally requires the declared behavior acceptance below.

The Adapter is generated during construction. Running the finished application does not
call AI.

## Python source output protocol

Python component generation (`pod` and `create`), Pipeline composition, Interface
delivery files, and the legacy entry generator separate metadata from source:

1. Generate JSON metadata such as Contracts, dependencies, method signatures,
   configuration additions and extra packages, without a `code` or `content` field.
2. Freeze that metadata and request one XML-like source artifact through text mode:

```xml
<create>
  <path>modules/services/priceorder.py</path>
  <content><![CDATA[
class PriceOrder:
    def execute(self, ctx):
        return {"total_cents": 9000}
]]></content>
</create>
```

The source response is not forced into JSON. The planned path must match exactly;
multiple actions, nested operands, extra fields and malformed XML are rejected.
If a metadata response also includes unrequested root-level `code` or `content`,
those fields are discarded. Source is still requested separately through XML;
discarded metadata code is never executed or used as a fallback. Contract fields
named `code` or `content` inside inputs/outputs are preserved.
CDATA splitting supports literal `]]>` in source, and the codec preserves source line endings.
This uses the same strict ActUnit-compatible artifact subset as AIPod Node, implemented
locally without requiring an unpublished ActUnit package or a Node process.

Source format failures retry against the frozen metadata. Existing component checks,
disposable runtime verification and Interface bundle validation still apply. Planning
and exact-patch repair continue to use JSON. Python generation now normally needs two
model calls per artifact because its metadata was previously generated with the source;
this change does not claim a reduction in latency or model cost.

Source requests default to a 65,536-token output budget and a 600-second SDK timeout.
JSON metadata, planning and patch requests default to 32,768 tokens; the model client
default timeout is 600 seconds. Internal stage-specific small limits have been removed. `generate_source()` accepts
`source_max_tokens` and `source_timeout_seconds` overrides for controlled experiments
or callers with different limits. The output budget can include model reasoning tokens;
it does not represent the length of the generated source alone.

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
| Pipeline | isolated execution with explicit real entry parameters before route registration |
| Interface | every Artifact validated, Adapter package imported, smoke executed |

After all layers complete, every required Interface verification command runs again.
Optional installation checks remain visible but do not fail runtime proof.

Pipeline planning declares public `inputs` and non-empty `verification_cases`, each with
a unique `name` and explicit `params`. A default-starting application includes an empty
parameter scenario; a route needing user input provides concrete values for that input.
The Pipeline sandbox never derives sample parameters or files from downstream Service
requirements. Each case uses its own disposable project, and exceptions, timeouts,
unhandled Runtime Failure records, and premature process exits fail the check.
Cases remain frozen while source is repaired. Reusing an existing Pipeline also reruns
its entry scenarios. Public inputs are saved beside the source in `.contract.json` and
referenced by `routes.toml`; inferred internal requirements do not replace this boundary.

An entry scenario is a smoke check. Every Interface must also declare a required
`kind: "behavior"` command using the framework's unittest driver, plus named `cases`
mapping test methods to requirements. Plans missing these checks are rejected, and
old smoke-only passes are invalidated. The driver is available directly:

```bash
python -m ai_pod_cli.behavior_tests tests/test_behavior.py
```

Each test uses a real registered route and `self.assert*` assertions on observed behavior.
The driver rejects empty tests, missing real route execution, ignored framework failures,
and tests containing only obviously constant assertions. Bare Python `assert` does not
count as unittest evidence. Every declared case must execute and pass; a missing or skipped
case cannot be replaced by another passing test. JSON proof includes per-test execution
and assertion counts. Acceptance test files are excluded from automatic repair targets.

Python Pod allows up to 10 application repair attempts, including rejected patches,
and verifies again after every applied repair. Attempts persist across resumes;
existing plans count their already-applied repairs toward this limit. The default
scheduler budget is 40 steps so all 10 rounds and the final verification can run.

These are minimum evidence checks, not a proof of complete requirements coverage or
arbitrary test correctness. The planner must declare meaningful scenarios and expected
outcomes; reviewers can inspect the frozen cases. Pure exception-only tests currently
need a successful route invocation in the same test to satisfy the execution evidence.

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

## Shared utility classes

Each project has one `utility_registry.json` shared by its generators. Registered
Python utility classes live in `modules/utils/` and are ordinary imports. They are
not DI beans, an additional runtime layer, or a separate Agent. Providers, Services,
Pipelines, Interface adapters, and suitable Model calculations can all reuse them.

The registry records each class's purpose, exact source path, public static-method
signatures and documentation, source hash, dependencies, and executable examples.
The signatures are extracted from source, not trusted from model metadata. A changed
source hash makes the entry invalid until it is updated through the registry.

During planning and metadata generation, the existing Agent can select three tools:

- `list_utilities`: search the shared directory by purpose or API.
- `read_utility`: inspect registered source, signatures, and current callers.
- `write_utility`: generate a missing pure helper through XML, run its declared cases,
  and register it only after validation. The model cannot overwrite an existing ID.

The tool loop permits eight actions before final metadata; a generation that does not
need a tool retains its original model-call count. The later XML source request sees
the latest directory and relevant tool reads. Successfully verified utilities remain
available even if a later component fails to generate.

Reusable calculations, parsing and formatting belong here. Resource access stays in
Providers, domain decisions in Services, and orchestration in Pipelines. Utilities
must not access Context, project components, files, networks, processes, or model APIs.
Use the framework's existing Context, Contract and Model conversion APIs directly
instead of creating another compatibility layer in a utility.

For example, multiple components can use this one registered class:

```python
from modules.utils.numbertools import NumberTools

bounded = NumberTools.clamp(value, 0, 10)
```

Human-authored sources can use the same registry without a model:

```bash
aipod utility list --query clamp
aipod utility read NumberTools
aipod utility write NumberTools --file /path/to/numbertools.py \
  --description "Shared numeric bounds" --cases /path/to/cases.json
aipod inspect utilities --json
```

`cases.json` supplies real invocations covering every public static method:

```json
[{"method":"clamp","args":[20,0,10],"expected":10}]
```

Cases can also provide `kwargs` or an expected exception with `raises`. Candidate
code is imported and executed in a disposable project; failed cases never publish
source or registry changes. Plain helper methods have annotated parameters and
returns, and their examples must leave caller inputs unchanged.

An explicit compatible update requires `--expected-sha256` and a caller verification
command after `--verify`. The old examples remain active; both examples and that
command must pass in an isolated candidate project before publication. Incompatible
method signatures need a new utility ID. Automatic one-file application repairs do
not patch shared utility sources behind the registry's back.

These checks enforce the supported utility subset and supplied examples; they do not
prove arbitrary code purity or exhaustive behavior coverage. Python and TypeScript
use the same directory concept with language-specific implementations and signatures.

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

Python Contracts normalize equivalent structured schemas and type annotations before
checking them. For example, these describe the same list of shared Models:

```python
{"type": "array", "items": {"model": "modules.models.gameentity.GameEntity"}}
"List[modules.models.gameentity.GameEntity] — entities in the current frame"
```

Supported annotations include `List[T]`, `list[T]`, `typing.List[T]`, `Dict[str, T]`,
`Optional[T]`, `Union[A, B]`, and `T | None`, including nested combinations. Normalization
preserves Model path capitalization, item schemas, and `required`, `default`, and
description metadata. Static Pipeline checks, runtime validation and Model
materialization, and verification samples all use this same representation.

This is a small Contract syntax, not full Python typing or JSON Schema support.
Unknown legacy type names and unsupported generic tokens such as `Tuple[int, int]`
remain compatible as type labels; retaining a label does not validate its elements.
Malformed supported generics and executable type expressions are rejected.

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
OPENAI_TIMEOUT_SECONDS = "600"
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
