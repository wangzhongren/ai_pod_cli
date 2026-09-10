# From bootstrap migration to Pod maintenance

Read this when planning Pod takeover or modifying an already managed module. Do not
require a model-backed trial for an analysis-only or runtime-only migration request.

## Ownership boundary

The bootstrap coding agent can adapt old call sites and initialize registrations in the
authorized batch. A content allowlist monitors that work after editing; it does not give
it the controller's protected execution semantics. Prefer a bounded AIPod module root
when the host's remaining files should not be exposed to a whole-layer revision.

Once the module is managed, use its actual Owner workflow:

| Owner | Source scope relative to the AIPod root |
|---|---|
| Model | src/models/ |
| Provider | src/providers/ |
| Service | src/services/ |
| Pipeline | src/pipelines/ |
| Interface | src/interfaces/ and interfaces/ |

Owners also have their tests and docs directories. Pod handles shared configuration and
dependencies; the controller writes aipod.json and .aipod/plan.json. Check the installed
WorkspaceTools grants rather than assuming this table permits arbitrary files.

For cross-owner corrections, submit request_change with target files, reason and desired
change; Pod decides and dispatches the owner. The requester never gains upstream writes.
Recheck affected downstream layers after upstream changes. Do not directly patch managed
upstream files or plan/registration state to bypass a failed handoff. Legacy host changes
outside the managed root remain a separate bootstrap batch with its own checks.

## Establish real state

Confirm bootstrap behavior, current layout/registrations and the real host loading path
before takeover. Custom host-injected factories can make CLI loading different from the
shipped path; establish a safe host testing adapter and document that distinction first.

Inspect the installed CLI and current state before choosing the command. In the current
Node implementation, `init` creates a manifest but not a completed plan. Do not rerun init
over existing registrations, manufacture complete stage records, or call revise without
an actual prior plan. For a first authorized takeover, the ordinary command is:

```sh
aipod-node pod --file /absolute/takeover-requirements.md --project-root /absolute/aipod-root
```

Describe the existing behavior, exact public contracts, remaining legacy boundary and
intended bounded work in the requirements. This starts the real workflow; it is not an
import-only or read-only adoption command. It may inspect and modify all pending layers.
Choose a project boundary compatible with that scope before running it. Let the
controller derive status from actual work and checks.

With real prior state, a subsequent requested revision can use:

```sh
aipod-node pod --stage auto --file /absolute/change.md --project-root /absolute/aipod-root
```

For interruption, inspect the persisted state and use the installed version's resume
path with the same original objective. Current Node `ConstructionAgent.run(originalObjective)`
loads matching state; a different objective starts a new plan, and revise initializes a
new revision. Do not synthesize status or clear state to make recovery appear successful.

Only run the model-backed workflow when existing user authorization covers it and the
necessary project context transfer to the configured endpoint. A request to edit this
Skill alone does not authorize a live migration trial. Complete local migration checks
first if takeover execution is not authorized or not requested, and mark takeover
unverified or not_requested rather than blocking the runtime-only deliverable.

## Verify takeover and record its limits

Use one small requested change within the migrated behavior, then run independent
acceptance for it and existing behavior. Record actual files changed, scope-check results,
layout validation, relevant Owner dispatch and downstream checks, and controller completion.
Do not invent an unrelated business change solely to get a passing takeover result.

Test cross-owner correction or cancellation/resume when included in the requested scope
or necessary for the promised maintenance capability. Preserve partial files/state, resume
through the real controller and repeat acceptance. Report what was actually exercised;
a within-owner success does not prove cross-owner dispatch, and cooperative cancellation
does not prove crash recovery or recovery of external transactions.

Check revisionScope in the installed Node source. At the version reviewed with this Skill,
any Provider/Service public/ registration causes it to fall back to whole-layer revision.
This preserves Owner boundaries but does not enforce an exact per-component allowlist.
Do not remove public entries or widen a recorded batch to hide this limitation. If the
broader scope is incompatible with the authorized batch, narrow the project boundary or
leave takeover unverified and explain the missing scope capability. Recheck this behavior
when upgrading rather than promising that the fallback is permanent.

Report four outcomes separately: behavior equivalence, architecture validity, host
execution, and Pod takeover. Successful application tests with an exhausted Agent budget
are business evidence, not a completed handoff. Record runtime-only integration accurately
when Pod ownership, custom loader compatibility, or recovery remains unverified.
