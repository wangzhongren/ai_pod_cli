import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile, readdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  analyzeUtilitySource, callWithUtilityTools, encodeSourceArtifact, listUtilities, loadProject,
  loadRunner, planStage, readUtility, validateUtilityImports, visibleLedger, writeUtility,
  type ModelClient, type UtilityCase,
} from "../src/index.js";

const source = `export class NumberUtils {
  static clamp(value: number, minimum: number, maximum: number): number {
    if (minimum > maximum) throw new RangeError("invalid bounds");
    return Math.min(maximum, Math.max(minimum, value));
  }
}`;
const cases: UtilityCase[] = [{ method: "clamp", args: [8, 0, 5], expected: 5 },
  { method: "clamp", args: [-2, 0, 5], expected: 0 }, { method: "clamp", args: [1, 5, 0], raises: "RangeError" }];
const request = { id: "NumberUtils", description: "Clamp a number to inclusive bounds", source, cases };

async function projectRoot(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "aipod-utilities-"));
  await writeFile(join(root, "aipod.json"), JSON.stringify({ schemaVersion: 1, beans: [], routes: [], interfaces: [] }));
  return root;
}

async function addCallers(root: string): Promise<void> {
  for (const layer of ["models", "providers", "services", "pipelines", "interfaces"]) await mkdir(join(root, "src", layer), { recursive: true });
  for (const [name, bound] of [["LowerProvider", 4], ["UpperProvider", 10]] as const) {
    await writeFile(join(root, `src/providers/${name}.ts`), `import { NumberUtils } from "../utils/NumberUtils.js";
      export class ${name} { limit(): number { return NumberUtils.clamp(${bound}, 0, 100); } }`);
  }
  for (const [name, provider, input, output] of [["FirstService", "LowerProvider", "value", "first"], ["SecondService", "UpperProvider", "first", "value"]] as const) {
    await writeFile(join(root, `src/services/${name}.ts`), `import { NumberUtils } from "../utils/NumberUtils.js";
      import type { ${provider} } from "../providers/${provider}.js";
      export class ${name} {
        constructor(private readonly dependencies: { ${provider}: ${provider} }) {}
        execute(ctx: { get(key: string): unknown }) {
          return { ${output}: NumberUtils.clamp(Number(ctx.get(${JSON.stringify(input)})) + 2, 0, this.dependencies.${provider}.limit()) };
        }
      }`);
  }
  await writeFile(join(root, "src/models/Point.ts"), 'import { NumberUtils } from "../utils/NumberUtils.js"; export class Point { static coordinate(value: number): number { return NumberUtils.clamp(value, -100, 100); } }');
  await writeFile(join(root, "src/pipelines/calculate.ts"), "export {};\n");
  await writeFile(join(root, "aipod.json"), JSON.stringify({ schemaVersion: 1,
    beans: [
      ...["LowerProvider", "UpperProvider"].map((id) => ({ id, category: "provider", file: `src/providers/${id}.ts`, description: "Bounds", dependencies: [], inputs: {}, outputs: {} })),
      ...[["FirstService", "LowerProvider", "value", "first"], ["SecondService", "UpperProvider", "first", "value"]].map(([id, provider, input, output]) => ({ id, category: "service", file: `src/services/${id}.ts`, description: "Adjust a value", dependencies: [provider], inputs: { [input!]: { type: "number" } }, outputs: { [output!]: { type: "number" } } })),
    ],
    routes: [{ name: "calculate", description: "Calculate", services: ["FirstService", "SecondService"], execution: { mode: "sequential" }, file: "src/pipelines/calculate.ts" }], interfaces: [],
  }));
}

