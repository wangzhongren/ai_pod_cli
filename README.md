<p align="center">
  <img src="docs/assets/aipod-icon.png" alt="AIPod" width="128">
</p>

<h1 align="center">AIPod</h1>

<p align="center"><strong>A governed, compositional Python runtime for AI-built software.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/AIPodCli/"><img alt="PyPI" src="https://img.shields.io/pypi/v/AIPodCli"></a>
  <a href="https://pypi.org/project/AIPodCli/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/AIPodCli"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

AIPod gives AI coding agents a small, explicit programming model instead of asking them
to generate an entire application as an unstructured pile of files.

```text
Software = Data + Capabilities + Transformations + Composition + Delivery

Model     -> Provider     -> Service     -> Pipeline     -> Interface
data         capability      business       program         CLI / Web / Desktop / Worker
```

AI proposes components and composition. AIPod preserves their identities, dependencies,
Contracts, execution order, and project state. The result is ordinary Python code that
can be opened, tested, repaired, and deployed with normal tools.

> AIPod is alpha software. Review generated code before production use.

## Why AIPod

AI is usually good at producing one function. Larger generated applications fail at the
boundaries:

- one Service writes `shipment_count` while the next reads `shipments_count`;
- a Model is accidentally treated as an injected Provider;
- a Service calls a method its Provider never declared;
- a later repair rewrites an earlier working layer;
- a large one-shot response is truncated;
- generated modules exist, but no runnable Pipeline or entry point connects them.

AIPod addresses this with four rules:

1. **Build in dependency order.** Data is decided before capabilities, business logic,
   Pipelines, and Interfaces.
2. **Make boundaries machine-readable.** Bean IDs, import paths, dependencies, inputs,
   outputs, and routes live in project metadata.
3. **Freeze accepted decisions.** An unstable downstream component does not authorize a
   rewrite of stable upstream components.
4. **Verify with real evidence.** Generation uses deterministic structural checks. Real
   tests and entry commands are executed afterward by a developer or external coding
   agent through `aipod verify`.

## Installation

Python 3.10 or newer is required.

```bash
pip install AIPodCli
```

Install the native Studio as well:

```bash
pip install "AIPodCli[studio]"
```

Configure any OpenAI-compatible model endpoint:

```bash
aipod config set OPENAI_API_KEY sk-your-key
aipod config set OPENAI_BASE_URL https://api.openai.com/v1
aipod config set OPENAI_MODEL your-model
```

Configuration is stored globally, so `aipod` can be used from different project
directories. Environment variables and a local `.env` can override global values.

## Quick Start

Create a clean directory and initialize it:

```bash
mkdir expense_tracker
cd expense_tracker
aipod init
```

Put the requirement in `requirements.md`:

```markdown
# Expense Tracker CLI

Build an offline expense tracker with persistent Expense records, Services for adding,
listing, deleting, and summarizing expenses, Pipelines for each use case, and a CLI entry.
Use ModelRepository for persistence and do not write raw SQL.
```

Generate the project:

```bash
aipod pod --file requirements.md --yes
```

Inspect what was built:

```bash
aipod inspect --summary --json
aipod inspect project --json
aipod visualize --open
```

Run a registered route:

```bash
aipod run add_expense --params '{"description":"Lunch","amount":28.5}' --json
```

Verify a real entry or test command:

```bash
aipod verify --json -- python expense_cli.py --help
aipod verify --json -- python -m unittest
```

## The Five-Layer Model

### 1. Model — data

Models are the canonical Python representation of business data.

Runtime values do not create database tables:

```python
from ai_pod_cli import Model

class Vector2(Model):
    x: float
    y: float
```

Persistent entities opt into SQLModel tables:

```python
from sqlmodel import Field
from ai_pod_cli import Model

class Expense(Model, table=True):
    id: int | None = Field(default=None, primary_key=True)
    description: str
    amount: float
```

Models are imported as data types. They are never injected as dependencies.

### 2. Provider — capability

Providers connect the program to infrastructure: files, HTTP, Redis, queues, windows,
audio, or another external system. Only capabilities explicitly required by the project
should be created.

Persistent Models use the built-in `ModelRepository`; Services do not write raw SQL.

### 3. Service — transformation

A Service performs one focused business transformation:

```python
from injector import inject
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.repository import ModelRepository

class SaveExpenseService:
    @inject
    def __init__(self, repository: ModelRepository):
        self.repository = repository

    def execute(self, ctx: PipelineContext):
        expense = ctx.get("expense")
        saved = self.repository.save(expense)
        ctx.set("saved_expense", saved)
        return {"expense_id": saved.id}
```

Inputs and outputs are recorded as Contracts in `beans_config.json`.

