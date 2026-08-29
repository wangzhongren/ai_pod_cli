<p align="center">
  <img src="docs/assets/aipod-icon.png" alt="AIPod icon" width="132">
</p>

<h1 align="center">AIPod</h1>

<p align="center"><strong>A compositional computation model and governed runtime for AI-built Python software.</strong></p>

AIPod lets AI create software without giving the model control of the runtime. Humans define the architecture and governance rules; AI discovers, creates, selects, and composes components inside those boundaries; AIPod validates and executes the resulting program deterministically.

```text
Component: (State, Input, Dependencies) -> Result(State', Output, Effects)
Pipeline:  Cn ∘ ... ∘ C2 ∘ C1

Software = Components + Dependencies + Composition + State + Effects
AIPod   = Software Model + Governance
```

> AIPod is currently alpha software. Components, dependency injection, sequential composition, contracts, validation, execution traces, CLI workflows, and the desktop Studio are implemented. First-class effects, parallel pipelines, retries, and compensation are on the roadmap.

## Why AIPod?

Most AI coding tools generate files. AIPod generates a system that remains inspectable and operable after generation:

- **Components** provide reusable capabilities and business transformations.
- **Contracts** describe required inputs, produced outputs, and dependencies.
- **Pipelines** compose compatible components into executable programs.
- **Interfaces** expose programs through CLI, web, desktop, API, workers, or schedulers.
- **PipelineContext** carries input and transient state through a run.
- **Dependency injection** provides deterministic runtime assembly and singleton reuse.
- **Validation and traces** make generated systems reviewable and debuggable.

The result is ordinary Python. AIPod does not hide the source or require a proprietary deployment target.

## The Four-Layer Model

```text
Capabilities            Business logic          Composition             Delivery
Providers       --->    Services        --->    Pipelines       --->    Interfaces
database, HTTP          use-case logic           execution order         CLI, web, desktop,
Redis, queues           and transformation       and state flow           API, worker, schedule
```

### Provider

A Provider wraps infrastructure or an external capability: a database, filesystem, HTTP client, Redis client, message producer, or model gateway. It is injected into Services and reused by the container.

### Service

A Service implements one business transformation. It reads inputs from `PipelineContext`, uses injected Providers, and returns a dictionary of outputs. Returned values are merged into the context for downstream components.

### Pipeline

A Pipeline is an ordered composition of Services. Before a visual pipeline is saved, AIPod checks whether each component's output contract can satisfy the next component's input contract.

### Interface

An Interface is an executable boundary around one or more Pipelines. A web server, desktop window, CLI command, API route, message consumer, or scheduled job belongs here—not in the business Pipeline itself.

## Quick Start

Python 3.10 or newer is required.

```bash
git clone https://github.com/wangzhongren/ai_pod_cli.git
cd ai_pod_cli

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -e ".[studio]"
```

Configure an OpenAI-compatible model once. The global configuration is reused by every project:

```bash
aipod config set OPENAI_API_KEY sk-your-key
aipod config set OPENAI_BASE_URL https://api.openai.com/v1
aipod config set OPENAI_MODEL your-model-name
```

Create and build a project:

```bash
mkdir expense_app
cd expense_app
aipod init
aipod pod "Build an expense tracker with add, list, summary, and delete operations" --yes
```

Then inspect or run it:

```bash
aipod inspect project
aipod run add_expense --params "{\"amount\": 42.5, \"category\": \"food\"}"
aipod studio .
```

`aipod pod` reuses compatible components and routes already registered in the project. Restarting Studio or describing another feature does not automatically recreate the entire Pod.

## AIPod Studio

AIPod Studio is a native desktop shell built with `pywebview` and WebView2 on Windows. It provides a VS Code-inspired workspace while preserving the Python runtime underneath.

![AIPod Studio project graph](docs/assets/aipod-studio.png)

```bash
pip install -e ".[studio]"
aipod studio .
```

From Studio you can:

- open or switch project directories and initialize an ordinary folder;
- describe an application and let AI build components, pipelines, and an entry point;
- manually register a complete Provider or Service;
- inspect the graph from Providers to Services, Pipelines, and Interfaces;
- compose a Pipeline by selecting Service nodes in execution order;
- inspect syntax-highlighted source in an editor tab;
- start discovered CLI, web, desktop, API, or Python entry points;
- stream process output in the integrated terminal;
- inspect validation results and recent execution traces.

The graph supports wheel zoom, canvas panning, fixed controls, collapsible navigation sections, and selectable nodes. Built-in runtime helpers are hidden by default so the graph focuses on application architecture.

## Build Workflows

### Describe the Whole Pod

```bash
aipod pod "Collect articles, remove duplicates, summarize them, and expose a CLI report" --yes
aipod pod --file requirements.md --yes
```

The command plans the architecture, reuses compatible existing beans, creates missing Providers and Services, composes Pipelines, registers routes, and generates an entry point when appropriate.

### Build Incrementally

Generate components:

```bash
aipod create --category provider --name ArticleRepository --desc "Persist and query articles in SQLite"
aipod create --category service --name SummarizeArticles --desc "Produce a concise report from collected articles"
```

Register hand-written code:

```bash
aipod add --category provider --name RedisStore \
  --class-path modules.providers.redis_store.RedisStore \
  --desc "Redis-backed cache and idempotency store"
```

Compose and run a route:

```bash
aipod compose "Collect articles, deduplicate them, then summarize the result" --name daily_report
aipod run daily_report --params "{\"topic\": \"AI\"}"
```

## Runtime Model

Every run starts with a `PipelineContext`:

```python
from ai_pod_cli.context import PipelineContext

ctx = PipelineContext(params={"topic": "AI"})
ctx.set("articles", [{"title": "Example"}])
articles = ctx.get("articles", [])
```

