# Node/Electron integration decisions

## Inspect before selecting an integration

Check both the host and AIPod package entry points, exports, runtime requirements,
module format, construction APIs, loader behavior and verification APIs. These details
are version dependent. Use current local source or official package documentation.

For an existing CommonJS host, an independently built ESM/TypeScript module loaded via
dynamic import may avoid changing the whole application's module mode. Verify actual
Electron Node compatibility and packaging. Do not require this layout for a project
that already has a suitable TypeScript build.

## Adapt rather than duplicate infrastructure

Existing factory functions and singleton objects may need wrappers around the runtime's
DI factory mechanism. Bind existing sockets, database workers and process bridges once.
Do not recreate a connection, Worker or native process per route call.

If PipelineContext clones data, pass only cloneable data with a meaningful contract.
Keep BrowserWindow, webContents, callbacks, database connections, processes and mutable
session ownership in Providers. Refer to a session by stable identity and, where needed,
a generation/version. Recheck identity immediately before a sensitive side effect.

Service-to-Service execution belongs in a Pipeline. Pure helpers can remain ordinary
functions, but do not disguise another registered Service as a helper or an unrestricted
Provider to bypass orchestration checks. A temporary legacy adapter is an explicit
compatibility boundary, not proof that its internals are governed.

## Production execution versus construction

Some AIPod versions compile sources and recreate build directories during dynamic loading.
Inspect that path before using it in a hot message loop or a read-only packaged application.
Prefer build-time compilation and a persistent Container/Runner where the host needs it.
Each event should have its own data context. Test startup order and shutdown ownership.

Custom host-injected factories may not be supported by the CLI/Studio loader. Keep one
component/contract registry as the source of truth and implement an explicit host testing
adapter when required. Do not manufacture completed Agent stage records to make an old
project appear automatically migrated. Test the runtime path actually shipped to users.

## Behavior checkpoints

Map input acceptance, persistence, submission, response and delivery acknowledgment before
adding retries. A request submitted to a backend can time out locally after acceptance.
Retrying the whole Pipeline can duplicate the request or a customer reply.

Treat queued, confirmed, failed and unknown outcomes distinctly if the host already does.
Preserve deduplication identity, fixed batch boundaries, media ordering, human override
checks and cancellation generations. Parallelism and streaming operators do not establish
equivalence with the old scheduler. Keep durable queue state unless migration specifically
requires changing it. Fail-fast behavior may differ from a batch that records an item
failure and intentionally continues.

For comparison, run both pure transformations on a cloned synthetic input and compare
outputs. For side effects, substitute recording Providers and compare the ordered intent
and state transitions. Only one implementation owns real sends/writes at a time.

## Verification and trace

Run original regression tests plus meaningful cases for the extracted boundary. Replace
source-text-shape tests with equivalent behavior assertions when moving their implementation;
do not delete coverage simply to make a refactor pass.

Loading or importing an adapter is not proof that its start/send path works. Review what
the runtime's smoke check actually executes. Run explicit route/adapter behavioral tests
after every repair, and test fresh module loading if the host caches imports.

A generic secret-name redactor may miss Cookie, ticket, chat content or platform identity.
Use a trace field allowlist suited to the application, synthetic replay fixtures, and the
host's writable runtime directory with retention limits. Never copy production credentials
into sample fixtures or source snapshots.