### 4. Pipeline — composition

A Pipeline composes registered Services into a program:

```python
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import Pod, build_container
from ai_pod_cli.context import PipelineContext
from modules.services.validate_expense import ValidateExpenseService
from modules.services.save_expense import SaveExpenseService

def run(ctx: PipelineContext):
    S = Pod(build_container(load_beans()))
    (S(ValidateExpenseService) | S(SaveExpenseService)).execute_all(ctx)
    return ctx.summary()
```

The `|` operator expresses deterministic left-to-right composition.

### 5. Interface — delivery

Interfaces expose Pipeline routes as a CLI, website, desktop application, worker, or
message consumer. They depend on routes through `PipelineRunner` instead of duplicating
business logic.

## Five-Stage Generation

`aipod pod` builds one layer at a time:

```text
1. Models
2. Providers
3. Services
4. Pipelines
5. Interfaces
```

Each stage is saved in `aipod_plan.json`. If generation is interrupted, running the same
Pod request resumes the first incomplete stage and reuses its frozen plan. Components
already registered in the Bean Pool are reused instead of regenerated.

## Code Is Composable; Chain-of-Thought Is Not

Code has explicit inputs, outputs, types, dependencies, and observable behavior. It can
be composed and tested. Hidden chain-of-thought has none of those guarantees: one model's
private reasoning cannot be safely connected to another model's private reasoning as if
the two formed a deterministic program.

AIPod therefore does not attempt to concatenate reasoning transcripts. It composes
**structured conclusions**.

```text
Worker reasoning (private)     Worker reasoning (private)
           ↓                              ↓
     Decision Fragment              Decision Fragment
           └──────────────┬───────────────┘
                          ↓
                  Leader composition
                          ↓
               Deterministic Reducer
                          ↓
                  Generated program
```

### Worker: solve one bounded problem

A Worker may plan a Model, Provider, Service, Pipeline, or Interface using whatever
reasoning is appropriate. It does not hand its chain-of-thought to the next Worker.
Instead, it returns a small decision fragment:

```json
{
  "id": "AggregateMetricsService",
  "kind": "service",
  "dependencies": ["ConfigStore"],
  "models": ["ParsedLogEntry", "AggregateMetrics"],
  "requires": ["filtered_entries"],
  "provides": ["metrics"],
  "invariants": ["latency percentiles use valid numeric samples only"]
}
```

### Leader: compose meaning

The Leader reads the requirement, Canonical Plan, Bean Pool, and Worker fragments. Its job
is to combine semantic intent:

- decide which fragments belong to the same program;
- select or reject competing proposals;
- preserve decisions already frozen by earlier stages;
- identify missing capabilities or ambiguous boundaries;
- order the next bounded work without rewriting stable work.

The Leader is allowed to reason, but its output must again be structured: accepted
fragments, rejected fragments, unresolved questions, and the proposed dependency graph.
That output is inspectable and can be checked independently.

### Reducer: enforce facts

The Reducer is not another creative Agent. It deterministically checks duplicate
decisions, unknown dependencies, unknown Model references, frozen category conflicts,
and dependency cycles. It never invents a correction to make conflicting fragments fit.

Only reduced decisions proceed to code generation. Validation evidence is reduced to
either acceptance or repair of the current candidate; it does not expand repair scope to
earlier frozen layers.

This creates two different kinds of composition:

```text
Semantic composition      = Leader combines explicit decisions
Executable composition    = Pipeline combines validated code
```

Neither requires chain-of-thought to become project state. The durable project memory is
the Canonical Plan, decision fragments, Contracts, Bean Pool, source code, and execution
evidence.

In the current CLI, the staged `pod` planner performs the Leader role sequentially and
the deterministic reducer validates its fragments. Parallel external Workers are a
future extension of the same protocol, not a requirement for using AIPod today.

## Contracts

Contracts describe the fields crossing a component boundary:

```json
{
  "inputs": {
    "tracking_number": "str",
    "options": {"model": "modules.models.options.Options"}
  },
  "outputs": {
    "shipment": {"model": "modules.models.shipment.Shipment"}
  }
}
```

AIPod checks:

- expected class name and category;
- Service `execute(ctx)` and Pipeline `run(ctx)` entry points;
- blocked dynamic-code constructs;
- dependency IDs and Model-as-data rules;
- fields read from and written to `PipelineContext`;
- adjacent Pipeline field names, types, and structured schemas;
- raw SQL inside generated Services.

Generation intentionally does not execute every candidate with invented sample data.
Files, queues, UI events, and domain state cannot be represented reliably by a generic
fixture. Runtime repair starts from a real command instead.

## Real Verification and Agent Repair

Run structure checks only:

