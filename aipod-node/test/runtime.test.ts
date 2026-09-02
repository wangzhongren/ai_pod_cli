import assert from "node:assert/strict";
import test from "node:test";

import {
  Container,
  analyzePipelineContracts,
  PipelineContext,
  PipelineRunner,
  failure,
  parallel,
  repeat,
  service,
  stream,
  validateServiceSource,
  type BeanDefinition,
} from "../src/index.js";

function definitions(): BeanDefinition[] {
  return [
    {
      id: "Increment",
      category: "service",
      inputs: { value: { type: "integer" } },
      outputs: { value: { type: "integer" } },
      factory: () => ({
        execute: (context: PipelineContext) => ({ value: Number(context.get("value")) + 1 }),
      }),
    },
    {
      id: "Double",
      category: "service",
      inputs: { value: { type: "integer" } },
      outputs: { value: { type: "integer" } },
      factory: () => ({
        execute: (context: PipelineContext) => ({ value: Number(context.get("value")) * 2 }),
      }),
    },
  ];
}

test("sequential services execute through governed refs", async () => {
  const container = new Container(definitions());
  const context = new PipelineContext({ value: 2 });
  const flow = service(container, "Increment").pipe(service(container, "Double"));

  const result = await flow.execute(context);

  assert.equal(result.status, "success");
  assert.equal(context.get("value"), 6);
  assert.deepEqual(context.steps.map((step) => step.component), ["Increment", "Double"]);
});

test("service cannot depend on or import another service", () => {
  assert.throws(() => new Container([
    ...definitions(),
    {
      id: "Coordinator",
      category: "service",
      dependencies: ["Increment"],
      factory: () => ({ execute: () => ({}) }),
    },
  ]), /cannot depend/);
  assert.ok(validateServiceSource('import { Worker } from "./services/worker.js"').length);
  assert.ok(validateServiceSource("const runner = new PipelineRunner()").length);
});

test("parallel branches isolate and deterministically merge", async () => {
  const container = new Container([
    {
      id: "Left", category: "service", factory: () => ({ execute: () => ({ left: 1 }) }),
    },
    {
      id: "Right", category: "service", factory: () => ({ execute: () => ({ right: 2 }) }),
    },
  ]);
  const context = new PipelineContext();

  const result = await parallel([
    service(container, "Left"), service(container, "Right"),
  ]).execute(context);

  assert.equal(result.status, "success");
  assert.deepEqual(context.data, { left: 1, right: 2 });
  assert.equal(context.steps[0]?.mode, "parallel");
});

test("repeat keeps orchestration visible and bounds iteration trace", async () => {
  const container = new Container([
    {
      id: "Tick", category: "service", factory: () => ({
        execute: (context: PipelineContext) => ({ tick: Number(context.get("tick", 0)) + 1 }),
      }),
    },
    {
      id: "Stop", category: "service", factory: () => ({
        execute: (context: PipelineContext) => ({ quit: Number(context.get("tick")) >= 3 }),
      }),
    },
  ]);
  const context = new PipelineContext();
  const frame = service(container, "Tick").pipe(service(container, "Stop"));

  const result = await repeat(frame, {
    untilField: "quit", outputField: "frames", traceLimit: 2,
  }).execute(context);

  assert.equal(result.status, "success");
  assert.equal(context.get("frames"), 3);
  assert.equal((context.steps[0]?.iterationTraces as unknown[]).length, 2);
});

test("stream applies bounded maps and batches", async () => {
  const container = new Container(definitions());
  const source = async function* () {
    for (let value = 0; value < 5; value += 1) yield { value };
  };
  const context = new PipelineContext();
  const pipeline = stream(source)
    .map(service(container, "Double"), { concurrency: 2 })
    .batch(2, "items");

  const batches = [];
  for await (const item of pipeline.items(context)) batches.push(item);

  assert.equal(batches.length, 3);
  assert.equal((batches[0]?.data.items as Record<string, unknown>[])[1]?.value, 2);
});

test("runner dispatches named routes", async () => {
  const container = new Container(definitions());
  const runner = new PipelineRunner([
    { name: "calculate", pipeline: service(container, "Increment") },
  ]);

  const execution = await runner.run("calculate", { value: 4 });

  assert.equal(execution.result.status, "success");
  assert.equal(execution.context.get("value"), 5);
});

test("failure stops a sequence", async () => {
  let called = false;
  const container = new Container([
    {
      id: "Fail", category: "service", factory: () => ({ execute: () => failure("stop") }),
    },
    {
      id: "Later", category: "service", factory: () => ({
        execute: () => { called = true; return {}; },
      }),
    },
  ]);
  const result = await service(container, "Fail").pipe(service(container, "Later"))
    .execute(new PipelineContext());
  assert.equal(result.status, "failure");
  assert.equal(called, false);
});

test("Pipeline Contract analysis separates type errors from semantic warnings", () => {
  const mismatch = analyzePipelineContracts(["Load", "Format"], [
    {
      id: "Load", inputs: {}, outputs: { count: { type: "integer" } },
    },
    {
      id: "Format", inputs: { count: { type: "string" } }, outputs: {},
    },
  ]);
  assert.equal(mismatch.valid, false);
  assert.equal(mismatch.issues[0]?.code, "contract_type_mismatch");

  const drift = analyzePipelineContracts(["Read", "Use"], [
    {
      id: "Read", inputs: {}, outputs: { shipmentCount: { type: "integer" } },
    },
    {
      id: "Use", inputs: { shipmentsCount: { type: "integer" } }, outputs: {},
    },
  ]);
  assert.equal(drift.valid, true);
  assert.equal(drift.warnings[0]?.code, "semantic_field_drift");
});
