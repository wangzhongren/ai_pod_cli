import assert from "node:assert/strict";
import { access, mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  inspectProject,
  loadInterface,
  ModelRepository,
  runInterfaceLifecycle,
  runRoute,
  runVerificationCommand,
  smokeInterface,
  startStudio,
  typeCheckProject,
  verifyInterface,
} from "../src/index.js";

async function runnableProject(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "aipod-node-project-"));
  await mkdir(join(root, "src", "services"), { recursive: true });
  await mkdir(join(root, "src", "models"), { recursive: true });
  await mkdir(join(root, "src", "interfaces"), { recursive: true });
  await mkdir(join(root, "interfaces", "CalculatorCli"), { recursive: true });
  await writeFile(join(root, "config.json"), JSON.stringify({ increment: { amount: 2 } }));
  await writeFile(join(root, "src", "models", "calculation.ts"),
    "export interface Calculation { value: number }\n");
  await writeFile(join(root, "src", "services", "increment.ts"), `
export class IncrementService {
  constructor(private readonly dependencies: Record<string, any>) {}
  execute(context: { get(key: string): unknown }) {
    const amount = Number(this.dependencies.ConfigStore.get("increment.amount", 1));
    return { value: Number(context.get("value")) + amount };
  }
}
`);
  await writeFile(join(root, "src", "interfaces", "calculator-cli.ts"), `
export class CalculatorCliAdapter {
  constructor(private readonly runner: any) {}
  requiredRoutes() { return ["calculate"]; }
  start(payload: Record<string, unknown> = {}) { return this.runner.run("calculate", payload); }
}
`);
  await writeFile(
    join(root, "interfaces", "CalculatorCli", "metadata.json"),
    JSON.stringify({ name: "CalculatorCli", version: 1 }),
  );
  await writeFile(join(root, "aipod.json"), JSON.stringify({
    schemaVersion: 1,
    beans: [{
      id: "ConfigStore", category: "provider", file: "aipod:config-store",
      description: "built in", dependencies: [], inputs: {}, outputs: {},
    }, {
      id: "Calculation", category: "model", file: "src/models/calculation.ts",
      description: "value model", dependencies: [], inputs: {}, outputs: {},
    }, {
      id: "IncrementService", category: "service", file: "src/services/increment.ts",
      description: "increment", dependencies: ["ConfigStore"],
      inputs: { value: { type: "integer" } },
      outputs: { value: { type: "integer" } },
    }],
    routes: [{
      name: "calculate", description: "calculate", services: ["IncrementService"],
      execution: { mode: "sequential" }, file: "src/pipelines/calculate.ts",
    }],
    interfaces: [{
      name: "CalculatorCli", description: "CLI", file: "src/interfaces/calculator-cli.ts",
      route: "calculate", kind: "cli",
      artifacts: [{
        path: "interfaces/CalculatorCli/metadata.json", role: "metadata",
        format: "json", instruction: "metadata",
      }],
      lifecycle: {
        install: [
          process.execPath, "-e",
          "require('fs').writeFileSync(process.argv[1], 'installed')",
          "{projectRoot}/installed.txt",
        ],
      },
      permissions: ["filesystem_write"],
      verify: [{
        name: "node", command: [process.execPath, "--version"],
        timeoutMs: 30_000, required: true,
      }],
    }],
  }));
  await mkdir(join(root, "src", "pipelines"), { recursive: true });
  await writeFile(join(root, "src", "pipelines", "calculate.ts"), "export {};\n");
  return root;
}

