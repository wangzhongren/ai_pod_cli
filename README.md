<p align="center"><img src="docs/assets/aipod-icon.png" alt="AIPod icon" width="132"></p>
<h1 align="center">AIPod</h1>
<p align="center"><strong>A governed, compositional runtime for AI-built Python applications.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/AIPodCli/"><img alt="PyPI" src="https://img.shields.io/pypi/v/AIPodCli"></a>
  <a href="https://pypi.org/project/AIPodCli/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/AIPodCli"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

AIPod does not ask AI to generate a pile of files and hope they work together. It gives AI a small set of architectural primitives, generates them in dependency order, validates each unit in an isolated runtime, and only then allows the unit to become part of the project.

```text
Program = Valid Components + Dependencies + Composition

Model       defines business data
Provider    provides external capability
Service     performs one business transformation
Pipeline    composes transformations into a program
Interface   exposes the program to a user or another system
```

The generated result is ordinary Python. There is no proprietary source format and no required AIPod cloud.

> AIPod is alpha software. Models, dependency injection, sequential Pipelines, lightweight Contracts, isolated generation checks, execution traces, retry/fallback policies, CLI workflows, and the native Studio are implemented. Parallel and event-driven composition, privileged-effect approval, rollback, and compensation remain roadmap work.

## The Problem AIPod Solves

AI can write an individual function well. The difficult part is keeping a growing application coherent:

- one component writes `shipment_count`, while another reads `shipments_count`;
- a Service invents a database method that its Provider does not expose;
- generated code writes raw SQL against a table that does not exist;
- a later repair silently changes an earlier component and breaks a working path;
- a large one-shot response is truncated before the application is complete;
- generated files import successfully but fail as soon as they touch runtime state.

AIPod addresses these failures with four rules:

1. **Generate in dependency order.** Data is frozen before persistence and business logic; business logic is frozen before composition and delivery.
2. **Make boundaries machine-readable.** Every component records its dependencies, inputs, outputs, import path, and category in the Bean Pool.
3. **Run code before accepting it.** A candidate is imported and executed in a disposable project copy before registration.
4. **Repair only the unstable layer.** If a Service fails, AIPod repairs that Service. It does not rewrite frozen Models or Providers.

```text
Plan one stage
      ↓
Generate one component
      ↓
Static Contract checks
      ↓
Disposable runtime execution
      ↓
Pass ──→ freeze and register ──→ next component
  │
  └────→ focused feedback / minimal patch ──→ validate again
```

## The Five-Layer Application Model

```text
Data              Capability          Business            Program             Delivery
Models      ───→  Providers     ───→  Services      ───→  Pipelines     ───→  Interfaces
SQLModel          external systems     use cases            execution graph      CLI / Web
entities          and infrastructure   transformations      and state flow       Desktop / Worker
```

### 1. Model: one definition for data and persistence

