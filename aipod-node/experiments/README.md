# Orders revision experiment

Run from the repository root after `cd aipod-node && npm run build`:

```sh
node aipod-node/experiments/orders.mjs
node aipod-node/experiments/orders.mjs --live
```

The first command uses synthetic local orders, in-memory inventory and notification
strings. It checks 10 baseline business cases, compile-time and runtime invalid values,
Model dependency propagation and conservative scope fallback.

`--live` uses the existing AIPod model configuration. It sends synthetic fixture metadata
and the discount requirement to that endpoint; it makes no real business transactions.
It runs three independent trials each of `auto` and explicit `services` revision,
starting from identical fixtures. Source generation defaults to the production XML-like protocol. Each trial has a 20-request cap and a 90-second
request timeout. Authentication, transport and rate-limit failures stop remaining trials.
Generated Interface verification/lifecycle commands are rejected by this experiment.

Acceptance checks live in the experiment script, outside the generated projects. They
cover discount boundaries (0, 9999, 10000, 10001, 20000 cents), stock reservation and
insufficient-stock behavior, and the exact notification message. The script records
Agent completion separately from acceptance, model request counts, elapsed time, source
hash changes, classifier targets and use of the typed Context API. Request counts are
not token cost; source hashes are not a full side-effect audit. The typed-API scan is a
textual diagnostic, not proof of sound typing.

Each execution prints its temporary `report.json` location. Generated projects and
synthetic model request/response evidence are retained beside it. Credentials and the
configured endpoint are not written into reports.

Options:

- `--repetitions=1`: change trial count.
- `--auto-only`: run only automatic revisions.
- `--request-timeout-ms=120000`: override the default experiment request timeout of 90000 ms; 120000 matches the production client default.
- `--source-format=json`: comparator-only adapter that replaces the XML source-output
  instruction with the former JSON `content` envelope, then passes the decoded source
  through the same generation validation and independent acceptance. Plans and repair
  remain JSON in both arms. This is not the default production generation path.
- `--json-compat`: historical diagnostic adapter that appends `Return a strict JSON
  object.` to JSON requests. The production client now supplies that instruction itself;
  old reports retain this flag to distinguish the earlier workaround.

These are small synthetic trials, not a general reliability benchmark. Existing tests
(`npm test` in `aipod-node`) separately cover failed-run resume and frozen-file repair
boundaries using deterministic model responses.

Example paired protocol smoke (one trial per arm, insufficient for reliability statistics):

```sh
node aipod-node/experiments/orders.mjs --live --auto-only --repetitions=1
node aipod-node/experiments/orders.mjs --live --auto-only --repetitions=1 --source-format=json
```

Reports now include the checked-out revision, whether tracked files have local edits,
the source format and each call's response character count. JSON response sizes are
re-serialized object lengths, not original wire bytes; neither count measures tokens.
Historical reports preserve their original revision and results.

Published evidence replaces machine-specific absolute paths with `<tmp>` and `<aipod-checkout>`; results and acceptance criteria are unchanged.
