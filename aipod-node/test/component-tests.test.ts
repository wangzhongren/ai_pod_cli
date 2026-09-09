import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { componentTestRecords, digest, encodeSourceArtifact, prepareComponentTests, repairArtifact, runComponentTests,
  validateStagePlan, type ComponentPlan, type ComponentTestRecord, type ProjectManifest } from "../src/index.js";
import { generateArtifacts, commitArtifacts } from "../src/agent/artifacts.js";

const serviceSource = `import {failure, success, type ModelRepository, type PipelineContext} from "aipod-node";
export class CreateTicket {
  constructor(private readonly dependencies: {ModelRepository: ModelRepository}) {}
  async execute(ctx: PipelineContext) {
    const actor = await this.dependencies.ModelRepository.get("users", String(ctx.get("actor_id")));
    if (actor?.role !== "admin") return failure("Only an administrator may create a ticket", {code:"forbidden"});
    await this.dependencies.ModelRepository.save("tickets", {id:"ticket-1", title:ctx.get("title")});
    return success({created:true});
  }
}`;
const project: ProjectManifest = { schemaVersion: 1, beans: [{ id: "ModelRepository", category: "provider", file: "aipod:model-repository", description: "isolated rows", dependencies: [], inputs: {}, outputs: {} },
  { id: "CreateTicket", category: "service", file: "src/services/create-ticket.ts", description: "Administrators create tickets", dependencies: ["ModelRepository"],
    inputs: { actor_id: { type: "string" }, title: { type: "string" } }, outputs: { created: { type: "boolean" } } },
], routes: [], interfaces: [] };
const authorizationTests = `import {defineComponentTests} from "aipod-node";
export default defineComponentTests([
  {name:"test_admin_creates",async run(sandbox,assert){
    assert.equal(await sandbox.count("users"),0);
    await sandbox.seed("users",[{id:"admin",role:"admin"}]);
    const {result}=await sandbox.run("CreateTicket",{actor_id:"admin",title:"explicit title"});
    assert.equal(result.status,"success"); assert.equal(await sandbox.count("tickets"),1);
    assert.equal((await sandbox.rows("tickets"))[0]!.title,"explicit title");
  }},
  {name:"test_viewer_denied",async run(sandbox,assert){
    await sandbox.seed("users",[{id:"viewer",role:"viewer"}]);
    const {result}=await sandbox.run("CreateTicket",{actor_id:"viewer",title:"denied"});
    assert.equal(result.status,"failure"); if(result.status==="failure")assert.equal(result.error.code,"forbidden");
    assert.equal(await sandbox.count("tickets"),0); assert.equal((await sandbox.rows("users"))[0]!.role,"viewer");
  }},
  {name:"test_missing_user_denied",async run(sandbox,assert){
    const {result}=await sandbox.run("CreateTicket",{actor_id:"missing",title:"denied"});
    assert.equal(result.status,"failure"); assert.deepEqual(await sandbox.snapshot(),{});
  }}
]);`;

async function fixture(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "aipod-test-sdk-"));
  await mkdir(join(root, "src/services"), { recursive: true });
  await mkdir(join(root, "tests/components"), { recursive: true });
  await writeFile(join(root, "aipod.json"), JSON.stringify(project));
  await writeFile(join(root, "src/services/create-ticket.ts"), serviceSource);
  await mkdir(join(root, ".aipod"), { recursive: true });
  await writeFile(join(root, ".aipod/data.json"), JSON.stringify({ users: { production: { id: "production", role: "admin" } } }));
  await writeFile(join(root, "config.json"), JSON.stringify({ productionSecret: "must not be copied" }));
  return root;
}
async function record(root: string, source: string, names: string[], id = "CreateTicket", stage: ComponentTestRecord["stage"] = "services"): Promise<ComponentTestRecord> {
  const path = `tests/components/${id}.test.ts`;
  await writeFile(join(root, path), source);
  return { id, stage, path, sha256: digest(source), planHash: "fixture", scenarios: names };
}

