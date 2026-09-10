/** Versioned SDK reference injected by role; examples are compiled and run in tests. */
import type { StageName } from "./types.js";

export const SDK_EXAMPLES = {
  model: `export interface ExampleValue { value: number }
`,
  provider: `import type { ConfigStore } from "aipod-node";

export class ConfiguredProvider {
  constructor(private readonly deps: { ConfigStore: ConfigStore }) {}
  add(value: number): number {
    return value + Number(this.deps.ConfigStore.get("example.increment", 2));
  }
}
`,
  repository: `import type { ModelRepository } from "aipod-node";

export async function repositoryExample(repo: ModelRepository) {
  const saved = await repo.save("examples", { id: "one", value: 3 });
  const found = await repo.get("examples", saved.id);
  const matches = await repo.find("examples", { value: 3 });
  const deleted = await repo.delete("examples", saved.id);
  return { found, matches, deleted };
}
`,
  contracts: `import type { Contract } from "aipod-node";
export const inputs = { value: { type: "integer", required: false } } as const satisfies Contract;
export const outputs = { answer: { type: "integer" } } as const satisfies Contract;
`,
  service: `import type { PipelineContext } from "aipod-node";
import type { ConfiguredProvider } from "../../providers/public/example.js";

export class ExampleService {
  constructor(private readonly deps: { ConfiguredProvider: ConfiguredProvider }) {}
  execute(context: PipelineContext) {
    const ctx = context.typed(
      { value: { type: "integer", required: false } },
      { answer: { type: "integer" } },
    );
    return ctx.output({ answer: this.deps.ConfiguredProvider.add(ctx.get("value") ?? 1) });
  }
}
`,
  pipeline: `import { service, type Container } from "aipod-node";

export function createPipeline(container: Container) {
  return service(container, "ExampleService");
}
export function createRoute(container: Container) {
  return { name: "example", pipeline: createPipeline(container) };
}
`,
  runner: `import { loadRunner } from "aipod-node";

export async function runExample(projectRoot: string) {
  const runner = await loadRunner(projectRoot);
  const { result, context } = await runner.run("example", { value: 3 });
  if (result.status === "failure") throw new Error(result.error.message);
  return { output: result.output, context };
}
`,
  adapter: `import type { InterfaceAdapter, PipelineRunner } from "aipod-node";

export class ExampleAdapter implements InterfaceAdapter {
  constructor(private readonly runner: PipelineRunner) {}
  requiredRoutes(): string[] { return ["example"]; }
  start(payload: Record<string, unknown> = {}) {
    return this.runner.run("example", payload);
  }
}
`,
  interface_check: `import { loadInterface, smokeInterface } from "aipod-node";

export async function checkInterface(projectRoot: string) {
  const report = await smokeInterface(projectRoot, "Example");
  if (report.status !== "passed") throw new Error(JSON.stringify(report));
  const { adapter, context } = await loadInterface(projectRoot, "Example");
  return { adapter, context };
}
`,
} as const;

const example = (name: keyof typeof SDK_EXAMPLES) => "\n```typescript\n" + SDK_EXAMPLES[name] + "```\n";

