import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  ConstructionAgent,
  applyCodePatches,
  loadState,
  repairArtifact,
  type ModelClient,
} from "../src/index.js";

class FakeClient implements ModelClient {
  readonly systems: string[] = [];
  serviceGenerations = 0;

  async complete(system: string): Promise<Record<string, unknown>> {
    this.systems.push(system);
    if (system.startsWith("CLASSIFY_REVISION_STAGE")) {
      return { stage: "interfaces", summary: "Presentation-only change" };
    }
    if (system.startsWith("PLAN_STAGE:models")) {
      return {
        summary: "User model",
        components: [{
          id: "User", file: "user.ts", description: "User data",
          dependencies: [], inputs: {}, outputs: {},
        }],
      };
    }
    if (system.startsWith("PLAN_STAGE:providers")) {
      return {
        summary: "Clock provider",
        components: [{
          id: "Clock", file: "clock.ts", description: "Current time",
          dependencies: [], inputs: {}, outputs: {},
        }],
      };
    }
    if (system.startsWith("PLAN_STAGE:services")) {
      return {
        summary: "Greeting service",
        components: [{
          id: "GreetingService", file: "greeting-service.ts",
          description: "Build a greeting", dependencies: ["Clock"],
          inputs: { name: { type: "string" } },
          outputs: { greeting: { type: "string" } },
        }],
      };
    }
    if (system.startsWith("PLAN_STAGE:pipelines")) {
      return {
        summary: "Greeting route",
        routes: [{
          name: "greet", description: "Greet one user",
          services: ["GreetingService"], execution: { mode: "sequential" },
        }],
      };
    }
    if (system.startsWith("PLAN_STAGE:interfaces")) {
      return {
        summary: "CLI adapter",
        interfaces: [{
          name: "GreetingCli", file: "greeting-cli.ts", description: "CLI",
          route: "greet", kind: "cli",
          artifacts: [{
            path: "interfaces/GreetingCli/metadata.json", role: "metadata",
            format: "json", instruction: "metadata",
          }],
          lifecycle: {}, permissions: [], verify: [],
        }],
      };
    }
    if (system.startsWith("GENERATE_COMPONENT:models:User")) {
      return { content: "export interface User { id: string; name: string }\n" };
    }
    if (system.startsWith("GENERATE_COMPONENT:providers:Clock")) {
      return { content: "export class Clock { now(): Date { return new Date(); } }\n" };
    }
    if (system.startsWith("GENERATE_COMPONENT:services:GreetingService")) {
      this.serviceGenerations += 1;
      if (this.serviceGenerations === 1) {
        return {
          content: 'import { HiddenService } from "./services/hidden.js"; export class GreetingService { execute() { return new HiddenService(); } }',
        };
      }
      return {
        content: "export class GreetingService { execute(context: { get(key: string): unknown }) { return { greeting: `Hello ${String(context.get('name'))}` }; } }\n",
      };
    }
    if (system.startsWith("GENERATE_INTERFACE_ARTIFACT:GreetingCli:")) {
      return { content: '{"name":"GreetingCli","version":1}\n' };
    }
    throw new Error(`Unexpected model call: ${system.slice(0, 80)}`);
  }
}

async function projectRoot(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), "aipod-node-agent-"));
  await writeFile(join(root, "aipod.json"), JSON.stringify({
    schemaVersion: 1, beans: [], routes: [], interfaces: [],
  }));
  return root;
}

