import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";

import { componentWorkerReadRoots } from "../src/runtime-dependencies.js";

test("worker read grants contain only the framework and explicit runtime/type dependency packages", async () => {
  const roots = await componentWorkerReadRoots();
  const names = await Promise.all(roots.map(async (root) => {
    assert.equal(root, await realpath(root));
    return (JSON.parse(await readFile(join(root, "package.json"), "utf8")) as { name: string }).name;
  }));
  assert.deepEqual(names.sort(), ["@types/node", "aipod-node", "smol-toml", "typescript", "undici-types"]);
});

test("worker grants resolve hoisted and symlinked packages without allowing their parent node_modules", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "aipod-hoisted-deps-"));
  const root = await realpath(temporary);
  try {
    const framework = join(root, "node_modules/aipod-node");
    const anchor = join(framework, "dist/src/runtime-dependencies.js");
    const packageAt = async (directory: string, name: string, exports?: Record<string, string>) => {
      await mkdir(join(directory, "dist"), { recursive: true });
      await writeFile(join(directory, "package.json"), JSON.stringify({ name, main: "dist/index.js", ...(exports ? { exports } : {}) }));
      await writeFile(join(directory, "dist/index.js"), "export {};\n");
    };
    await packageAt(framework, "aipod-node");
    await mkdir(dirname(anchor), { recursive: true }); await writeFile(anchor, "export {};\n");
    const typescript = join(root, "store/typescript");
    await packageAt(typescript, "typescript");
    await symlink(typescript, join(root, "node_modules/typescript"), "dir");
    const toml = join(root, "node_modules/smol-toml");
    await packageAt(toml, "smol-toml", { ".": "./dist/index.js" });
    const nodeTypes = join(root, "node_modules/@types/node");
    await packageAt(nodeTypes, "@types/node");
    const undici = join(nodeTypes, "node_modules/undici-types");
    await packageAt(undici, "undici-types");
    await packageAt(join(root, "node_modules/undici-types"), "undici-types");
    const grants = await componentWorkerReadRoots(pathToFileURL(anchor).href);
    assert.deepEqual(new Set(grants), new Set([framework, typescript, toml, nodeTypes, undici]));
    assert.ok(!grants.includes(join(root, "node_modules")));
    assert.ok(!grants.includes(join(root, "node_modules/@types")));
    assert.ok(!grants.includes(root));
  } finally { await rm(temporary, { recursive: true, force: true }); }
});