export const SDK_SECTIONS = {
  models: `MODEL API
Node Models are ordinary TypeScript interfaces/types/classes; there is no Python-style Model
base class. Interfaces are erased at runtime: do not instantiate or inject them. Register their
model IDs/files for the project ledger, and import their types from the registered source.
Use explicit runtime/domain validation where TypeScript's compile-time types are insufficient.
` + example("model"),
  dependencies: `CONFIGURATION, DEPENDENCIES AND STORAGE
Source imports use "aipod-node". Provider/Service constructors receive ONE object keyed by
exact dependency IDs, e.g. deps.ConfigStore or deps.ModelRepository, not positional providers.
The Container resolves Providers/Services as singletons; Models are not injectable.
ConfigStore(projectRoot, file?) requires await config.load() when created manually;
loadContainer/loadRunner already load it. config.get(path, fallback?) -> value/fallback;
await config.reload() reloads configuration. Node config is config.json/config.toml.
ModelRepository(projectRoot, file=".aipod/data.json") is the built-in JSON store.
await repo.save(collection, value, idField="id") -> saved record; id must be string/number.
await repo.get(collection, id) -> record | undefined; await repo.list(collection) -> records.
await repo.find(collection, filters) -> matching records; await repo.delete(collection, id) -> boolean.
Generic record values must satisfy Record<string, unknown>; spread a typed interface into a
plain record when necessary. Use AIPOD_DATA_DIR for temporary data while checking generation.
` + example("repository"),
  providers: "PROVIDER IMPLEMENTATION\nPlace the class in impl/ and expose a thin named re-export in public/.\n" + example("provider"),
  context: `PIPELINE CONTEXT
new PipelineContext(params={}, {data?, branchId?}={}).
context.get(key, fallback?) -> data first, then params, then fallback; set(key, value) -> void.
context.summary() -> {params, data, steps}; context.steps stores execution traces.
Services use context.typed(literalInputs, literalOutputs) -> ContractContext with typed get/set/output.
ctx.get(name) checks a declared input; ctx.set(name, value) writes a checked declared output.
ctx.output(mapping) validates and returns the complete declared output mapping; it does not
itself publish it to the underlying context. Return it from execute(), so the runtime publishes it.
Keep schemas literal in context.typed(...) for field inference; do not cast contracts to any.
`,
  contracts: `REGISTRATION CONTRACTS
Contract = Record<string, ContractField>. Each field is an object with type/required/properties/items.
Types: string, number, integer, boolean, object, array, any. required defaults to true;
required:false permits an absent key, not null for a string/number field. Node contracts have
no automatic default injection; use ?? or domain validation in the Service for optional inputs.
Nested object properties are another Contract; array items are a ContractField.
Do not use Python type strings or prose such as "int 1..60, optional" as a Node field spec.
Specify business ranges explicitly in your domain code. Match finish metadata to the literal
schemas in execute(). Outputs missing on a failure path must be optional or consistently supplied.
validateContract(data, contract={}, prefix="$") -> string[]; [] means valid.
Register Provider/Service public files via finish; the controller maintains aipod.json.
` + example("contracts"),
  services: "SERVICE IMPLEMENTATION\nThe dependency is an existing public Provider; no Service-to-Service calls.\n" + example("service"),
  pipelines: `COMPOSITION API
Import service, sequence, parallel, repeat and type Container from "aipod-node".
service(container, "ServiceId") -> ServiceRef; sequence(...nodes) -> sequential ExecutionNode.
await node.execute(context) -> Result. ServiceRef.retry(retries=3, delayMs=0) and
fallback(otherRef) return configured references. Compose Services only in this layer.
Export createPipeline(container) and createRoute(container) in a pipeline source file.
Before registration, test the factory with a real container. Submit name/file/services/execution
and public inputs in finish.routes. loadRunner builds registered routes from aipod.json's
services/execution metadata; keep that metadata consistent with the generated factory.
` + example("pipeline"),
  runner: `REGISTERED ROUTE EXECUTION
await loadContainer(projectRoot) -> Container; container.resolve(id) -> Provider/Service instance.
await loadRunner(projectRoot) -> PipelineRunner (compiles project sources and loads registered beans).
Reuse a loaded runner in an HTTP server instead of recompiling for every request.
runner.routeNames() -> string[]; await runner.run(routeName, params={}) -> {result, context}.
result is a discriminated Result: success has status="success", output, effects;
failure has status="failure", error={code,message,retryable,details}, effects.
Service errors normally become failure Results; unknown routes and loading/compilation can throw.
An application result may have its own ok/error inside result.output: inspect that too.
Do not treat the outer {result, context} as the output itself or as Python's raw dict result.
typeCheckProject(projectRoot) -> Promise<SemanticDiagnostic[]>; [] means no TS diagnostics.
` + example("runner"),
  interfaces: `INTERFACE SDK — Node signatures and return shapes
InterfaceAdapter is a TypeScript interface: implement it, do not extend it as a runtime class.
requiredRoutes(): string[]; start(payload?: Record<string, unknown>): unknown | Promise<unknown>.
The loader passes a PipelineRunner to the Adapter constructor, not a dependencies dictionary.
For manifest name "Example", export class ExampleAdapter from the registered file.
await loadInterface(projectRoot, name) -> {adapter, context}; it compiles/loads the project.
new InterfaceContext(projectRoot); await context.routeNames() -> string[];
await context.runRoute(route, params={}) -> {result, context}; await context.runner() -> PipelineRunner.
await adapter.start(payload) takes ONLY the payload: do not pass a Python-style InterfaceContext.
start may keep an HTTP/GUI/worker running. Test such entries in a child process, probe them,
then terminate and wait in finally; do not await a long-running start before sending requests.
smokeInterface(projectRoot, name) -> Promise<{status, requiredRoutes, missingRoutes}> checks
route presence only, not functional behavior. There is no universal Node adapter.stop() method.
Interface metadata goes in finish.interfaces: name/file/route/kind/artifacts, plus any intended
lifecycle/verify/permissions. Preserve metadata on updates. lifecycle install/uninstall commands
are argv arrays with {node}/{projectRoot} placeholders, not joined shell strings.
verify entries are {name, command: string[], required: boolean, timeoutMs?} objects.
await verifyInterface(projectRoot, name) -> {status, checks}; an empty checks array proves no behavior.
` + example("adapter") + example("interface_check"),
  verification: `ENTRY AND ACCEPTANCE CHECKS
During Agent checks: node "$AIPOD_NODE_CLI" interface smoke Example --project-root /PROJECT
node "$AIPOD_NODE_CLI" interface run Example --payload '{"value":3}' --project-root /PROJECT
The smoke/verify CLI prints a status object: inspect status/checks, not only its exit code.
For user projects, the equivalent installed binary is aipod-node.
runVerificationCommand(projectRoot, argv, timeoutMs=120000) -> Promise<{status, exitCode,
stdout, stderr, command, durationMs}>. argv is executed without a shell; | is not a pipeline there.
Run actual positive/negative behavior checks and rerun the failed check after its owner repairs it.
Do not mask test exit codes with | tail or || true; the framework already bounds captured output.
A successful import, help command or route-presence smoke is not full acceptance. Report the
checks actually performed and remaining gaps, rather than claiming all requirements passed.
`,
} as const;

