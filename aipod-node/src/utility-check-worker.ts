import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

import type { UtilityCase } from "./utilities.js";

const request = JSON.parse(await readFile(process.argv[2]!, "utf8")) as { file: string; symbol: string; cases: UtilityCase[] };
const imported = await import(pathToFileURL(request.file).href) as Record<string, Record<string, (...args: unknown[]) => unknown>>;
const utility = imported[request.symbol];
assert.ok(utility, "Utility class export is missing");
for (const item of request.cases) {
  const method: ((...args: unknown[]) => unknown) | undefined = utility[item.method];
  assert.equal(typeof method, "function", `Unknown static method '${item.method}'`);
  const arguments_ = structuredClone(item.args);
  if (item.raises) {
    let error: unknown;
    try { method!(...arguments_); } catch (caught) { error = caught; }
    assert.ok(error instanceof Error && error.name === item.raises, `Expected ${item.raises} from ${item.method}`);
  } else {
    const actual = method!(...arguments_);
    assert.ok(!(actual instanceof Promise), "Utility methods must be synchronous");
    assert.deepStrictEqual(actual, item.expected, `Utility case failed: ${item.method}`);
  }
  assert.deepStrictEqual(arguments_, item.args, `Utility case mutated its caller's arguments: ${item.method}`);
}