test("construction agent builds five stages and resumes without model calls", async () => {
  const root = await projectRoot();
  try {
    const client = new FakeClient();
    const events: string[] = [];
    const agent = new ConstructionAgent(root, client, (event) => events.push(`${event.stage}:${event.action}`));

    const state = await agent.run("Build a greeting CLI");
    const project = JSON.parse(await readFile(join(root, "aipod.json"), "utf8")) as {
      beans: { id: string; file: string }[];
      routes: { name: string }[];
      interfaces: { name: string }[];
    };

    assert.equal(state.status, "complete");
    assert.equal(state.verification.status, "passed");
    assert.deepEqual(project.beans.map((bean) => bean.id), ["User", "Clock", "GreetingService"]);
    assert.equal(project.beans[2]?.file, "src/services/greeting-service.ts");
    assert.equal(project.routes[0]?.name, "greet");
    assert.equal(project.interfaces[0]?.name, "GreetingCli");
    assert.match(
      await readFile(join(root, "interfaces", "GreetingCli", "metadata.json"), "utf8"),
      /GreetingCli/,
    );
    assert.ok(events.includes("verification:complete"));
    const servicePrompt = client.systems.find((value) => value.startsWith("PLAN_STAGE:services"));
    assert.ok(servicePrompt?.includes("Clock"));
    assert.ok(!servicePrompt?.includes("GreetingService"));
    assert.equal(client.serviceGenerations, 2);

    const noCalls: ModelClient = {
      complete: async () => { throw new Error("completed stages must not call the model"); },
    };
    const resumed = await new ConstructionAgent(root, noCalls).run("Build a greeting CLI");
    assert.equal(resumed.status, "complete");

    const revised = await agent.revise("Change the CLI presentation", "auto");
    assert.equal(revised.status, "complete");
    assert.equal(revised.stages.pipelines.status, "complete");
    assert.equal(revised.stages.interfaces.attempts, 1);
    assert.ok(client.systems.some((value) => value.startsWith("CLASSIFY_REVISION_STAGE")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("invalid stage plan stores evidence and is not frozen", async () => {
  const root = await projectRoot();
  const client: ModelClient = {
    complete: async (system) => {
      if (system.startsWith("PLAN_STAGE:models") || system.startsWith("PLAN_STAGE:providers")) {
        return { summary: "empty", components: [] };
      }
      if (system.startsWith("PLAN_STAGE:services")) {
        return {
          summary: "invalid",
          components: [{
            id: "Coordinator", file: "coordinator.ts", description: "invalid",
            dependencies: ["MissingService"], inputs: {}, outputs: {},
          }],
        };
      }
      throw new Error("unexpected call");
    },
  };
  try {
    await assert.rejects(
      new ConstructionAgent(root, client).run("Build invalid app"),
      /unknown dependency/,
    );
    const state = await loadState(root, "Build invalid app");
    assert.equal(state.stages.services.status, "failed");
    assert.equal(state.stages.services.plan, undefined);
    assert.ok(state.stages.services.evidence[0]?.includes("unknown dependency"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("repair applies bounded exact patches and preserves public exports", async () => {
  const source = "export class Worker { value = 1; }\n";
  assert.equal(
    applyCodePatches(source, [{ oldText: "value = 1", newText: "value = 2" }]),
    "export class Worker { value = 2; }\n",
  );
  assert.throws(
    () => applyCodePatches(source, [{ oldText: "export class Worker", newText: "class Hidden" }]),
    /removed public exports/,
  );

  const root = await mkdtemp(join(tmpdir(), "aipod-node-repair-"));
  const file = "worker.ts";
  await writeFile(join(root, file), source);
  const client: ModelClient = {
    complete: async () => ({
      patches: [{ oldText: "value = 1", newText: "value = 3" }],
    }),
  };
  try {
    await repairArtifact(client, root, file, ["value should be 3"]);
    assert.match(await readFile(join(root, file), "utf8"), /value = 3/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("construction agent cancels cooperatively before committing a partial stage", async () => {
  const root = await projectRoot();
  let cancelled = false;
  const client: ModelClient = {
    complete: async () => {
      cancelled = true;
      return {
        summary: "would create a model",
        components: [{
          id: "Draft", file: "draft.ts", description: "draft",
          dependencies: [], inputs: {}, outputs: {},
        }],
      };
    },
  };
  try {
    await assert.rejects(
      new ConstructionAgent(root, client, () => undefined, () => cancelled)
        .run("Build then cancel"),
      /cancelled/,
    );
    const state = await loadState(root, "Build then cancel");
    assert.equal(state.status, "cancelled");
    assert.equal(state.stages.models.status, "pending");
    await assert.rejects(readFile(join(root, "src", "models", "draft.ts")), /ENOENT/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
