import assert from "node:assert/strict";
import test from "node:test";
import { PipelineContext, type Contract, type InferContract } from "../src/index.js";

const inputs = {
  order: { type: "object", properties: {
    id: { type: "string" }, lines: { type: "array", items: { type: "number" } },
  } },
  discount: { type: "number", required: false },
} as const satisfies Contract;
const outputs = { total: { type: "number" } } as const satisfies Contract;

// Compiled by npm test; invalid field names/types must stay compile errors.
function typeAssertions(context: PipelineContext) {
  const view = context.typed(inputs, outputs);
  const id: string = view.get("order").id;
  const discount: number | undefined = view.get("discount");
  // @ts-expect-error unknown input key
  view.get("oder");
  // @ts-expect-error optional field cannot be assumed present
  const required: number = view.get("discount");
  // @ts-expect-error inputs are not writable outputs
  view.set("order", { id, lines: [] });
  // @ts-expect-error wrong output type
  view.set("total", "12");
  // @ts-expect-error required output is missing
  view.output({});
  // @ts-expect-error nested array item has wrong type
  const invalid: InferContract<typeof inputs> = { order: { id, lines: ["12"] } };
  return { discount, required, invalid };
}
void typeAssertions;

test("contract Context infers nested data and checks reads, writes and complete outputs", () => {
  const context = new PipelineContext({ order: { id: "o-1", lines: [10, 20] } });
  const view = context.typed(inputs, outputs);
  assert.equal(view.get("discount"), undefined);
  const order = view.get("order");
  order.lines.push(99);
  assert.deepEqual(view.get("order").lines, [10, 20]);
  view.set("total", view.get("order").lines.reduce((a, b) => a + b, 0));
  assert.equal(context.get("total"), 30);
  assert.deepEqual(view.output({ total: 30 }), { total: 30 });
  assert.throws(() => view.set("total", "30" as never), /expected number/);
  assert.equal(context.get("total"), 30);
  assert.throws(() => view.output({} as never), /required field/);
  assert.throws(() => view.get("missing" as never), /Undeclared input/);
  assert.throws(() => view.set("missing" as never, 1 as never), /Undeclared output/);
  context.set("order", { id: 123, lines: [] });
  assert.throws(() => view.get("order"), /expected string/);
  assert.throws(() => new PipelineContext().typed(inputs, outputs), /required field/);
});
