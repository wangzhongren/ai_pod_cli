// Shared-workspace construction, ownership and cancellation are covered in workspace.test.ts.
import assert from "node:assert/strict";
import {mkdtemp,readFile,rm,writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import test from "node:test";
import {applyCodePatches,repairArtifact,type ModelClient} from "../src/index.js";

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
