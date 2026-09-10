---
name: aipod-migrate
description: Migrate existing Node.js, CommonJS, TypeScript, or Electron applications incrementally to AIPod while preserving business behavior. Use for migration analysis, bounded implementation, or resuming an existing migration; not for generating a new application or unrelated refactoring.
---

# AIPod migration

Use the current coding agent for the initial extraction and adaptation of legacy code.
Do not start another generator to rewrite the repository. A later, authorized Pod takeover
uses the actual AIPod workflow to validate ongoing maintenance, as described below.
Scripts provide evidence, not architectural decisions or write isolation.

## Choose the requested outcome

- For an assessment or plan, inspect code and produce a file-level migration proposal.
  Do not change application code merely because this skill was selected.
- For implementation, continue through the requested scope: extract or adapt code,
  update call sites, run meaningful checks, and report remaining limits.
- For a resumed migration, read the existing migration record and current changes first.
  Do not treat an old passing result as evidence for edited sources.

Distinguish **bootstrap migration** from **Pod-managed maintenance**. During bootstrap,
the coding agent may adapt legacy files and initialize the component registry within the
explicit batch scope; this is not Owner-enforced execution. For modules already managed
by Pod, route changes through their owning Agents. A migration allowlist does not grant
another Owner write access or permission to edit controller state. Read
[references/pod-handoff.md](references/pod-handoff.md) before planning takeover or changing
an already managed module. Runtime-only integration may be the requested outcome; do not
silently expand it into a model-backed takeover trial.

## Establish the real baseline

Read repository instructions, package entry points, lockfiles, core call paths, and
related tests. Use local source as evidence for runtime behavior; old documentation
may describe an earlier implementation. Trace shared state and external side effects,
not only imports or directory names. Do not import an Electron entry point to inspect it.

Record the current revision, existing user changes, migration objective, selected
flow, and verified behavior in a project-local migration record. Use
[references/migration-record.md](references/migration-record.md) for its contents.

Choose one bounded behavior to migrate first. A pure transformation with observable
inputs and outputs is usually a useful pilot. Moving files into five folders alone
does not establish AIPod contracts or runtime enforcement.

Read [references/node-integration.md](references/node-integration.md) before choosing
module layout, Provider adapters, the production loader, or retry policy. Inspect the
actual AIPod version available locally; do not assume documentation or a registry version
contains a particular fix. If the target runtime is unavailable, complete analysis and
identify that dependency rather than inventing API calls.

## Implement in bounded batches

1. Write the input/output contracts, dependency boundary, exact files allowed to change,
   and acceptance checks for the batch. Use existing authorization; routine migration
   choices do not require a separate approval round.
2. Run the relevant baseline tests and retain their result. Distinguish existing failures
   from failures caused by the migration. Use synthetic or explicitly authorized data.
3. Before editing, take a source snapshot using the helper below. Preserve current dirty
   edits; never use a repository reset as migration rollback.
4. Adapt existing infrastructure through narrow Providers. Extract focused Services and
   put multi-Service orchestration in Pipelines. Keep UI/event handlers at Interface
   boundaries and avoid exposing a global application object to every component. Follow
   the layout and registration requirements below for newly migrated components.
5. Compare the new behavior with the old behavior using fixtures or pure shadow execution.
   Shadow mode must not duplicate network requests, writes, replies, or other side effects.
6. Run the scope check, the installed AIPod layout/registry validation, type checks and
   behavioral tests. Follow the concrete verification path in
   [references/node-integration.md](references/node-integration.md). Check newly migrated
   registrations against the public-entry rule even if the runtime accepts legacy flat
   components. After a repair, repeat the same behavioral verification; syntax success
   and a successful shell exit alone do not establish requirement coverage.
7. Record completion only when the batch's acceptance criteria pass. Continue to the next
   batch when it is part of the requested scope. Keep failed batches resumable with evidence.

Scope helper (Python 3.10+, Git; replace placeholders and use absolute paths):

```text
python <skill>/scripts/migration_guard.py snapshot --root <repo> --batch card-context --allow path/to/current.js --allow path/to/new.ts --allow path/to/test.js
python <skill>/scripts/migration_guard.py check --root <repo> --batch card-context
```

The baseline is stored in `.aipod-migration/<batch>.baseline.json`. Allowed entries are
exact repository-relative files, not globs or directories. The helper compares content
with the pre-edit snapshot, including initially dirty and non-ignored untracked files.
It refuses to overwrite a baseline. Use a new batch ID after recording the prior outcome.
Read [references/guard.md](references/guard.md) for limits and result meanings.

Do not edit the baseline or expand its allowlist to conceal unexpected edits. Inspect
those edits, preserve unrelated user work, and establish a new explicit batch if the
actual task requires a broader change. Source scope passing is not behavior passing.

## Match the current architecture

- Models hold business data; import them rather than inject them.
- New Provider/Service code uses `src/<layer>/contracts/`, `impl/`, and `public/`.
  `contracts/` contains stable types and behavior contracts and must not depend on
  `impl/` or `public/`. `impl/` holds implementation and local helpers. `public/` contains
  thin explicit named re-exports, not business logic or forwarding wrapper classes.
- Register new Provider/Service IDs through their `public/` entry files. The exported
  name must match the registered ID. Multiple IDs may share an entry; remove only the
  intended named export when unregistering one. Preserve existing flat registrations
  unless moving them is in scope; legacy compatibility is not a template for new entries.
- Across layers, import Provider/Service capabilities through `public/` and types
  through `contracts/`, never another layer's `impl/`. Services cannot import, inject,
  or call other Services; composition belongs in Pipelines. Interfaces call registered
  routes through the Runner instead of invoking Services directly.
- Plan nested directories by domain or capability; do not impose mirrored trees or
  register every helper. Keep legacy adapters explicit about ungoverned behavior.

These paths are relative to the chosen AIPod project root, which may be a bounded module
inside the host repository. Record both roots; the source guard still uses the Git root.
See the integration reference for an example and version-sensitive validation details.

## Preserve application semantics

Extract project-specific invariants from code and tests: identity keys, message order,
batch boundaries, deduplication, submission/acknowledgment states, human overrides,
failure handling, and shutdown. Resolve consequential conflicts with the user when
source/tests cannot establish the intended behavior; continue independent analysis.

Do not wrap a whole legacy orchestrator as one Service and call the migration complete.
Temporary adapters must identify which legacy behavior remains outside AIPod enforcement.
Keep runtime handles and mutable session state in Providers; contracts describe business
data and outcomes. Distinguish expected skips from faults and queued from confirmed sends.

On failure, repair the evidence-backed batch and repeat its checks. After two unsuccessful
repair attempts with the same evidence, reassess the boundary instead of broad rewriting.
If progress requires missing credentials, real platform resources, a changed business
rule, or work outside the authorized scope, record the blocker and complete unaffected work.

## Deliver and hand off

Deliver changed files, actual check results, the updated migration record, and rollback
conditions. Report behavior equivalence, architecture validity, host execution and Pod
takeover separately, with evidence or an explicit unverified status for each. Scope checks
are additional evidence, not a substitute for any of those outcomes. Follow
[references/pod-handoff.md](references/pod-handoff.md) when takeover is in scope.
Explain which behavior is now governed and what is still legacy. Do not claim
live-account or packaged-application validation from mocks alone. A rollback must account
for in-flight and already-submitted side effects; never replay them automatically.

Commit, push, publication, live customer messages, or new remote model transfers require
authorization for those actions; creating a migration skill does not authorize them.
