---
name: aipod-node
description: Build, inspect, run, verify, repair, and operate governed Node.js/TypeScript applications managed by AIPod Node. Use when a project contains aipod.json or .aipod/plan.json, or when the user asks for aipod-node, its Agent, Studio, Interfaces, Routes, Broker, Streams, or Workers.
metadata:
  short-description: Develop governed AIPod Node applications
---

# AIPod Node Development

Use AIPod Node as the architecture, construction, and execution protocol. Use normal
editing tools for bounded implementation fixes. Do not embed or invoke another coding
agent inside an AIPod Service or generated project.

## Choose the executable

Prefer an installed `aipod-node` command. Inside this repository, build and use the local
CLI when the command is not installed:

```bash
cd aipod-node
npm install
npm run build
node dist/src/cli.js help
```

Do not reinstall dependencies when `node_modules` already satisfies the lockfile. Node.js
20 or newer is required.

## Discover machine state first

When `aipod.json` exists, inspect it through the stable Project Model before making
architectural decisions:

```bash
aipod-node inspect .
aipod-node verify .
```

When using the repository CLI, replace `aipod-node` with
`node /path/to/aipod-node/dist/src/cli.js`.

Use exact Bean IDs, Contract fields, Route names, Interface names, execution modes,
permissions, lifecycle commands, and verification declarations returned by `inspect`.
Do not infer IDs from filenames. If the directory is not initialized and the user asked
for an AIPod Node project, run `aipod-node init <directory>` first.

## Shared Python and Node configuration

Python AIPod and AIPod Node intentionally share:

```text
~/.aipod/config.toml          global [env] model configuration
<project>/config.toml         project runtime configuration
```

Configuration priority is process environment, project `.env`, then global `[env]`.
Use only masked inspection:

```bash
aipod-node config list
aipod-node config path
```

Never print, request, replace, or copy an existing API key. Components read project
configuration through injected `ConfigStore` and dot notation; they do not read model
credentials. The Agent may see non-sensitive project configuration, but secret, token,
password, key, and authorization values are redacted before model calls.

## Build or modify a Pod

For a complete application requirement:

```bash
aipod-node pod "REQUIREMENT" --project-root .
aipod-node pod --file requirements.md --project-root .
```

For a change to an existing application, preserve frozen upstream stages:

```bash
aipod-node pod "CHANGE" --stage auto --project-root .
```

Use an explicit `models|providers|services|pipelines|interfaces` stage only when the user
asks to override automatic impact classification.

The local scheduler always controls the five-stage order. The model decides only the
bounded content of the current stage. Let `.aipod/plan.json` resume completed stages; do
not delete it to retry a failure. Read its public evidence when a stage stops.

An explicit user request to build or modify with AIPod Node authorizes the workflow's
intrinsic transfer of the requirement and minimum visible stage context to the configured
OpenAI-compatible endpoint. State that boundary before the first model-backed command,
but do not request separate confirmation for each stage. Platform network approval may
still be required. This does not authorize publication, deployment, installation,
unrelated external services, or destructive changes.

Focused operations are available when a full Pod rebuild is unnecessary:

```bash
aipod-node create --category model|provider|service --description "..." --project-root .
aipod-node add --id ID --category TYPE --file src/path.ts --project-root .
aipod-node compose "PIPELINE INSTRUCTION" --project-root .
```

## Preserve the five-layer boundary

```text
Model → Provider → Service → Pipeline → Interface
```

- Models are typed data and never enter dependency injection.
- Providers expose infrastructure and may be injected into Services.
- A Service sees only its Contract, Models, Providers, and `PipelineContext`.
- A Service cannot import, inject, resolve, construct, or execute another Service or
  `PipelineRunner`.
- Pipelines are the only place to compose Services. Keep sequence, parallelism, retries,
  repetition, and streaming visible there.
- Interfaces see frozen Routes, not Services. Inspect their Artifacts, permissions,
  lifecycle, and every required verification command.

Do not hide orchestration in a coordinating Service. Use `sequence`, `parallel`,
`repeat`, or `stream` in the Pipeline Runtime. `repeat` conditions are named Context
fields, not arbitrary generated callbacks.

## Run and verify with real evidence

Run a registered Route and retain its redacted Trace:

```bash
aipod-node run ROUTE --params '{"field":"value"}' --project-root .
```

Inspect and exercise the delivery boundary:

```bash
aipod-node interface list --project-root .
aipod-node interface smoke NAME --project-root .
aipod-node interface verify NAME --project-root .
aipod-node interface run NAME --payload '{}' --project-root .
```

Run real verification commands as argument arrays after `--`, never as a shell string:

```bash
aipod-node verify . -- node --test
aipod-node verify . -- npm test
```

A structure-only result is `unverified`, not passed. Full-project verification uses one
TypeScript Program and must catch cross-file imports, missing exports, type
incompatibilities, and Runtime API misuse. Repeat the same command after a repair.

When verification fails, keep repair local:

1. Read Project Model issues, TypeScript file/line/code, Trace, and Agent evidence.
2. Select the smallest evidence-backed file.
3. Preserve public exports and Service visibility.
4. Apply bounded exact replacements or a focused edit.
5. Repeat the identical verification command.
6. Stop when repair requires changing a frozen architectural decision or an external
   blocker repeats.

Do not commit, push, publish, install lifecycle files, or broaden repair scope unless the
user explicitly requests it.

## Studio

Open the local authenticated Studio when visual inspection or background Agent progress
is useful:

```bash
aipod-node studio .
```

Studio binds to loopback by default and uses a per-process token. Treat a request to bind
it to a non-loopback host as network exposure requiring explicit user authorization and
appropriate access controls.

## Distributed Streams and Workers

Use the distributed runtime only when the task actually requires multiple processes or
machines. Read the distributed section of [README.md](README.md) before configuring a
Broker or Worker.

The delivery contract is **at least once**, not exactly once. Services performing
external side effects must use `messageId` or publisher key for idempotency. The built-in
Broker is one persistent coordinator, not an HA replicated cluster.

Typical operations:

```bash
aipod-node broker --host 127.0.0.1 --port 8787 --project-root .
aipod-node publish --broker URL --token TOKEN --stream NAME --key KEY --payload '{}'
aipod-node worker --broker URL --token TOKEN --stream NAME --group GROUP \
  --route ROUTE --project-root .
aipod-node broker-stats --broker URL --token TOKEN
aipod-node dead-letters --broker URL --token TOKEN --stream NAME --group GROUP
aipod-node requeue --broker URL --token TOKEN --message-id ID --stream NAME --group GROUP
```

Do not expose a Broker, install a system service, or requeue dead letters without the
user's authorization. Inspect dead-letter error and attempt evidence before replay.

## Develop the AIPod Node package

When changing `aipod-node` itself, run:

```bash
npm test
npm run check
npm pack --dry-run
```

Loopback HTTP tests may be skipped by a restricted sandbox; rerun the specific Node test
with scoped loopback permission rather than weakening it. Ensure `node_modules`, `dist`,
`.aipod`, Broker data, Traces, and temporary build output are not committed or packed.

Read [README.md](README.md) when a task needs the complete CLI reference, distributed
operations, or current feature limitations. Read
[examples/distributed-orders/README.md](examples/distributed-orders/README.md) only for a
distributed Worker example.
