import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { SDK_EXAMPLES, SDK_ROLE_SECTIONS } from "../src/agent/sdk-reference.js";
import { WorkspaceAgent, WorkspaceTools, type Owner } from "../src/agent/workspace.js";
import type { ModelClient } from "../src/agent/types.js";
import { compiledSourcePath } from "../src/loader.js";
import {
  loadContainer, loadRunner, ModelRepository, PipelineContext, sdkReference, validateContract,
} from "../src/index.js";

test("each role keeps its bundled SDK reference after conversation history is trimmed", async () => {
  const root = await mkdtemp(resolve(tmpdir(), "aipod-sdk-prompt-"));
  try {
    for (const role of Object.keys(SDK_ROLE_SECTIONS) as Owner[]) {
      const tools = await WorkspaceTools.create(root, role);
      let observation = 0;
      tools.execute = async () => ({ content: `observation-${observation++}:` + "x".repeat(41000) });
      const calls: { system: string; prompt: string }[] = [];
      const client: ModelClient = {
        async complete() { throw new Error("Text instructions expected"); },
        async completeText(system, prompt) {
          calls.push({ system, prompt });
          return calls.length <= 4 ? "<read><path>README.md</path></read>"
            : "<finish><summary>Checked</summary></finish>";
        },
      };
      await new WorkspaceAgent(client, tools, () => false, 5, undefined, null).run(
        "Check example", {}, async () => { throw new Error("No change request expected"); },
        async action => action,
      );
      for (const { system } of calls) {
        assert.ok(system.startsWith(`WORKSPACE_AGENT:${role}\n`));
        assert.ok(system.includes(sdkReference(role)));
        assert.match(system, /AIPod Instruction Set/);
      }
      assert.ok(!calls.at(-1)!.prompt.includes("observation-0:"));
      assert.ok(calls.at(-1)!.prompt.includes("observation-3:"));
    }
    assert.ok(sdkReference("models").includes(SDK_EXAMPLES.model));
    assert.ok(!sdkReference("models").includes("INTERFACE SDK"));
    assert.ok(!sdkReference("interfaces").includes("CONFIGURATION, DEPENDENCIES AND STORAGE"));
    assert.ok(sdkReference("providers").includes(SDK_EXAMPLES.repository));
    assert.ok(sdkReference("services").includes(SDK_EXAMPLES.service));
    assert.ok(sdkReference("pipelines").includes(SDK_EXAMPLES.pipeline));
    for (const role of ["interfaces", "pod"] as const) {
      assert.ok(sdkReference(role).includes(SDK_EXAMPLES.interface_check));
      assert.match(sdkReference(role), /long-running start/);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("the exact SDK prompt examples compile and run as a complete project", async () => {
  const root = await mkdtemp(resolve(tmpdir(), "aipod-sdk-examples-"));
  const write = async (file: string, source: string) => {
    await mkdir(dirname(resolve(root, file)), { recursive: true });
    await writeFile(resolve(root, file), source);
  };
  try {
    const files: Record<keyof typeof SDK_EXAMPLES, string> = {
      model: "src/models/value.ts",
      provider: "src/providers/impl/example.ts",
      service: "src/services/impl/example.ts",
      pipeline: "src/pipelines/example.ts",
      adapter: "src/interfaces/example.ts",
      contracts: "src/checks/contracts.ts",
      repository: "src/checks/repository.ts",
      runner: "src/checks/runner.ts",
      interface_check: "src/checks/interface-check.ts",
    };
    for (const name of Object.keys(files) as (keyof typeof SDK_EXAMPLES)[]) {
      await write(files[name], SDK_EXAMPLES[name]);
    }
    await write("src/providers/public/example.ts", 'export { ConfiguredProvider } from "../impl/example.js";\n');
    await write("src/services/public/example.ts", 'export { ExampleService } from "../impl/example.js";\n');
    await write("package.json", '{"private":true,"type":"module"}');
    await write("config.json", JSON.stringify({ example: { increment: 2 } }));
    await write("aipod.json", JSON.stringify({
      schemaVersion: 1,
      beans: [
        { id: "ExampleValue", category: "model", file: files.model, description: "value", dependencies: [], inputs: {}, outputs: {} },
        { id: "ConfigStore", category: "provider", file: "aipod:config-store", description: "config", dependencies: [], inputs: {}, outputs: {} },
        { id: "ConfiguredProvider", category: "provider", file: "src/providers/public/example.ts", description: "add", dependencies: ["ConfigStore"], inputs: {}, outputs: {} },
        { id: "ExampleService", category: "service", file: "src/services/public/example.ts", description: "add", dependencies: ["ConfiguredProvider"],
          inputs: { value: { type: "integer", required: false } }, outputs: { answer: { type: "integer" } } },
      ],
      routes: [{ name: "example", description: "add", file: files.pipeline, services: ["ExampleService"], execution: { mode: "sequential" } }],
      interfaces: [{ name: "Example", description: "CLI", file: files.adapter, route: "example", kind: "cli" }],
    }));
    // loadContainer runs the real TypeScript semantic check before loading the examples.
    const container = await loadContainer(root);
    const example = (name: keyof typeof files) => import(pathToFileURL(compiledSourcePath(root, files[name])).href);
    const { inputs, outputs } = await example("contracts");
    assert.deepEqual(validateContract({}, inputs), []);
    assert.ok(validateContract({ value: null }, inputs).length > 0);
    assert.ok(validateContract({}, outputs).length > 0);
    assert.equal(container.resolve("ConfiguredProvider"), container.resolve("ConfiguredProvider"));
    const { repositoryExample } = await example("repository");
    const repo = new ModelRepository(root);
    assert.deepEqual(await repositoryExample(repo), {
      found: { id: "one", value: 3 }, matches: [{ id: "one", value: 3 }], deleted: true,
    });
    assert.equal(await repo.get("examples", "one"), undefined);
    const { createRoute } = await example("pipeline");
    const route = createRoute(container);
    assert.equal(route.name, "example");
    const ctx = new PipelineContext({ value: 3 }, { data: { value: 4 } });
    const direct = await route.pipeline.execute(ctx);
    assert.equal(direct.status, "success");
    assert.deepEqual(direct.output, { answer: 6 });
    assert.equal(ctx.get("answer"), 6);

    const { runExample } = await example("runner");
    const actual = await runExample(root);
    assert.deepEqual(actual.output, { answer: 5 });
    assert.equal(actual.context.get("answer"), 5);
    const runner = await loadRunner(root);
    const defaultRun = await runner.run("example");
    assert.equal(defaultRun.result.status, "success");
    if (defaultRun.result.status === "success") assert.deepEqual(defaultRun.result.output, { answer: 3 });
    const invalid = await runner.run("example", { value: "bad" });
    assert.equal(invalid.result.status, "failure");
    if (invalid.result.status === "failure") assert.equal(typeof invalid.result.error.message, "string");

    const { checkInterface } = await example("interface_check");
    const { adapter, context } = await checkInterface(root);
    const entry = await adapter.start({ value: 4 });
    assert.equal(entry.result.status, "success");
    assert.deepEqual(entry.result.output, { answer: 6 });
    assert.deepEqual(await context.routeNames(), ["example"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
