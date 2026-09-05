# Migration record

Maintain a concise Markdown or JSON record in the project's existing engineering docs or
`.aipod-migration/`. Its contents should enable another agent to resume without chat history:

- Objective and whether the request is analysis, pilot implementation, or a larger migration.
- Repository revision, relevant pre-existing changes, host runtime and AIPod version/source.
- Entry points and concrete call paths; code/test evidence for required business behavior.
- Component mapping: old file/symbol, target layer, contract, dependencies, call sites,
  and any remaining legacy responsibilities.
- Each batch: ID, exact allowed files, scope snapshot path, acceptance criteria and status
  (`planned`, `in_progress`, `verified`, or `blocked`).
- Verification commands as argument arrays, execution directory, exit codes, concise
  results, and the code revision/content state they apply to. Never store secrets in commands.
- Failure evidence and local repairs; scope-check result separately from behavioral results.
- Rollback switch/boundary, in-flight work ownership and side effects that must not replay.
- Next action and unverified external dependencies (for example packaged Electron, native
  DLLs or real test accounts).

Record contracts as concrete fields/types and semantics, not only prose descriptions.
For example an ID may need to remain a string even if all observed characters are digits.
Treat a canceled or skipped action according to business semantics, not automatically as
an infrastructure fault. Do not change original acceptance criteria to hide a failure.

If two sources conflict, cite both and distinguish observed implementation from desired
behavior. Resolve important intent gaps before modifying that behavior.
