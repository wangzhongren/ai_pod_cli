import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, mkdir, writeFile, readFile, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { SourceGraph, validateLayout } from "../src/agent/component-layout.js";
import { ensureProjectDirectories, saveProject, type ProjectBean, type ProjectManifest } from "../src/agent/project.js";
import { loadContainer } from "../src/loader.js";
import { PipelineContext } from "../src/context.js";
import { addBean } from "../src/operations.js";
import { verifyProject, ConstructionAgent } from "../src/agent/agent.js";
import { WorkspaceTools } from "../src/agent/workspace.js";
import { encodeSourceArtifact } from "../src/agent/source-codec.js";

async function fixture() {
  const root = await mkdtemp(resolve(tmpdir(), "aipod-layout-"));
  const put = async (file: string, source: string) => { await mkdir(dirname(resolve(root, file)), { recursive: true }); await writeFile(resolve(root, file), source); };
  await put("package.json", '{"type":"module"}');
  await put("config.json", "{}");
  await ensureProjectDirectories(root);
  const beans: ProjectBean[] = [
    { id: "Store", category: "provider", file: "src/providers/public/storage.ts", description: "memory", dependencies: [], inputs: {}, outputs: {} },
    { id: "Calculate", category: "service", file: "src/services/public/index.ts", description: "double", dependencies: ["Store"], inputs: {}, outputs: { total: { type: "number" } } },
  ];
  const project: ProjectManifest = { schemaVersion: 1, beans, routes: [], interfaces: [] };
  await saveProject(root, project);
  await put("src/providers/contracts/storage.ts", "export interface Storage { value(): number }\n");
  await put("src/providers/impl/storage/memory.ts", "import type {Storage} from '../../contracts/storage.js'; export class MemoryStore implements Storage { value() { return 6; } }\n");
  await put(beans[0]!.file, "export {MemoryStore as Store} from '../impl/storage/memory.js';\n");
  await put("src/services/contracts/calculation.ts", "import type {PipelineContext} from 'aipod-node'; export interface Calculation { execute(ctx: PipelineContext): unknown }\n");
  await put("src/services/impl/math/rules/arithmetic.ts", "export function double(value: number) { return value * 2; }\n");
  await put("src/services/impl/math/calculate.ts", "import type {PipelineContext} from 'aipod-node'; import type {Store} from '../../../providers/public/storage.js'; import type {Calculation} from '../../contracts/calculation.js'; import {double} from './rules/arithmetic.js'; export class Calculator implements Calculation { constructor(private deps: {Store: Store}) {} execute(ctx: PipelineContext) { ctx.set('total', double(this.deps.Store.value())); } }\n");
  await put(beans[1]!.file, "export {Calculator as Calculate} from '../impl/math/calculate.js';\n");
  return { root, put, beans, project };
}

test("nested public aliases load with real dependency injection and can move implementation", async () => {
  const {root, put, beans, project} = await fixture();
  try {
    assert.deepEqual(await verifyProject(root, project), []);
    const ctx = new PipelineContext();
    (await loadContainer(root)).resolve<{execute(ctx: PipelineContext): void}>("Calculate").execute(ctx);
    assert.equal(ctx.get("total"), 12);
    await put("src/services/impl/algorithms/numbers/double.ts", "export function double(value: number) { return value * 3; }\n");
    await put("src/services/impl/math/rules/arithmetic.ts", "export {double} from '../../algorithms/numbers/double.js';\n");
    assert.deepEqual(validateLayout(root, beans), []);
    const next = new PipelineContext();
    (await loadContainer(root)).resolve<{execute(ctx: PipelineContext): void}>("Calculate").execute(next);
    assert.equal(next.get("total"), 18, "Reload must follow changed nested dependencies behind an unchanged public entry");
  } finally { await rm(root, {recursive: true, force: true}); }
});

test("manual registration accepts public exports and preserves legacy flat components", async () => {
  const {root, put, beans, project} = await fixture();
  try {
    await saveProject(root, {...project, beans: []});
    await addBean(root, beans[0]!); await addBean(root, beans[1]!);
    await put("src/services/legacy.ts", "export class Legacy { execute() {} }\n");
    await addBean(root, {...beans[1]!, id: "Legacy", file: "src/services/legacy.ts"});
    assert.equal(JSON.parse(await readFile(resolve(root, "aipod.json"), "utf8")).beans.length, 3);
  } finally { await rm(root, {recursive: true, force: true}); }
});

test("private cross-layer imports and reversed contract dependencies are rejected", async () => {
  const {root, put, beans} = await fixture();
  try {
    for (const path of ["src/services/impl/math/rules/arithmetic.ts", "src/pipelines/route.ts"]) {
      await put(path, "import {MemoryStore} from 'src/providers/impl/storage/memory.js';\n");
      assert.ok(validateLayout(root, beans).some((error) => error.includes("through public/")));
      await rm(resolve(root, path));
      if (path.includes("arithmetic")) await put(path, "export function double(value: number) { return value * 2; }\n");
    }
    await put("src/providers/contracts/storage.ts", "export {Store} from '../public/storage.js';\n");
    assert.ok(validateLayout(root, beans).some((error) => error.includes("contracts cannot depend")));
  } finally { await rm(root, {recursive: true, force: true}); }
});

