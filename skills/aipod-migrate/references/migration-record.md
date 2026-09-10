# Migration record

Maintain a concise Markdown or JSON record in the project's existing engineering docs or
`.aipod-migration/`. Its contents should enable another agent to resume without chat history:

- Objective and whether the request is analysis, pilot implementation, or a larger migration.
- Repository revision, relevant pre-existing changes, host runtime and AIPod version/source.
- Git repository root, AIPod project root, bootstrap or Pod-managed mode, and the explicit
  legacy boundary. List host adapters and files not covered by Pod ownership.
- Entry points and concrete call paths; code/test evidence for required business behavior.
- Component mapping: old file/symbol, target layer, contract, dependencies, call sites,
  public registration path/ID, implementation and contract paths, owning Agent, and any
  remaining legacy responsibilities.
- Each batch: ID, exact allowed files, scope snapshot path, acceptance criteria and status
  (`planned`, `in_progress`, `verified`, or `blocked`).
- Verification commands as argument arrays, execution directory, exit codes, concise
  results, and the code revision/content state they apply to. Never store secrets in commands.
- Failure evidence and local repairs; scope-check result separately from behavioral results.
- Separate outcomes for behavior equivalence, architecture validation, host execution and
  Pod takeover: `passed`, `failed`, `unverified`, or `not_requested`, with supporting
  commands and source hashes/revisions. Batch `verified` applies only to its stated scope.
- For takeover: official initialization/resume command, actual controller state, trial
  objective, observed write scope, Owner changes, downstream rechecks and acceptance.
  Record cancellation/resume separately when tested; never infer it from files surviving.
  Distinguish business tests passing from the Agent reaching complete. Record any
  whole-layer scope fallback and untested custom-loader or lifecycle behavior.
- Rollback switch/boundary, in-flight work ownership and side effects that must not replay.
- Next action and unverified external dependencies (for example packaged Electron, native
  DLLs or real test accounts).

Record contracts as concrete fields/types and semantics, not only prose descriptions.
For example an ID may need to remain a string even if all observed characters are digits.
Treat a canceled or skipped action according to business semantics, not automatically as
an infrastructure fault. Do not change original acceptance criteria to hide a failure.

If two sources conflict, cite both and distinguish observed implementation from desired
behavior. Resolve important intent gaps before modifying that behavior.
