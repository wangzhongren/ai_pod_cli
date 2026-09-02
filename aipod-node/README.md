# AIPod Node

Node.js/TypeScript implementation of AIPod's governed compositional Runtime.

This subproject starts from the architectural boundary established by the Python runtime:

```text
Model → Provider → Service → Pipeline → Interface
```

A Service can see its Contract, Models, and Providers. It cannot import, inject, resolve,
or execute another Service. Service composition belongs exclusively to Pipelines.

## Status

Initial runtime foundation:

- typed `PipelineContext` with branch snapshots and deterministic merging;
- structured `Success`, `Failure`, and `Effect` results;
- runtime input/output Contract validation;
- singleton dependency container with Service isolation;
- sequential Service composition;
- isolated parallel branches;
- governed `repeat` loops with bounded iteration traces;
- bounded async streams and batching;
- named route runner;
- resumable Model → Provider → Service → Pipeline → Interface construction Agent;
- OpenAI-compatible JSON model client using native `fetch`;
- stage-specific capability visibility;
- per-artifact generation with up to three validation-guided attempts;
- bounded exact-text source repair with public-export protection;
- TypeScript validation, staging, atomic commit, and final project verification;
- public Agent history and validation evidence in `.aipod/plan.json`;
- machine-readable Project Model with structural issues;
- dynamic TypeScript compilation and generated Bean loading;
- built-in JSON `ConfigStore`;
- built-in atomic JSON `ModelRepository`;
- real named Route execution with redacted persisted Trace;
- optional real verification commands with timeout and bounded output;
- basic Interface loading, execution, and smoke;
- multi-file Interface Artifacts with permissions and lifecycle metadata;
- install, uninstall, and required Interface verification commands;
- authenticated local Web Studio with Project Model, source, Route, Interface, and Pod APIs;
- cooperative Pod cancellation before atomic stage commit;
- static Pipeline Contract flow analysis with type errors and semantic warnings;
- full-project TypeScript semantic checking through `ts.createProgram`;
- persistent HTTP Stream Broker with consumer groups;
- message deduplication keys, leases, heartbeats, retry, and dead letters;
- concurrent distributed Workers that execute governed Routes;
- automatic earliest-affected-stage selection for existing project changes;
- real command verification with timeout, bounded output, and redaction;
- `init`, `inspect`, `pod`, `create`, `add`, `compose`, `run`, `verify`, `interface`,
  and `studio` CLI commands;
- Node built-in test suite.

Visual drag-and-drop Pipeline composition, highly available Broker replication,
SQL-backed repositories, and the full Python feature set have not been ported yet.

## Development

```bash
cd aipod-node
npm install
npm test
```

Node.js 20 or newer is required.

Run the package CLI during repository development without addressing build internals:

```bash
npm run build
npm run cli -- help
```

## Installation and CLI

Install AIPod Node as a project-local development tool:

```bash
npm install --save-dev aipod-node
npx aipod-node help
```

`npx` resolves the local `node_modules/.bin/aipod-node` executable created from the
package's `bin` declaration. A global installation is optional, not required:

```bash
npm install --global aipod-node
aipod-node help
```

## AI/Codex skill

[`SKILL.md`](SKILL.md) contains the agent-facing workflow for discovering, building,
running, repairing, verifying, and operating AIPod Node projects. Install or reference
the `aipod-node/` subdirectory as the `aipod-node` skill when using this repository from
Codex or another SKILL.md-compatible coding agent.

## Shared configuration with Python

The Python and Node.js implementations read the same global file:

```text
~/.aipod/config.toml
```

```toml
[env]
OPENAI_API_KEY = "..."
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_MODEL = "your-model"
OPENAI_TIMEOUT_SECONDS = "120"
```

Configuration priority is:

```text
process environment → project .env → ~/.aipod/config.toml [env]
```

Node `ConfigStore` also reads the same project `config.toml` as Python and uses the same
dot notation, such as `config.get("database.url")`. Existing Node-only `config.json` files
remain supported as a fallback.

Both CLIs can manage the shared global configuration:

```bash
aipod config set OPENAI_MODEL deepseek-chat
npx aipod-node config get OPENAI_MODEL
```

## Example

