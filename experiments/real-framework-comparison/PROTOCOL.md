# FlaskBB continuous registration maintenance pilot

Requirements and acceptance were frozen before any live inference. The isolation correction below invalidated the first attempt; the clean rerun keeps the same requirements. This is one project and one sequence, not a leaderboard or a statistical superiority claim.

## Arms

1. Official mini-SWE-agent 2.4.6, original FlaskBB.
2. The same official mini-SWE-agent, migrated registration slice.
3. AIPod Python at 97f782e, default translated mode, identical migrated registration slice.

The first comparison includes migration; the second/third comparison holds initial source identical and changes orchestration. Mini uses the official mini.yaml prompts, DefaultAgent, LitellmModel native bash tool and LocalEnvironment execution semantics. Its supported environment is wrapped with macOS filesystem/network containment. Model serialization redacts credentials. Neither native agent loop nor tool parsing is replaced by our own baseline.

All arms use the same configured API endpoint/model, 16,384 maximum output tokens, 120-second request timeout, existing installed dependencies and compiled translations. Mini uses temperature 0.1; AIPod retains native per-role temperatures (worker and translator 0.1, impact classification and approval 0). Each round allows 180 actual HTTP requests including conversions and retries, at most 540 per arm for the full three-round sequence. No hidden budget: all roles receive the remaining count. Native internal step/error limits remain applicable. Retries count even if no useful response is produced. Token usage comes from raw API responses; no dollar price is invented for an unpriced model. Output tokens include any separately reported reasoning tokens. Cached input is reported separately; summing tokens does not establish a monetary cost ranking. Arms run concurrently on the same host, so observed time is descriptive rather than a controlled CPU benchmark.

An atomic aggregate ledger caps all attempts at the user's authorized 1,620 HTTP requests, including 185 reserved requests in the invalidated attempt. The clean nine-cell rerun therefore shares at most 1,435 remaining requests, subject to each cell's 180-request cap. If the aggregate cap is reached, later cells are censored, not scored as completed failures. Completed valid data excludes the invalidated attempt; its costs are reported separately.

Each round starts a fresh native task conversation and preserves the previous round's files. Earlier requirements are repeated in later task descriptions. Rounds proceed even after a failed delivery, so later work may recover previous defects. No human edits to trial application code after launch. No hidden-test failure feedback is supplied between rounds. One run per arm; no best-of-N selection.

## Requirements and evaluation

See tasks.json for the exact common requirements. The sequence covers identity normalization, database-driven registration group restrictions, and validation failure-hook resilience.

The fixed external test_acceptance.py contains 26 cumulative cases: 8 in round one, 18 through round two, 26 through round three. It imports the same original FlaskBB public API in all arms. Agents cannot read the file or external results through their shell. Both initial sources produce the identical 22 failures / 4 passes when all future requirements are evaluated.

Original FlaskBB regression: 454 tests, all passing in original and migrated baselines. The initial missing compiled gettext catalog was resolved using the upstream documented pybabel build command in both baselines. No original test was modified. Full regression and independently authored cumulative acceptance run after each submitted or interrupted/budget-exhausted round. Original-test hashes are compared. A changed original test invalidates an unqualified regression-pass claim.

Primary outputs: cumulative requirement pass/fail, original regressions, native delivery status, changed files and human code repairs. Separate outputs: HTTP/token usage, execution time, control/translation failures, AIPod static layout validity. Changed-file count does not automatically mean unrelated churn; classify diffs against requirements. Migration diff and environment freeze are recorded separately. Setup was performed by the experimenter, not benchmarked as an autonomous migration product.

## Limits

Only the registration/validator slice is migrated. Legacy plugin bodies, ORM types and the rest of FlaskBB remain host compatibility boundaries. Several contracts are explicitly `any`; this is not full-project trusted-state enforcement. Source count and regression breadth do not imply that these three maintenance issues are intrinsically large. This pilot can find overhead/regression/failure mechanisms; it cannot establish superiority across projects/frameworks or prove recovery from an arbitrary process crash. Fresh process invocation between rounds tests file persistence only, not mid-operation recovery. No claims about the paper's complete trusted-state mechanism are supported by this pilot.

The initial migrated plan marks existing layers complete solely to exercise maintenance mode. Behavioral equivalence is independently checked with the 454-test suite; this seeded plan is not represented as a model-produced migration or evidence certificate.

## Isolation correction

The first attempt's mini-original worker read baseline-future logs left in /tmp. Those logs contained hidden acceptance snippets, despite the acceptance source and round-output directories being denied. Its 8/8 result is invalid. All three arms of that attempt were stopped and archived under /tmp/aipod-flaskbb-trial-invalid-1; no application patch or conversation is reused. The clean rerun denies file contents in the entire temporary directory except the current arm's project and installed virtualenv, and denies user-home reads. File metadata remains available for OS path resolution. Explicit denial probes cover baseline logs, XML, the invalidated patch, task manifest, another arm, and the acceptance source. The full original regression must pass under that restriction before new model calls. All arms now receive their explicit working directory to avoid irrelevant environment discovery.

There was also a zero-call preflight error from nested sandbox-exec; containment is now added to AIPod's native profile, preserving its original write grants. These are experimenter setup defects, not product regressions. Theme static assets are a materialized upstream symlink in all three copies; exclude them from the migration-source cost. The actual migration touches 35 files including empty package initializers (420 lines added, 148 removed).

## Reproduction

Bootstrap the pinned upstream FlaskBB source with bootstrap.py. Install the pinned upstream mini-SWE-agent plus FlaskBB and AIPod in the same Python 3.12 environment. Compile FlaskBB translations. run.py --prepare copies the baseline sources, freezes hashes and dependencies, then run.py --arm ARM --round N --live runs one bounded cell. Paths are explicit in run.py for this local experiment. Raw logs and snapshots stay under /tmp/aipod-flaskbb-trial; the final report and selected reproducibility artifacts are copied into this directory.