export const SDK_ROLE_SECTIONS = {
  models: ["models"],
  providers: ["dependencies", "contracts", "providers"],
  services: ["context", "dependencies", "contracts", "services"],
  pipelines: ["context", "contracts", "pipelines", "runner"],
  interfaces: ["contracts", "runner", "interfaces", "verification"],
  pod: ["contracts", "runner", "interfaces", "verification"],
} as const;

export function sdkReference(role: StageName | "pod"): string {
  if (!Object.hasOwn(SDK_ROLE_SECTIONS, role)) throw new Error(`Unknown SDK reference role: ${role}`);
  return `AIPod Node SDK reference bundled with this framework — role: ${role}.
Use these signatures and examples directly; these SDK snippets are not response instructions.
Example names/paths are placeholders: use the registered names and dependencies in project context.
Preserve AIPOD_NODE_CLI/AIPOD_NODE_MODULE/AIPOD_BUILD_DIR; for one-off ESM checks use
node --input-type=module and await import(process.env.AIPOD_NODE_MODULE).
This reference remains in the system prompt on every turn. Inspect SDK source only for a
specific missing detail or observed discrepancy, not to repeatedly rediscover these APIs.
Other chapters are available through sdkReference(role), exported by aipod-node.
\n` + SDK_ROLE_SECTIONS[role].map(name => SDK_SECTIONS[name].trim()).join("\n\n");
}
