import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";
import { revisionScope, validateRevisionPlan } from "../src/agent/revision.js";
import type { ProjectManifest } from "../src/agent/project.js";

test("revision graph follows DI, type-only and transitive imports, routes and Interfaces", async () => {
  const root = await mkdtemp(join(tmpdir(), "aipod-scope-"));
  const project: ProjectManifest = {
    schemaVersion: 1,
    beans: [
      { id: "Order", category: "model", file: "src/models/order.ts", dependencies: [], description: "", inputs: {}, outputs: {} },
      { id: "Clock", category: "provider", file: "src/providers/clock.ts", dependencies: [], description: "", inputs: {}, outputs: {} },
      { id: "Price", category: "service", file: "src/services/price.ts", dependencies: ["Clock"], description: "", inputs: {}, outputs: {} },
      { id: "Other", category: "service", file: "src/services/other.ts", dependencies: [], description: "", inputs: {}, outputs: {} },
    ],
    routes: [
      { name: "price", file: "src/pipelines/price.ts", description: "", services: ["Price"], execution: { mode: "sequential" } },
      { name: "other", file: "src/pipelines/other.ts", description: "", services: ["Other"], execution: { mode: "sequential" } },
    ],
    interfaces: [{ name: "PriceCli", file: "src/interfaces/price.ts", description: "", route: "price", kind: "cli" }],
  };
  const files: Record<string, string> = Object.fromEntries([
    ...project.beans.map((item) => item.file), ...project.routes.map((item) => item.file),
    ...project.interfaces.map((item) => item.file),
  ].map((file) => [file, "export {};\n"]));
  files["src/models/order.ts"] = "export interface Order { id: string }";
  files["src/helpers/order.ts"] = 'export type { Order } from "../models/order.js";';
  files["src/services/price.ts"] = 'import type { Order } from "../helpers/order.js"; export type Input = Order;';
  try {
    for (const [file, source] of Object.entries(files)) {
      await mkdir(dirname(join(root, file)), { recursive: true });
      await writeFile(join(root, file), source);
    }
    assert.deepEqual(await revisionScope(root, project, "models", ["Order"]), {
      models: ["Order"], providers: [], services: ["Price"], pipelines: ["price"], interfaces: ["PriceCli"],
    });
    assert.deepEqual(await revisionScope(root, project, "providers", ["Clock"]), {
      models: [], providers: ["Clock"], services: ["Price"], pipelines: ["price"], interfaces: ["PriceCli"],
    });
    assert.equal(await revisionScope(root, project, "services", ["NewService"]), undefined);
    assert.equal(await revisionScope(root, project, "services", []), undefined);
    await writeFile(join(root, "src/services/price.ts"), "const path = './unknown.js'; void import(path);");
    assert.equal(await revisionScope(root, project, "models", ["Order"]), undefined);
    assert.deepEqual(validateRevisionPlan("pipelines", { summary: "", routes: [] }, ["price"]).length, 1);
    assert.equal(await readFile(join(root, "src/services/other.ts"), "utf8"), "export {};\n");
  } finally { await rm(root, { recursive: true, force: true }); }
});