test("helpers cannot hide another Service or runtime orchestration", async () => {
  const {root, put, beans} = await fixture();
  try {
    await put("src/services/impl/other.ts", "export class Other {execute() {}}\n");
    await put("src/services/public/other.ts", "export {Other} from '../impl/other.js';\n");
    beans.push({...beans[1]!, id: "Other", file: "src/services/public/other.ts"});
    await put("src/services/impl/math/rules/arithmetic.ts", "import {Other as Hidden} from '../../other.js'; export function double(value: number) {return value * 2;}\n");
    assert.ok(validateLayout(root, beans).some((error) => error.includes("another Service")));
    await put("src/services/impl/math/rules/arithmetic.ts", "import {PipelineRunner} from 'aipod-node'; export function double(value: number) {return value * 2;}\n");
    assert.ok(validateLayout(root, beans).some((error) => error.includes("orchestration")));
  } finally { await rm(root, {recursive: true, force: true}); }
});

test("private registrations, export cycles, missing names and public logic fail statically", async () => {
  const {root, put, beans} = await fixture();
  try {
    assert.throws(() => new SourceGraph(root).component({...beans[0]!, file: "src/providers/impl/storage/memory.ts", id: "MemoryStore"}), /Register components through public/);
    await put(beans[0]!.file, "export {Store} from './second.js';\n");
    await put("src/providers/public/second.ts", "export {Store} from './storage.js';\n");
    assert.ok(validateLayout(root, beans).some((error) => error.includes("Cyclic")));
    await put(beans[0]!.file, "export {Missing as Store} from '../impl/storage/memory.js';\n");
    assert.ok(validateLayout(root, beans).some((error) => error.includes("Cannot resolve export")));
    await put(beans[0]!.file, "throw Error('must not execute'); export {MemoryStore as Store} from '../impl/storage/memory.js';\n");
    assert.ok(validateLayout(root, beans).some((error) => error.includes("public/ contains only")));
    await put("src/services/impl/math/calculate.ts", "export class Calculator {}\n");
    assert.ok(validateLayout(root, beans).some((error) => error.includes("must implement execute")));
  } finally { await rm(root, {recursive: true, force: true}); }
});

test("public local re-exports work and symlink implementations fail", async () => {
  const {root, put, beans} = await fixture();
  try {
    await put(beans[0]!.file, "import {MemoryStore as Store} from '../impl/storage/memory.js'; export {Store};\n");
    assert.deepEqual(validateLayout(root, beans), []);
    await symlink(resolve(root, "src/providers/impl/storage/memory.ts"), resolve(root, "src/providers/impl/link.ts"));
    await put(beans[0]!.file, "export {MemoryStore as Store} from '../impl/link.js';\n");
    assert.ok(validateLayout(root, beans).some((error) => error.includes("symlink")));
  } finally { await rm(root, {recursive: true, force: true}); }
});

test("Agent can register public entries after writing and checking nested implementation", async t => {
  const {root, beans, project} = await fixture();
  try {
    await saveProject(root, {...project, beans: [beans[0]!]});
    const tools = await WorkspaceTools.create(root, "services");
    const ready = await tools.shell("printf backend-ready");
    if (ready.output.includes("sandbox_apply: Operation not permitted")) { t.skip("Outer sandbox prevents nested shell; rerun with local shell permission"); return; }
    assert.equal(ready.exitCode, 0, ready.output);
    const actions = [encodeSourceArtifact({path: "src/services/impl/math/rules/arithmetic.ts", content: "export function double(value: number) {return value + value;}\n"}),
      JSON.stringify({tool: "shell", command: 'node "$AIPOD_NODE_CLI" verify --project-root .'}),
      JSON.stringify({tool: "finish", summary: "Nested implementation checked", components: [beans[1]]})];
    await new ConstructionAgent(root, {async complete() {throw Error("unused");}, async completeText() {assert.ok(actions.length, "Agent should finish without repair"); return actions.shift()!;}}).runStage("services", "Expose Calculate via public");
    assert.equal(JSON.parse(await readFile(resolve(root, "aipod.json"), "utf8")).beans[1].file, beans[1]!.file);
  } finally { await rm(root, {recursive: true, force: true}); }
});

test("removing one component preserves the shared public file and other registrations", async t => {
  const {root, put, beans, project} = await fixture();
  try {
    await put("src/providers/impl/other.ts", "export class Other {}\n");
    const exports = "export {MemoryStore as Store} from '../impl/storage/memory.js';\n";
    await put(beans[0]!.file, exports + "export {Other} from '../impl/other.js';\n");
    await saveProject(root, {...project, beans: [...beans, {...beans[0]!, id: "Other"}]});
    const ready = await (await WorkspaceTools.create(root, "providers")).shell("printf backend-ready");
    if (ready.output.includes("sandbox_apply: Operation not permitted")) { t.skip("Outer sandbox prevents nested shell; rerun with local shell permission"); return; }
    assert.equal(ready.exitCode, 0, ready.output);
    const actions = [encodeSourceArtifact({path: beans[0]!.file, content: exports}),
      JSON.stringify({tool: "shell", command: `node --input-type=module -e 'const {typeCheckProject} = await import(process.env.AIPOD_NODE_MODULE); const errors = await typeCheckProject(process.cwd()); if(errors.length) throw Error(JSON.stringify(errors));'`}),
      JSON.stringify({tool: "finish", summary: "Removed unused Other export", remove: ["Other"]})];
    await new ConstructionAgent(root, {async complete() {throw Error("unused");}, async completeText() {assert.ok(actions.length); return actions.shift()!;}}).runStage("providers", "Remove unused Other provider");
    const current = JSON.parse(await readFile(resolve(root, "aipod.json"), "utf8")) as ProjectManifest;
    assert.deepEqual(current.beans.map((bean) => bean.id), ["Store", "Calculate"]);
    assert.equal(await readFile(resolve(root, beans[0]!.file), "utf8"), exports);
    assert.deepEqual(validateLayout(root, current.beans), []);
  } finally { await rm(root, {recursive: true, force: true}); }
});
