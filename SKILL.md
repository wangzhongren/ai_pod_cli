---
name: aipod-development
description: Build, inspect, compose, run, and repair Python applications managed by AIPod. Use when a project contains beans_config.json, routes.toml, or aipod_plan.json, or when the user asks to use ai-pod-cli or aipod commands.
---

# AIPod Development

Use AIPod as the architecture and execution protocol. Use your normal code-editing
tools for implementation repair. Do not embed or invoke another coding agent.

## Discover the project

When `beans_config.json` exists, read machine state before making architectural
decisions:

```bash
aipod inspect --summary --json
aipod inspect project --json
```

Use exact Bean IDs, `class_path` values, Contract fields, and route names returned by
AIPod. Do not infer them from filenames. If the directory is not initialized and the
user asked to build an AIPod project, run `aipod init` first.

## Build

For a complete requirement, prefer a requirements file so the objective remains stable:

```bash
aipod pod --file requirements.md --yes --json
```

For focused changes:

```bash
aipod create --category model --name NAME --desc "DESCRIPTION" --json
aipod create --category provider --name NAME --desc "DESCRIPTION" --json
aipod create --category service --name NAME --desc "DESCRIPTION" --json
aipod compose "INSTRUCTION" --name ROUTE --json
```

Let `pod` resume `aipod_plan.json`; do not delete or recreate a frozen plan merely to
retry an incomplete stage.

## Respect the five layers

- Models define runtime or persistent data. Import Models; never inject them.
- Providers expose infrastructure capabilities and may be injected.
- Services implement focused transformations through `execute(ctx)`.
- Pipelines compose registered Services.
- Interfaces expose registered Pipeline routes.

Preserve validated upstream layers when a downstream layer fails. Do not rename Contract
fields or Bean IDs without concrete evidence that the frozen definition is wrong.

## Verify with real evidence

Generation performs deterministic structural and Contract checks. Validate behavior with
the project's real test, smoke, or entry command:

```bash
aipod verify --json -- python -m unittest
aipod verify --json -- python app.py --smoke
```

Run commands as argument arrays after `--`; do not wrap them in a shell string. If no real
command is known, run `aipod verify --json` for structural checks and inspect discovered
routes or entry files before choosing one.

## Repair loop

When verification fails:

1. Read `checks.structure`, `checks.execution`, and `repair.suggested_files`.
2. Identify the smallest component supported by the traceback and project model.
3. Modify that component and directly related tests only.
4. Do not modify credentials, publish packages, commit, or push unless explicitly asked.
5. Repeat the same `aipod verify` command.
6. Stop when it passes, when the same external blocker repeats, or when repair requires
   changing a frozen architectural decision. Ask the user before expanding scope.

Use `aipod inspect run RUN_ID --json` when a registered route has already produced a trace.
Treat generated code as ordinary Python and review it before production use.