test("one global utility is imported by multiple actual Providers, Services and a Model", async () => {
  const root = await projectRoot();
  try {
    const entry = await writeUtility(root, request);
    assert.match(entry.methods[0]!.signature, /clamp\(value: number/);
    await addCallers(root);
    const execution = await (await loadRunner(root)).run("calculate", { value: 7 });
    assert.equal(execution.context.get("value"), 6);
    assert.deepEqual(await validateUtilityImports(root), []);
    const helper = await readUtility(root, "NumberUtils");
    assert.equal(helper.callers.length, 5);
    assert.match(helper.source, /class NumberUtils/);
    assert.equal((await listUtilities(root, "clamp bounds")).length, 1);
    const project = await loadProject(root);
    for (const stage of ["models", "providers", "services", "pipelines", "interfaces"] as const) {
      assert.equal((visibleLedger(project, stage).utilities as unknown[]).length, 1);
    }
    assert.ok(!project.beans.some((bean) => bean.id === "NumberUtils"));
    await writeFile(join(root, entry.path), source.replace("Math.min", "Math.max"));
    await assert.rejects(readUtility(root, entry.id), /hash drift/);
    await assert.rejects(loadRunner(root), /hash drift/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("utility discovery does not write directories and undeclared imports are rejected", async () => {
  const root = await projectRoot();
  try {
    assert.deepEqual(await listUtilities(root), []);
    await assert.rejects(readUtility(root, "Missing"), /Unknown utility/);
    assert.deepEqual(await readdir(root), ["aipod.json"]);
    await mkdir(join(root, "src/providers"), { recursive: true });
    await mkdir(join(root, "src/utils"), { recursive: true });
    await writeFile(join(root, "src/utils/Undeclared.ts"), "export class Undeclared {}\n");
    await writeFile(join(root, "src/providers/forward.ts"), 'export { Undeclared } from "../utils/Undeclared.js";');
    assert.match((await validateUtilityImports(root)).join("; "), /unregistered Utility/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("utility generation tools are genuinely selected by the planning model and XML creates the helper", async () => {
  const root = await projectRoot();
  let action = 0;
  let sourceCalls = 0;
  try {
    const client: ModelClient = {
      complete: async (system, user) => {
        assert.match(system, /^PLAN_STAGE:services/);
        assert.match(system, /write_utility/);
        action += 1;
        if (action === 1) return { tool: "list_utilities", arguments: { query: "bounds" } };
        if (action === 2) { assert.match(user, /"utilities":\[\]/); return { tool: "write_utility", arguments: { id: request.id, description: request.description, instruction: "Clamp without mutating anything", cases } }; }
        if (action === 3) { assert.match(user, /"status":"success"/); return { tool: "read_utility", arguments: { id: request.id } }; }
        assert.match(user, /export class NumberUtils/);
        return { summary: "Reuse NumberUtils", components: [] };
      },
      completeText: async (system) => { sourceCalls += 1; assert.match(system, /^GENERATE_UTILITY:NumberUtils/); return encodeSourceArtifact({ path: "src/utils/NumberUtils.ts", content: source }); },
    };
    const project = await loadProject(root);
    const plan = await planStage(client, "services", "Build bounded calculations", project);
    assert.equal(plan.summary, "Reuse NumberUtils");
    assert.equal(action, 4); assert.equal(sourceCalls, 1);
    assert.equal(project.utilities![0]!.id, "NumberUtils");
    let calls = 0;
    await planStage({ complete: async () => { calls += 1; return { components: [] }; } }, "models", "No helper needed", project);
    assert.equal(calls, 1, "metadata without a tool retains the original model call count");
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("unknown and malformed tool actions return error evidence; existing utilities cannot be overwritten by models", async () => {
  const root = await projectRoot();
  try {
    const entry = await writeUtility(root, request);
    let step = 0;
    await callWithUtilityTools({ complete: async (_, user) => {
      step += 1;
      if (step === 1) return { tool: "shell", arguments: {} };
      if (step === 2) { assert.match(user, /Unknown utility tool/); return { tool: "read_utility", arguments: { id: entry.id }, components: [] }; }
      if (step === 3) { assert.match(user, /Malformed utility tool/); return { tool: "write_utility", arguments: { id: entry.id, description: "overwrite", instruction: "change it", cases } }; }
      assert.match(user, /model tools cannot overwrite/); return { components: [] };
    }, completeText: async () => { throw new Error("overwriting must be refused before generating source"); } }, root, "PLAN_STAGE:services", "test");
    assert.equal((await readUtility(root, entry.id)).sha256, entry.sha256);
    let repeats = 0;
    await assert.rejects(callWithUtilityTools({ complete: async () => { repeats += 1; return { tool: "list_utilities", arguments: {} }; } }, root, "PLAN_STAGE:models", "loop"), /action limit/);
    assert.equal(repeats, 9);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("failed cases, missing cases, unsafe code and type errors never publish a utility or partial registry", async () => {
  const root = await projectRoot();
  try {
    await assert.rejects(writeUtility(root, { ...request, cases: [] }), /explicit method/);
    await assert.rejects(writeUtility(root, { ...request, cases: [{ method: "clamp", args: [8, 0, 5], expected: 99 }] }), /Utility verification failed/);
    await assert.rejects(writeUtility(root, { ...request, source: source.replace("return Math.min(maximum, Math.max(minimum, value));", 'return "wrong";') }), /type verification failed/);
    for (const content of [
      'import { readFileSync } from "node:fs"; export class Bad { static run(): string { return readFileSync("secret", "utf8"); } }',
      "export class Bad { static run(): string { return process.cwd(); } }",
      "export class Bad { static value = 1; static run(): number { return Bad.value++; } }",
      "export class Bad { static run(context: PipelineContext): number { return 1; } }",
      "let state = 0; export class Bad { static run(): number { return state++; } }",
    ]) assert.throws(() => analyzeUtilitySource(content, "Bad", "src/utils/Bad.ts", []), /cannot|Utility|Utilities/);
    await assert.rejects(writeUtility(root, { id: "Mutator", description: "bad", source: "export class Mutator { static sorted(values: number[]): number[] { return values.sort(); } }", cases: [{ method: "sorted", args: [[2, 1]], expected: [1, 2] }] }), /mutated its caller/);
    assert.deepEqual(await listUtilities(root), []);
    await assert.rejects(readFile(join(root, "utility_registry.json")), /ENOENT/);
    assert.deepEqual(await readdir(join(root, "src/utils")), []);
    const entry = await writeUtility(root, request);
    assert.deepEqual(await writeUtility(root, request), entry);
    await assert.rejects(writeUtility(root, { ...request, description: "changed description" }), /updates require/);
    assert.equal((await readUtility(root, entry.id)).sha256, entry.sha256);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("utilities can reuse another registered helper and approved pure standard functions", async () => {
  const root = await projectRoot();
  try {
    await writeUtility(root, request);
    await writeUtility(root, { id: "PathUtils", description: "File extension with bounded length", source: `import { extname } from "node:path";
      import { NumberUtils } from "./NumberUtils.js";
      export class PathUtils { static length(path: string): number { return NumberUtils.clamp(extname(path).length, 0, 10); } }`,
    cases: [{ method: "length", args: ["file.txt"], expected: 4 }] });
    assert.ok((await readUtility(root, request.id)).callers.includes("src/utils/PathUtils.ts"));
    assert.equal((await listUtilities(root)).length, 2);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("explicit updates require a matching hash and verify real callers only in the isolated candidate project", async () => {
  const root = await projectRoot();
  try {
    const original = await writeUtility(root, request);
    await assert.rejects(writeUtility(root, { ...request,
      source: source.replace("Math.min(maximum, Math.max(minimum, value))", "Math.min(maximum, Math.max(minimum, value)) + 1"),
      cases: [{ method: "clamp", args: [8, 0, 5], expected: 6 }],
    }, { expectedSha256: original.sha256, verificationCommand: [process.execPath, "-e", "process.exit(0)"] }), /Utility verification failed/);
    await addCallers(root);
    const registry = await readFile(join(root, "utility_registry.json"), "utf8");
    const changed = { ...request, source: source.replace("Math.min(maximum, Math.max(minimum, value))", "Math.min(maximum, Math.max(minimum, value)) + (maximum === 10 ? 1 : 0)"),
      cases: [{ method: "clamp", args: [6, 0, 10], expected: 7 }] };
    await assert.rejects(writeUtility(root, changed, { expectedSha256: "bad", verificationCommand: [process.execPath, "--version"] }), /hash mismatch/);
    await assert.rejects(writeUtility(root, { ...request,
      source: source.replace("value: number", "value: string").replace("minimum, value", "minimum, Number(value)"),
      cases: [{ method: "clamp", args: ["8", 0, 5], expected: 5 }],
    }, { expectedSha256: original.sha256, verificationCommand: [process.execPath, "--version"] }), /preserve existing public method signatures/);
    const runtime = new URL("../src/index.js", import.meta.url).href;
    const command = [process.execPath, "--input-type=module", "-e", `import assert from 'node:assert/strict'; import {writeFile} from 'node:fs/promises'; import {loadRunner} from ${JSON.stringify(runtime)};
      await writeFile('verification-marker.txt','candidate only'); const run=await(await loadRunner(process.cwd())).run('calculate',{value:7}); assert.equal(run.context.get('value'),6);`];
    await assert.rejects(writeUtility(root, changed, { expectedSha256: original.sha256, verificationCommand: command }), /Utility verification failed/);
    await assert.rejects(writeUtility(root, { ...request, source: `${source}\n// documentation\n` }, { expectedSha256: original.sha256, verificationCommand: [process.execPath, "-e",
      `require('node:fs').writeFileSync('src/utils/NumberUtils.ts', ${JSON.stringify(source)});`,
    ] }), /verification modified candidate sources/);
    assert.equal(await readFile(join(root, original.path), "utf8"), source);
    assert.equal(await readFile(join(root, "utility_registry.json"), "utf8"), registry);
    await assert.rejects(readFile(join(root, "verification-marker.txt")), /ENOENT/);
    const updated = await writeUtility(root, { ...request, source: `${source}\n// Documentation update\n` }, { expectedSha256: original.sha256, verificationCommand: command });
    assert.notEqual(updated.sha256, original.sha256);
    assert.equal((await (await loadRunner(root)).run("calculate", { value: 7 })).context.get("value"), 6);
    await assert.rejects(readFile(join(root, "verification-marker.txt")), /ENOENT/);
  } finally { await rm(root, { recursive: true, force: true }); }
});