```bash
aipod verify --json
```

Run a real command without shell interpolation:

```bash
aipod verify --timeout 120 --json -- python app.py --smoke
```

The result includes:

- project structural status;
- exact command and exit code;
- bounded stdout and stderr;
- project-local traceback files and line numbers;
- suggested repair files;
- redaction of common API key and Bearer token formats.

AIPod does not embed Codex, Claude Code, Pi, or another coding agent. [`SKILL.md`](SKILL.md)
is the portable handoff protocol. An external agent reads `inspect`, runs `verify`, makes
the smallest evidence-backed repair, and repeats the same command.

```text
AIPod generates and describes the system
                ↓
aipod verify returns real execution evidence
                ↓
Codex / Claude Code / another agent edits ordinary Python
                ↓
the same verification command runs again
```

Install or copy this repository as an `aipod-development` skill in the skill directory
used by your coding agent. Codex-specific display metadata is included in
`agents/openai.yaml`.

## Native Studio

```bash
aipod studio .
```

<p align="center">
  <img src="docs/assets/aipod-studio.png" alt="AIPod Studio" width="920">
</p>

Studio provides:

- a VS Code-inspired native workspace using `pywebview + WebView2`;
- project directory switching and initialization;
- AI-first and manual component creation;
- non-blocking generation progress and cancellation;
- a graph of Models, Providers, Services, Pipelines, and Interfaces;
- dependency and execution-route edges;
- zooming, panning, fixed canvas controls, and collapsible navigation;
- syntax-highlighted source tabs;
- Pipeline composition and entry execution;
- streamed program output and run traces.

Built-in runtime Providers are hidden from the graph by default so the view focuses on
project-owned architecture.

## Runtime Results and Policies

Services may return dictionaries or explicit results:

```python
from ai_pod_cli import Effect, Failure, Success

return Success(
    output={"shipment_id": 42},
    effects=(Effect("database.write", {"model": "Shipment"}),),
)

return Failure("inventory unavailable", code="inventory_unavailable", retryable=True)
```

Sequential execution supports retry, timeout, and fallback policies through component
metadata. Execution steps and Effects are recorded in Pipeline traces.

## Project Files

```text
project/
├── aipod_plan.json          resumable five-stage decisions
├── beans_config.json        Bean Pool and Contracts
├── config.toml              application configuration
├── routes.toml              route-to-Pipeline mapping
├── requirements.txt         generated Python dependencies
├── modules/
│   ├── models/
│   ├── providers/
│   └── services/
├── pipelines/
├── app.py / cli.py          generated Interface
└── .aipod/runs/             redacted execution traces
```

## CLI Reference

| Command | Purpose | Uses AI |
|---|---|:---:|
| `aipod init [--install-deps]` | Initialize the current directory | No |
| `aipod pod DESC [--file FILE] [--yes] [--json]` | Build or resume all five stages | Yes |
| `aipod create --category model/provider/service --name NAME --desc DESC` | Generate one component | Yes |
| `aipod add --category model/provider/service --name NAME --class-path PATH --desc DESC` | Register hand-written code | No |
| `aipod compose CMD [--name NAME] [--json]` | Generate and register a Pipeline | Yes |
| `aipod entry DESC` | Generate an Interface | Yes |
| `aipod run ROUTE [--params JSON] [--json]` | Execute a route and persist its trace | No |
| `aipod inspect [TARGET] [NAME] [--summary] [--json]` | Read stable project state | No |
| `aipod verify [--timeout N] [--json] -- COMMAND...` | Produce real repair evidence | No |
| `aipod visualize [--output FILE] [--open]` | Export the project graph | No |
| `aipod studio [PATH] [--debug]` | Open native Studio | No |
| `aipod config set/get/remove/list/path` | Manage global model configuration | No |

## Development

```bash
git clone https://github.com/wangzhongren/ai_pod_cli.git
cd ai_pod_cli
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[studio]"
python -m unittest discover -s tests
```

On Windows terminals using a non-UTF-8 code page:

```powershell
$env:PYTHONUTF8 = "1"
```

## Current Boundaries

- Pipeline composition is currently sequential.
- Contract analysis cannot prove arbitrary Python semantics.
- Privileged Effect approval and denial policies are not yet enforced.
- Generated code is not a substitute for review, real tests, or deployment isolation.
- External model providers may impose output and reasoning-token limits.

## Roadmap

- parallel, asynchronous, event, and streaming composition;
- privileged Provider and Effect approval policies;
- rollback and compensation operators;
- richer Contract diagnostics without duplicating canonical Models;
- reusable component packages and a governed capability registry;
- deeper Studio integration with Agent-neutral verification reports.

## License

[MIT](LICENSE)