test("real authorization tests seed isolated rows and preserve denied users without inventing privileges", async () => {
  const root = await fixture();
  try {
    const before = await readFile(join(root, ".aipod/data.json"), "utf8");
    const tests = await record(root, authorizationTests, ["test_admin_creates", "test_viewer_denied", "test_missing_user_denied"]);
    assert.deepEqual(await runComponentTests(root, project, tests), []);
    assert.equal(await readFile(join(root, ".aipod/data.json"), "utf8"), before);
    assert.equal(await readFile(join(root, "src/services/create-ticket.ts"), "utf8"), serviceSource);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("empty tests, no target call, no assertion, swallowed assertions and mocked targets cannot pass", async () => {
  const root = await fixture();
  try {
    for (const [body, expected] of [
      ['if(false){await sandbox.run("CreateTicket",{});}assert.equal(await sandbox.count("users"),0);', /no real tested-component call/],
      ['await sandbox.run("CreateTicket",{actor_id:"missing",title:"x"});if(false){assert.equal(await sandbox.count("users"),0);}', /no executed assertion/],
      ['const execution=await sandbox.run("CreateTicket",{actor_id:"missing",title:"x"}); try{assert.equal(execution.result.status,"success");}catch{}', /swallowed a failed assertion/],
      ['await sandbox.run("CreateTicket",{actor_id:"missing",title:"x"});assert.ok(true);', /constant-only assertions/],
    ] as const) {
      const code = `import {defineComponentTests} from "aipod-node"; export default defineComponentTests([{name:"test_bad",async run(sandbox,assert){${body}}}]);`;
      assert.match((await runComponentTests(root, project, await record(root, code, ["test_bad"]))).join("\n"), expected);
    }
    const empty = 'import {defineComponentTests} from "aipod-node";export default defineComponentTests([]);';
    assert.match((await runComponentTests(root, project, await record(root, empty, []))).join("\n"), /nonempty literal case array/);
    const mock = 'import {defineComponentTests} from "aipod-node";export default defineComponentTests([{name:"test_bad",providers:{CreateTicket:{execute(){return {created:true};}}},async run(sandbox,assert){const run=await sandbox.run("CreateTicket",{});assert.ok(run);}}]);';
    assert.match((await runComponentTests(root, project, await record(root, mock, ["test_bad"]))).join("\n"), /cannot be replaced/);
    const storage = mock.replace("CreateTicket:{execute", "ModelRepository:{execute");
    assert.match((await runComponentTests(root, project, await record(root, storage, ["test_bad"]))).join("\n"), /built-in storage\/config cannot be replaced/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("Model data is checked against the actual generated TypeScript declaration", async () => {
  const root = await fixture();
  try {
    await mkdir(join(root, "src/models"));
    await writeFile(join(root, "src/models/user.ts"), "export interface User { id: string; enabled: boolean }\n");
    const models: ProjectManifest = { schemaVersion: 1, beans: [{ id: "User", file: "src/models/user.ts", category: "model", description: "data", dependencies: [], inputs: {}, outputs: {} }], routes: [], interfaces: [] };
    const code = `import {defineComponentTests} from "aipod-node";export default defineComponentTests([{name:"test_model",async run(sandbox,assert){
      const value=await sandbox.model("User",{id:"u",enabled:true});assert.equal(value.id,"u");
      await assert.rejects(()=>sandbox.model("User",{id:123,enabled:true}),/not assignable/);
    }}]);`;
    assert.deepEqual(await runComponentTests(root, models, await record(root, code, ["test_model"], "User", "models")), []);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("tests freeze before implementation; failed candidates revise only source and reuse reruns the frozen test", async () => {
  const root = await fixture();
  try {
    await rm(join(root, "src/services/create-ticket.ts"));
    const bean = project.beans[1]!;
    const plan: ComponentPlan = { ...bean, file: "create-ticket.ts", tests: [
      { name: "test_admin_creates", requirement: "An explicitly seeded admin creates one ticket" },
      { name: "test_viewer_denied", requirement: "An explicitly seeded viewer stays a viewer and cannot create" },
      { name: "test_missing_user_denied", requirement: "A missing user is denied without rows being synthesized" },
    ] };
    const order: string[] = [];
    let implementations = 0;
    const client = { complete: async () => { throw new Error("source and tests use XML"); }, completeText: async (system: string) => {
      const prefix = system.split("\n")[0]!; order.push(prefix);
      const path = JSON.parse(/The path must be exactly ("(?:\\.|[^"\\])*")\./.exec(system)![1]!);
      if (system.startsWith("GENERATE_COMPONENT_TESTS:")) return encodeSourceArtifact({ path, content: authorizationTests });
      implementations += 1;
      assert.equal((await componentTestRecords(root)).length, 1, "test must be frozen before any implementation is generated");
      return encodeSourceArtifact({ path, content: implementations === 1 ? serviceSource.replace('actor?.role !== "admin"', 'false') : serviceSource });
    } };
    const before = { ...project, beans: [project.beans[0]!] };
    const artifacts = await generateArtifacts(client, "services", { summary: "test first", components: [plan] }, before, root);
    assert.match(order[0]!, /^GENERATE_COMPONENT_TESTS:/); assert.equal(order.filter((item) => item.startsWith("GENERATE_COMPONENT_TESTS:")).length, 1);
    assert.equal(implementations, 2);
    await commitArtifacts(root, "services", artifacts);
    const frozen = (await componentTestRecords(root))[0]!;
    assert.deepEqual(await runComponentTests(root, project, frozen), []);
    assert.equal((await prepareComponentTests({ complete: async () => { throw new Error("must reuse"); } }, root, "services", plan, before)).sha256, frozen.sha256);
    await assert.rejects(repairArtifact(client, root, frozen.path, ["test fails"]), /cannot modify frozen/);
    await writeFile(join(root, frozen.path), authorizationTests.replace('role:"viewer"', 'role:"admin"'));
    assert.match((await runComponentTests(root, project, frozen)).join("\n"), /frozen component test was modified/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("invalid SDK tests are corrected before source generation and plans without scenarios are rejected", async () => {
  const root = await fixture();
  try {
    assert.match(validateStagePlan("services", { summary: "missing", components: [{ ...project.beans[1]!, file: "create-ticket.ts" }] }, project).join("; "), /needs named test_/);
    const plan = { ...project.beans[1]!, file: "create-ticket.ts", tests: [{ name: "test_bad", requirement: "Explicit setup" }] };
    let calls = 0;
    await assert.rejects(prepareComponentTests({ complete: async () => ({}), completeText: async (system) => {
      calls += 1;
      const path = JSON.parse(/The path must be exactly ("(?:\\.|[^"\\])*")\./.exec(system)![1]!);
      return encodeSourceArtifact({ path, content: 'import {defineComponentTests} from "aipod-node";export default defineComponentTests([{name:"test_bad",async run(sandbox,assert){await sandbox.seed("users",{id:"x"});const value=await sandbox.run("CreateTicket",{});assert.equal(value.result.status,"success");}}]);' });
    } }, root, "services", plan, project), /test generation failed/);
    assert.equal(calls, 3); assert.deepEqual(await componentTestRecords(root), []);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("invalid fixture setup stops implementation regeneration instead of weakening business rules", async () => {
  const root = await fixture();
  try {
    const plan: ComponentPlan = { ...project.beans[1]!, file: "create-ticket.ts", tests: [{ name: "test_setup", requirement: "Create with an explicit valid user fixture" }] };
    let implementations = 0;
    await assert.rejects(generateArtifacts({ complete: async () => ({}), completeText: async (system) => {
      const path = JSON.parse(/The path must be exactly ("(?:\\.|[^"\\])*")\./.exec(system)![1]!);
      if (system.startsWith("GENERATE_COMPONENT_TESTS:")) return encodeSourceArtifact({ path, content: 'import {defineComponentTests} from "aipod-node";export default defineComponentTests([{name:"test_setup",async run(sandbox,assert){await sandbox.seed("users",[{}]);const result=await sandbox.run("CreateTicket",{actor_id:"x",title:"x"});assert.equal(result.result.status,"success");}}]);' });
      implementations += 1; return encodeSourceArtifact({ path, content: serviceSource });
    } }, "services", { summary: "bad fixture", components: [plan] }, { ...project, beans: [project.beans[0]!] }, root), /implementation repair is forbidden/);
    assert.equal(implementations, 1);
    assert.equal(await readFile(join(root, "src/services/create-ticket.ts"), "utf8"), serviceSource);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("file fixtures and explicit configuration reach real Providers without copying production settings", async () => {
  const root = await fixture();
  try {
    await mkdir(join(root, "src/providers"));
    await writeFile(join(root, "src/providers/reader.ts"), 'import {readFileSync} from "node:fs";import type {ConfigStore} from "aipod-node";export class Reader {constructor(private readonly deps:{ConfigStore:ConfigStore}){} read(path:string){return readFileSync(path,"utf8");} settings(){return this.deps.ConfigStore.values();}}');
    const providers: ProjectManifest = { schemaVersion: 1, beans: [{ id: "ConfigStore", category: "provider", file: "aipod:config-store", description: "config", dependencies: [], inputs: {}, outputs: {} },
      { id: "Reader", category: "provider", file: "src/providers/reader.ts", description: "provider", dependencies: ["ConfigStore"], inputs: {}, outputs: {} }], routes: [], interfaces: [] };
    const code = 'import {defineComponentTests} from "aipod-node";export default defineComponentTests([{name:"test_file",config:{format:"text"},async run(sandbox,assert){const file=await sandbox.writeFile("fixtures/input.txt","hello");assert.equal(await sandbox.callProvider("Reader","read",[file]),"hello");assert.deepEqual(await sandbox.callProvider("Reader","settings"),{format:"text"});}},{name:"test_default_config",async run(sandbox,assert){assert.deepEqual(await sandbox.callProvider("Reader","settings"),{});}}]);';
    assert.deepEqual(await runComponentTests(root, providers, await record(root, code, ["test_file", "test_default_config"], "Reader", "providers")), []);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("a declared external dependency may be doubled while the actual Service executes", async () => {
  const root = await fixture();
  try {
    await mkdir(join(root, "src/providers"));
    await writeFile(join(root, "src/providers/remote.ts"), 'throw new Error("Real remote dependency must not be imported during this test");export class RemoteProvider {read(){return "remote";}}');
    await writeFile(join(root, "src/services/uses-remote.ts"), 'export class UsesRemote {constructor(private readonly deps:{RemoteProvider:{read():string}}){}execute(){return {value:this.deps.RemoteProvider.read()};}}');
    const doubled: ProjectManifest = { schemaVersion: 1, beans: [
      { id: "RemoteProvider", category: "provider", file: "src/providers/remote.ts", description: "remote", dependencies: [], inputs: {}, outputs: {} },
      { id: "UsesRemote", category: "service", file: "src/services/uses-remote.ts", description: "read", dependencies: ["RemoteProvider"], inputs: {}, outputs: { value: { type: "string" } } },
    ], routes: [], interfaces: [] };
    const code = 'import {defineComponentTests} from "aipod-node";export default defineComponentTests([{name:"test_dependency",providers:{RemoteProvider:{read(){return "fixture";}}},async run(sandbox,assert){const execution=await sandbox.run("UsesRemote",{});assert.equal(execution.result.status,"success");if(execution.result.status==="success")assert.equal(execution.result.output.value,"fixture");}}]);';
    assert.deepEqual(await runComponentTests(root, doubled, await record(root, code, ["test_dependency"], "UsesRemote")), []);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("network access, candidate source rewriting and infinite execution are blocked", async () => {
  const root = await fixture();
  try {
    await mkdir(join(root, "src/providers"));
    const providers: ProjectManifest = { schemaVersion: 1, beans: [{ id: "Reader", category: "provider", file: "src/providers/reader.ts", description: "provider", dependencies: [], inputs: {}, outputs: {} }], routes: [], interfaces: [] };
    const denied = 'import {defineComponentTests} from "aipod-node";export default defineComponentTests([{name:"test_network",async run(sandbox,assert){await assert.rejects(()=>sandbox.callProvider("Reader","read"),/External connections are disabled/);}}]);';
    await writeFile(join(root, "src/providers/reader.ts"), 'export class Reader { async read(){return fetch("https://example.invalid");} }');
    assert.deepEqual(await runComponentTests(root, providers, await record(root, denied, ["test_network"], "Reader", "providers")), []);
    await writeFile(join(root, "src/providers/reader.ts"), 'import {writeFileSync} from "node:fs";export class Reader { read(){writeFileSync("src/providers/reader.ts","export class Reader {}");return true;} }');
    const mutate = 'import {defineComponentTests} from "aipod-node";export default defineComponentTests([{name:"test_mutation",async run(sandbox,assert){assert.equal(await sandbox.callProvider("Reader","read"),true);}}]);';
    assert.match((await runComponentTests(root, providers, await record(root, mutate, ["test_mutation"], "Reader", "providers"))).join("\n"), /modified candidate sources/);
    await writeFile(join(root, "src/providers/reader.ts"), 'export class Reader { read(){while(true){} } }');
    assert.match((await runComponentTests(root, providers, await record(root, mutate, ["test_mutation"], "Reader", "providers"), [], 1200)).join("\n"), /timed out/);
    await writeFile(join(root, "src/providers/reader.ts"), 'export class Reader { read(){process.exit(0);} }');
    assert.match((await runComponentTests(root, providers, await record(root, mutate, ["test_mutation"], "Reader", "providers"))).join("\n"), /missing or invalid completed-test evidence/);
    await writeFile(join(root, "src/providers/reader.ts"), 'import {writeFileSync} from "node:fs";export class Reader { read(){writeFileSync("aipod.json","{}");return true;} }');
    assert.match((await runComponentTests(root, providers, await record(root, mutate, ["test_mutation"], "Reader", "providers"))).join("\n"), /modified candidate sources/);
  } finally { await rm(root, { recursive: true, force: true }); }
});
