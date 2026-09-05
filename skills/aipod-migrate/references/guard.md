# Source scope guard

Run `scripts/migration_guard.py` with Python 3.10+ and Git. It requires `--root` to be the
Git repository's top-level directory. Each `--allow` names one exact relative file.
Paths cannot escape the repository, target Git metadata, or target the baseline directory.

`snapshot` records SHA-256 hashes of tracked and non-ignored untracked regular files,
including existing dirty changes. It creates a new baseline exclusively and never
overwrites one. `check` reports added/removed/modified paths compared with that snapshot,
marking changes outside the allowlist as violations. It does not reset, repair, stage,
commit or execute any project code.

Exit codes:

- 0: snapshot created or source changes are within scope.
- 1: source changes outside the recorded scope.
- 2: operational/validation error; verification is inconclusive.

Limitations: this is a content-scope check, not a sandbox, behavior test or security
boundary. Git-ignored untracked outputs, permissions, Git metadata, external files, running
processes and external side effects are not covered. Baseline records are excluded to
allow progress notes; do not place application source there. Git submodules and symbolic
links are rejected rather than followed. The snapshot must be taken while files are stable.
Submodules need their own explicit migration boundary; do not silently ignore them.

Do not interpret an unchanged file as correct, or a permitted file as permission to destroy
user edits. A passing result must still be accompanied by behavioral verification.