A Service performs one transformation:

```python
from ai_pod_cli.context import PipelineContext


class SummarizeArticles:
    def execute(self, ctx: PipelineContext) -> dict:
        articles = ctx.get("articles", [])
        summary = "\n".join(item["title"] for item in articles)
        return {"summary": summary}
```

A Pipeline resolves components from the Pod and composes them with `|`:

```python
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import Pod, build_container
from ai_pod_cli.context import PipelineContext
from modules.services.collect_articles import CollectArticles
from modules.services.summarize_articles import SummarizeArticles


def run(ctx: PipelineContext) -> dict:
    container = build_container(load_beans())
    S = Pod(container)
    return (S(CollectArticles) | S(SummarizeArticles)).execute_all(ctx)
```

Each component is resolved as a singleton within its container. Each dictionary result is merged into the context, and every step records its component, result preview, and duration.

### Structured Results

Components may keep returning dictionaries, or use explicit computation results:

```python
from ai_pod_cli import Effect, Failure, Success


def execute(self, ctx):
    if not ctx.params.get("invoice_id"):
        return Failure(
            "invoice_id is required",
            code="invalid_input",
            retryable=False,
        )

    return Success(
        output={"published": True},
        effects=(
            Effect("message", "invoices", "publish"),
        ),
    )
```

`Success.output` is merged into `PipelineContext`. An explicit `Failure` stops the remaining sequential Pipeline and produces a failed execution trace. Legacy dictionary returns remain fully compatible.

### Execution Policies

Retry and fallback policies are declared where a component is composed:

```python
flow = (
    S(FetchRemoteInvoice)
    .retry(3, delay_seconds=0.2)
    .fallback(ReadCachedInvoice)
    | S(RenderInvoice)
)
result = flow.execute_all(ctx)
```

Policies are visible in execution steps through `attempts`, `status`, `last_error`, and `fallback`. Exceptions still propagate normally when no retry or fallback policy handles them.

## Contracts and Governance

`beans_config.json` is the machine-readable component pool. A component entry records identity, import path, category, description, dependencies, and inferred input/output contracts.

```json
{
  "id": "SummarizeArticles",
  "class_path": "modules.services.summarize_articles.SummarizeArticles",
  "category": "service",
  "dependencies": ["ArticleRepository"],
  "inputs": {"articles": "list"},
  "outputs": {"summary": "str"}
}
```

Current validation covers importability and registration, the required Service boundary, route and Pipeline structure, adjacent contract compatibility, context-based argument flow, rejection of `sys.exit()` inside Pipelines, and project-level diagnostics in both CLI and Studio.

Contracts currently use inferred lightweight type names. Rich schemas and stricter static composition are planned.

## Redis and Message Queues

Redis, Kafka, RabbitMQ, and similar systems fit the same model:

```text
Redis client / Queue producer  -> Provider
Cache or publish use case      -> Service
Ordered business flow          -> Pipeline
Message consumer process       -> Interface
```

For example, a `RabbitMQProvider` owns connections and publish/consume primitives, a `PublishInvoice` Service expresses the business operation, and a worker entry point receives messages and invokes an AIPod route. Long-running listeners remain Interfaces so infrastructure lifecycle does not leak into reusable business components.

## Project Structure

```text
expense_app/
├── modules/
│   ├── providers/          # Infrastructure capabilities
│   └── services/           # Business transformations
├── pipelines/              # Composed execution routes
├── beans_config.json       # Component pool and contracts
├── routes.toml             # Route-to-pipeline registry
├── config.toml             # Project configuration
├── requirements.txt        # Generated project dependencies
├── server.py               # Example discovered interface
└── .aipod/
    └── runs/               # Runtime traces (gitignored)
```

The global model configuration lives at `~/.aipod/config.toml`. Project configuration and generated code remain local to each project.

## CLI Reference

| Command | Purpose | Uses AI |
|---|---|:---:|
| `aipod init [--install-deps]` | Initialize the current directory | No |
| `aipod pod DESC [--file FILE] [--yes]` | Build or extend a complete Pod | Yes |
| `aipod create --category ... --name ... --desc ...` | Generate one Provider or Service | Yes |
| `aipod add --category ... --name ... --class-path ... --desc ...` | Register hand-written code | No |
| `aipod compose CMD [--name NAME]` | Generate and register a Pipeline | Yes |
| `aipod entry DESC` | Generate an executable project entry point | Yes |
| `aipod run ROUTE [--params JSON]` | Execute a route and save its trace | No |
| `aipod inspect [TARGET] [NAME] [--json]` | Inspect project/run metadata | No |
| `aipod visualize [--output FILE] [--open]` | Export an interactive graph | No |
| `aipod studio [PATH] [--debug]` | Open the native desktop Studio | No |
| `aipod config set/get/remove/list/path` | Manage global model configuration | No |

Run `aipod COMMAND --help` for complete arguments.

## Development

```bash
git clone https://github.com/wangzhongren/ai_pod_cli.git
cd ai_pod_cli
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -e ".[studio]"
python -m unittest discover -s tests -q
```

The package uses `setuptools`; the desktop frontend is bundled as a package asset and does not require a separate Node build.

## Security

- Keep API keys in global config or environment variables; never commit them.
- Review AI-generated code and dependencies before executing an unfamiliar project.
- Treat Providers as privileged boundaries because they can access files, networks, databases, and external services.
- Use least-privilege credentials and isolate untrusted generated projects.

## Roadmap

- Effect approval and denial policies
- Parallel, asynchronous, event, and streaming composition
- Timeout, rollback, and compensation operators
- Rich schema contracts and stronger static Pipeline checking
- Sandboxed execution and approval policies for privileged Providers
- Reusable component packages and a governed capability registry

## License

[MIT](LICENSE)