```ts
import {
  Container,
  PipelineContext,
  repeat,
  service,
} from "aipod-node";

const container = new Container([
  {
    id: "ReadInput",
    category: "service",
    factory: () => ({
      execute: () => ({ quitRequested: false }),
    }),
  },
  {
    id: "RenderFrame",
    category: "service",
    factory: () => ({
      execute: () => ({ rendered: true }),
    }),
  },
]);

const frame = service(container, "ReadInput")
  .pipe(service(container, "RenderFrame"));

const context = new PipelineContext({ maxFrames: 60 });
await repeat(frame, {
  untilField: "quitRequested",
  maxIterationsField: "maxFrames",
  outputField: "executedFrames",
}).execute(context);
```

## CLI

After installing:

```bash
npx aipod-node init ./demo
npx aipod-node inspect ./demo
```

Configure any OpenAI-compatible endpoint and run the construction Agent:

```bash
export OPENAI_API_KEY="..."
export OPENAI_MODEL="your-model"
export OPENAI_BASE_URL="https://api.openai.com/v1" # optional

npx aipod-node pod \
  "Build a typed greeting service, route, and CLI Interface" \
  --project-root ./demo
```

The Agent executes stages deterministically. The model decides the bounded content of the
current stage, but never chooses the next tool or sees capabilities hidden by that stage.
Completed stages resume without another model call.

Modify an existing project while freezing unaffected upstream stages:

```bash
npx aipod-node pod \
  "Change the CLI output to JSON" \
  --stage auto \
  --project-root ./demo
```

Inspect, verify, and run generated artifacts:

```bash
npx aipod-node inspect ./demo
npx aipod-node verify ./demo
npx aipod-node verify ./demo -- node --test
npx aipod-node run greet \
  --params '{"name":"Ada"}' \
  --project-root ./demo

npx aipod-node interface list --project-root ./demo
npx aipod-node interface smoke GreetingCli --project-root ./demo
npx aipod-node interface verify GreetingCli --project-root ./demo
npx aipod-node interface install GreetingCli --project-root ./demo
npx aipod-node interface run GreetingCli \
  --payload '{"name":"Ada"}' \
  --project-root ./demo
```

Focused construction commands are also available:

```bash
npx aipod-node create \
  --category service \
  --description "Format a greeting" \
  --project-root ./demo

npx aipod-node compose \
  "Validate then format a greeting" \
  --project-root ./demo
```

Open the local Studio:

```bash
npx aipod-node studio ./demo
```

Studio binds to `127.0.0.1` on a random port and uses a per-process access token. It can
inspect validation issues and Agent state, read project-local source, execute Routes,
run/smoke Interfaces, and start the background Pod Agent.

## Distributed Stream and Worker

Start one persistent Broker on a reachable host:

```bash
export AIPOD_BROKER_TOKEN="replace-with-a-secret"

npx aipod-node broker \
  --host 0.0.0.0 \
  --port 8787 \
  --project-root ./demo
```

Publish idempotently from any machine:

```bash
npx aipod-node publish \
  --broker http://broker-host:8787 \
  --token "$AIPOD_BROKER_TOKEN" \
  --stream orders \
  --key order-1001 \
  --payload '{"orderId":"order-1001"}'
```

Run one or more Workers on other machines:

```bash
npx aipod-node worker \
  --broker http://broker-host:8787 \
  --token "$AIPOD_BROKER_TOKEN" \
  --stream orders \
  --group order-processors \
  --consumer worker-a \
  --route processOrder \
  --concurrency 8 \
  --project-root ./demo
```

Inspect Broker and dead-letter state, then replay a repaired message:

```bash
npx aipod-node broker-stats --broker http://broker-host:8787 \
  --token "$AIPOD_BROKER_TOKEN"

npx aipod-node dead-letters --broker http://broker-host:8787 \
  --token "$AIPOD_BROKER_TOKEN" --stream orders --group order-processors

npx aipod-node requeue --broker http://broker-host:8787 \
  --token "$AIPOD_BROKER_TOKEN" --message-id MESSAGE_ID \
  --stream orders --group order-processors
```

Delivery is **at least once**. The Broker persists messages and group delivery state,
recovers expired leases, and moves exhausted messages to its dead-letter state. Services
that perform external side effects must use the message ID or publisher key as an
idempotency key. The current Broker is a single durable coordinator, not an HA replicated
cluster.

## Semantic Type Checking

Generated projects are checked as one TypeScript Program rather than as isolated files.
The checker resolves relative `.js` imports back to TypeScript sources and maps
`aipod-node` to the installed Runtime declarations. It reports cross-file missing exports,
assignment incompatibilities, Runtime API misuse, exact file/line/column, and diagnostic
codes. Agent verification, `inspect`, `verify`, and real Route loading all use the same
semantic check.