AIPod uses [SQLModel](https://sqlmodel.tiangolo.com/) as its default business data primitive. A Model is simultaneously a Python type, a Pydantic validation boundary, and a SQLAlchemy table definition when `table=True`.

```python
from sqlmodel import Field
from ai_pod_cli import Model


class Shipment(Model, table=True):
    id: int | None = Field(default=None, primary_key=True)
    tracking_number: str = Field(index=True, unique=True)
    status: str = "pending"
    shipment_count: int = 0
```

This removes drift between a DTO, ORM entity, handwritten table schema, and separate Contract model. The class is the canonical definition.

### 2. Provider: infrastructure, not business logic

A Provider wraps a real external capability such as Redis, a message broker, HTTP API, filesystem, or model gateway. Providers are created by the dependency-injection container and reused as singletons within that container.

Database persistence is already supplied by the built-in `ModelRepository`; AI does not need to generate a database Provider or raw SQL for normal SQLModel persistence.

```python
from ai_pod_cli import ModelRepository

shipment = repository.save(Shipment(tracking_number="PKG-001"))
same = repository.get(Shipment, shipment.id)
all_shipments = repository.list(Shipment)
pending = repository.find(Shipment, status="pending")
pending_again = repository.find(Shipment, {"status": "pending"})
repository.delete(shipment)
```

`ModelRepository` imports project Models and creates missing tables before access. Both keyword filters and a filter dictionary are supported because generated and hand-written code naturally use both forms.

### 3. Service: one business transformation

A Service reads entry data or upstream outputs from `PipelineContext`, uses injected dependencies, and returns named outputs.

```python
from injector import inject
from ai_pod_cli import ModelRepository
from ai_pod_cli.context import PipelineContext
from modules.models.shipment import Shipment


class CreateShipment:
    @inject
    def __init__(self, repository: ModelRepository):
        self.repository = repository

    def execute(self, ctx: PipelineContext) -> dict:
        shipment = self.repository.save(
            Shipment(tracking_number=ctx.get("tracking_number"))
        )
        return {"shipment": shipment.model_dump()}
```

A Service owns business decisions. It does not own a web server, queue listener, or process lifecycle. If its Contract declares a `dict`, it returns serializable data such as `model_dump()`; if components intentionally exchange a Model, the Contract references that registered Model explicitly.

### 4. Pipeline: composition is the program

A Pipeline is an ordered composition of Services:

```python
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import Pod, build_container
from ai_pod_cli.context import PipelineContext
from modules.services.create_shipment import CreateShipment
from modules.services.publish_shipment import PublishShipment


def run(ctx: PipelineContext) -> dict:
    container = build_container(load_beans())
    S = Pod(container)
    return (S(CreateShipment) | S(PublishShipment)).execute_all(ctx)
```

The `|` operator represents execution order and state flow:

```text
S0 ──CreateShipment──→ S1 ──PublishShipment──→ S2
```

Before saving a Pipeline, AIPod checks that required fields are available and adjacent output/input types are compatible. During execution, successful outputs are merged into `PipelineContext` for downstream Services.

### 5. Interface: expose a Pipeline without contaminating it

An Interface is a process boundary: a CLI command, web/API server, desktop window, worker, message consumer, or scheduled process. Interfaces invoke routes registered in `routes.toml`; they do not contain core business rules.

Redis and message-queue consumers therefore belong at the Interface boundary, while their clients belong in Providers and their business actions belong in Services.

## Why Generation Uses Five Stages

`aipod pod` does not ask the model to design the entire application in one enormous response. It runs a staged loop:

```text
1. Models      define and freeze the business vocabulary
2. Providers   add only explicitly required external capabilities
3. Services    implement operations against frozen dependencies
4. Pipelines   compose only Services that actually exist
5. Interfaces  expose only routes that were successfully saved
```

Each stage reloads the Bean Pool before planning the next stage. A stage may legally be empty—for example, an application using only `ModelRepository` needs no custom Provider.

This ordering gives repairs a clear owner:

| Failure | Repair target | Frozen layers preserved |
|---|---|---|
| Invalid field or SQLModel definition | Current Model | Earlier Models |
| Wrong external API wrapper | Current Provider | Models and earlier Providers |
| Missing input, wrong repository call, bad output | Current Service | Models, Providers, earlier Services |
| Incompatible execution order | Current Pipeline | All components |
| Broken startup or route exposure | Current Interface | Components and Pipelines |

The rule is deliberately conservative: **stable upstream code is evidence, not repair material**.

## Installation

Python 3.10 or newer is required.

```bash
pip install AIPodCli
pip install "AIPodCli[studio]"  # native Studio support
```

Development installation:

```bash
git clone https://github.com/wangzhongren/ai_pod_cli.git
cd ai_pod_cli
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -e ".[studio]"
```

## Configure the Model Once

AIPod uses an OpenAI-compatible Chat Completions endpoint. Configuration is stored globally and reused across projects:

```bash
aipod config set OPENAI_API_KEY sk-your-key
aipod config set OPENAI_BASE_URL https://api.openai.com/v1
aipod config set OPENAI_MODEL your-model-name
```

```bash
aipod config list
aipod config get OPENAI_MODEL
aipod config path
```

Local environment variables take priority over global configuration. Never commit API keys.

## Quick Start

```bash
mkdir logistics_control_tower
cd logistics_control_tower
aipod init

aipod pod "Build a multi-warehouse logistics control tower. Manage products, inventory, shipments and audit events; detect low stock and delayed shipments; expose CLI and web interfaces. Persist entities through SQLModel and do not use raw SQL." --yes
```

For a large specification:

```bash
aipod pod --file requirements.md --yes
```

Inspect and run the result:

```bash
aipod inspect project
aipod compose --list
aipod run create_shipment --params "{\"tracking_number\": \"PKG-001\"}"
aipod studio .
```

`aipod pod` reloads and reuses registered Models, Providers, Services, and routes. Restarting Studio does not recreate a project. Switching Studio to an ordinary folder requires **Initialize Project** once; switching back to an initialized project does not.

## AIPod Studio

AIPod Studio is a native desktop shell built with `pywebview`; Windows uses WebView2.

![AIPod Studio project graph](docs/assets/aipod-studio.png)

```bash
aipod studio .
aipod studio D:\work\my_project
```

Studio provides:

- a VS Code-inspired workspace;
- directory switching and one-click initialization;
- AI-first creation and manual component registration;
- non-blocking Pod jobs with live stages, character count, retries, validation, and generation progress;
- cooperative cancellation that preserves validated components;
- a graph showing Models, Providers, Services, Pipelines, and Interfaces;
- fixed graph controls, wheel zoom, canvas panning, and collapsible navigation;
- syntax-highlighted source tabs, hidden until explicitly opened;
- Pipeline composition by selecting Service nodes in execution order;
- entry discovery and execution for CLI, web, desktop, and worker programs;
- streamed process output and persisted execution traces.

Built-in runtime helpers are hidden from the application graph by default so the graph focuses on architecture owned by the project.

## Validation and the End-to-End Loop

### Static component checks

- expected class name and category;
- canonical imports for `Model`, `ConfigStore`, and `ModelRepository`;
- dependencies exist and use real Bean IDs;
- Models use `class Name(Model, table=True)` and define a primary key;
- Services implement `execute(ctx)`;
- fields read from context are declared as inputs;
- fields written to context are declared as outputs;
- raw SQL is rejected in generated Services.

### Disposable runtime checks

Before registration, AIPod copies the project into a temporary sandbox, installs the candidate only in that copy, builds the real DI container, imports frozen SQLModels, initializes a temporary database, creates deterministic sample rows, and executes the candidate with generated Contract inputs.

The sample generator understands scalar types, dates, datetimes, UUIDs, Decimals, Enums, registered Models, objects, lists, and Contract enum values such as `'IN' | 'OUT' | 'ADJUST'`.

Only after this run succeeds is the candidate written into the real project and registered in `beans_config.json`.

### Pipeline checks

- a Pipeline must expose `run(ctx)`;
- referenced Services must exist;
- required fields must be available at each step;
- adjacent Contracts must be compatible;
- the candidate executes in a disposable project copy before saving;
- `sys.exit()` is not allowed inside reusable Pipelines.

### Focused repair

Validation feedback is returned to the model. Runtime-only failures first attempt a constrained code patch. Structural or Contract failures regenerate only the current candidate. The stage stops if it cannot be repaired within its attempt limit; previously accepted components remain intact.

## Contracts Without a Second Data Model

Contracts describe names and types crossing a component boundary; they do not duplicate every SQLModel field.

```json
{
  "id": "CreateShipment",
  "category": "service",
  "dependencies": ["ModelRepository"],
  "inputs": {"tracking_number": "str"},
  "outputs": {"shipment": "dict"}
}
```

For shared business objects, reference the canonical Model:

```json
{
  "shipment": {
    "model": "modules.models.shipment.Shipment"
  }
}
```

Use a Model when identity and shared field semantics matter. Use scalar/dict/list Contracts for command parameters and presentation reports. This keeps boundaries strict without creating a second schema system.

## State, Results, Effects, and Policies

`PipelineContext` carries entry parameters, produced data, and execution steps:

```python
from ai_pod_cli.context import PipelineContext

ctx = PipelineContext({"tracking_number": "PKG-001"})
ctx.set("shipment_id", 1)
shipment_id = ctx.get("shipment_id")
```

Services can return a normal dictionary or an explicit result:

```python
from ai_pod_cli import Effect, Failure, Success


def execute(self, ctx):
    shipment_id = ctx.get("shipment_id")
    if shipment_id is None:
        return Failure("shipment_id is required", code="invalid_input")

    return Success(
        output={"published": True},
        effects=(Effect("message", "shipments", "publish"),),
    )
```

`Success.output` is merged into the context. `Failure` stops the remaining sequential Pipeline. Effects are recorded values today; enforcement and approval policies are roadmap work.

Retry and fallback are declared at composition time:

```python
flow = (
    S(FetchShipment)
    .retry(3, delay_seconds=0.2)
    .fallback(ReadCachedShipment)
    | S(RenderShipment)
)
```

Execution steps record status, attempts, duration, last error, and fallback.

## Redis, Queues, and External Systems

```text
Redis / Kafka / RabbitMQ client       Provider
Cache, publish, or consume decision   Service
Ordered business operation            Pipeline
Long-running listener process         Interface
Payload and persisted entity          Model
```

A RabbitMQ Provider owns connection and publishing primitives. A `PublishShipment` Service decides what event to publish. A worker Interface receives a message and invokes a route. Connection lifecycle stays out of reusable business logic.

## Project Structure

```text
logistics_control_tower/
├── modules/
│   ├── models/             # SQLModel business entities
│   ├── providers/          # Explicit external capabilities
│   └── services/           # Business transformations
├── pipelines/              # Executable compositions
├── beans_config.json       # Bean Pool, dependencies, and Contracts
├── routes.toml             # Route-to-Pipeline registry
├── config.toml             # Project and database configuration
├── requirements.txt        # Generated dependencies
├── main.py                 # Possible CLI Interface
├── server.py               # Possible Web Interface
└── .aipod/runs/            # Execution traces (gitignored)
```

The default project includes only `ConfigStore`, `ModelRepository`, and `PipelineRunner`. They are available to generated code but hidden from the Studio graph by default.

## Incremental and Manual Workflows

```bash
aipod create --category model --name Shipment --desc "A persisted shipment with tracking number and status"
aipod create --category service --name CreateShipment --desc "Validate and persist a Shipment through ModelRepository"

aipod add --category provider --name RedisStore \
  --class-path modules.providers.redis_store.RedisStore \
  --desc "Redis-backed cache and idempotency capability"

aipod compose "Create a shipment, allocate inventory, then publish its event" --name allocate_shipment
aipod run allocate_shipment --params "{\"tracking_number\": \"PKG-001\"}"
```

## CLI Reference

| Command | Purpose | Uses AI |
|---|---|:---:|
| `aipod init [--install-deps]` | Initialize the current directory | No |
| `aipod pod DESC [--file FILE] [--yes]` | Build or extend a Pod through five stages | Yes |
| `aipod create --category model/provider/service --name NAME --desc DESC` | Generate one component | Yes |
| `aipod add --category model/provider/service --name NAME --class-path PATH --desc DESC` | Register hand-written code | No |
| `aipod compose CMD [--name NAME]` | Generate and register a Pipeline | Yes |
| `aipod compose --list` | List registered Pipelines | No |
| `aipod entry DESC` | Generate an executable Interface | Yes |
| `aipod run ROUTE [--params JSON]` | Execute a route and persist its trace | No |
| `aipod inspect [TARGET] [NAME] [--json]` | Inspect project, components, routes, or runs | No |
| `aipod visualize [--output FILE] [--open]` | Export an interactive graph | No |
| `aipod studio [PATH] [--debug]` | Open native Studio | No |
| `aipod config set/get/remove/list/path` | Manage global model configuration | No |

Run `aipod COMMAND --help` for complete arguments.

## Current Boundaries

- Pipeline execution is sequential; parallel, async, stream, and event operators are not first-class yet.
- `Effect` values are recorded, but privileged-effect approval policies are not enforced yet.
- The sandbox is a disposable project copy and subprocess check, not an operating-system security boundary.
- Contract inference is lightweight and cannot prove arbitrary Python semantics.
- Generated dependencies and code should still be reviewed before production deployment.
- Output limits vary by model provider. AIPod retries truncated responses with a larger budget and can fall back from an incomplete stream to a complete non-stream response.

## Development

```bash
git clone https://github.com/wangzhongren/ai_pod_cli.git
cd ai_pod_cli
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[studio]"
$env:PYTHONUTF8 = "1"   # Windows PowerShell if the terminal defaults to GBK
python -m unittest discover -s tests
```

The package uses `setuptools`. Studio assets are bundled in the wheel and do not require a separate Node build.

## Security

- Store credentials in global configuration or environment variables; never commit them.
- Review generated code and dependencies before deployment.
- Treat custom Providers and Interfaces as privileged boundaries.
- Use least-privilege credentials and isolate untrusted projects.
- Do not treat the generation sandbox as a substitute for an OS container or VM when executing hostile code.

## Roadmap

- effect approval and denial policies;
- parallel, asynchronous, event, and streaming composition;
- timeout, rollback, and compensation operators;
- richer Contract diagnostics without duplicating canonical Models;
- stronger OS sandboxing for privileged Providers;
- reusable component packages and a governed capability registry.

## License

[MIT](LICENSE)