test("project model, dynamic loader, trace, and Interface execute real generated code", async () => {
  const root = await runnableProject();
  try {
    const model = await inspectProject(root) as {
      validation: { valid: boolean };
      summary: { services: number; routes: number; interfaces: number };
    };
    assert.equal(model.validation.valid, true);
    assert.deepEqual(model.summary, {
      models: 1, providers: 1, services: 1, routes: 1, interfaces: 1,
    });

    const trace = await runRoute(root, "calculate", { value: 3 });
    assert.equal(trace.status, "success");
    assert.equal(
      ((trace.context as { data: { value: number } }).data.value),
      5,
    );
    await access(String(trace.tracePath));

    const smoke = await smokeInterface(root, "CalculatorCli");
    assert.equal(smoke.status, "passed");
    const { adapter } = await loadInterface(root, "CalculatorCli");
    const execution = await adapter.start({ value: 8 }) as {
      context: { get(key: string): unknown };
    };
    assert.equal(execution.context.get("value"), 10);
    assert.equal((await verifyInterface(root, "CalculatorCli")).status, "passed");
    assert.equal((await runInterfaceLifecycle(root, "CalculatorCli", "install")).status, "passed");
    assert.equal(await readFile(join(root, "installed.txt"), "utf8"), "installed");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("semantic checker catches cross-file and assignment errors missed by transpilation", async () => {
  const root = await mkdtemp(join(tmpdir(), "aipod-node-types-"));
  await mkdir(join(root, "src"), { recursive: true });
  await writeFile(join(root, "src", "types.ts"), "export interface Item { id: string }\n");
  await writeFile(join(root, "src", "broken.ts"), `
import type { Missing } from "./types.js";
const count: string = 42;
export const value: Missing = { id: count };
`);
  try {
    const diagnostics = await typeCheckProject(root);
    assert.ok(diagnostics.some((item) => item.code === 2305));
    assert.ok(diagnostics.some((item) => item.code === 2322));
    assert.ok(diagnostics.some((item) => item.file === "src/broken.ts" && item.line));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("ModelRepository persists, finds, and deletes typed records atomically", async () => {
  const root = await mkdtemp(join(tmpdir(), "aipod-node-repository-"));
  const repository = new ModelRepository(root);
  try {
    await Promise.all([
      repository.save("tasks", { id: "a", title: "First", done: false }),
      repository.save("tasks", { id: "b", title: "Second", done: true }),
    ]);
    assert.equal((await repository.get("tasks", "a"))?.title, "First");
    assert.equal((await repository.list("tasks")).length, 2);
    assert.deepEqual(
      (await repository.find<{ id: string; done: boolean }>("tasks", { done: true }))
        .map((item) => item.id),
      ["b"],
    );
    assert.equal(await repository.delete("tasks", "a"), true);
    assert.equal(await repository.get("tasks", "a"), undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("verification executes an exact command and persists redacted evidence", async () => {
  const root = await mkdtemp(join(tmpdir(), "aipod-node-verify-"));
  try {
    const evidence = await runVerificationCommand(root, [
      process.execPath,
      "-e",
      "console.log('token=sk-exampleSECRET123')",
    ]);
    assert.equal(evidence.status, "passed");
    assert.equal(evidence.stdout.includes("sk-exampleSECRET123"), false);
    const persisted = await readFile(join(root, ".aipod", "verification.json"), "utf8");
    assert.equal(persisted.includes("sk-exampleSECRET123"), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Studio serves authenticated project, source, and route APIs", async (context) => {
  const root = await runnableProject();
  let studio: Awaited<ReturnType<typeof startStudio>>;
  try {
    studio = await startStudio(root);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EPERM") {
      context.skip("sandbox does not permit loopback listeners");
      await rm(root, { recursive: true, force: true });
      return;
    }
    throw error;
  }
  try {
    const studioUrl = new URL(studio.url);
    const token = studioUrl.searchParams.get("token") ?? "";
    const base = `${studioUrl.protocol}//${studioUrl.host}`;
    const headers = { "x-aipod-token": token, "content-type": "application/json" };

    const projectResponse = await fetch(`${base}/api/project`, { headers });
    assert.equal(projectResponse.status, 200);
    const project = await projectResponse.json() as { validation: { valid: boolean } };
    assert.equal(project.validation.valid, true);

    const sourceResponse = await fetch(
      `${base}/api/source?path=${encodeURIComponent("src/services/increment.ts")}`,
      { headers },
    );
    assert.match((await sourceResponse.json() as { source: string }).source, /IncrementService/);

    const runResponse = await fetch(`${base}/api/run`, {
      method: "POST", headers, body: JSON.stringify({ route: "calculate", params: { value: 5 } }),
    });
    const trace = await runResponse.json() as { status: string; context: { data: { value: number } } };
    assert.equal(trace.status, "success");
    assert.equal(trace.context.data.value, 7);

    const forbidden = await fetch(`${base}/api/source?path=${encodeURIComponent("../outside")}`, { headers });
    assert.equal(forbidden.status, 400);
  } finally {
    await studio.close();
    await rm(root, { recursive: true, force: true });
  }
});
