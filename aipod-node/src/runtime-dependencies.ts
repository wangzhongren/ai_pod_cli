import { readFile, realpath } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

async function packageRoot(name: string, require: ReturnType<typeof createRequire>): Promise<string> {
  let entry: string;
  try { entry = require.resolve(`${name}/package.json`); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ERR_PACKAGE_PATH_NOT_EXPORTED") throw error;
    entry = require.resolve(name);
  }
  let directory = dirname(await realpath(entry));
  while (true) {
    try {
      const manifest = JSON.parse(await readFile(resolve(directory, "package.json"), "utf8")) as { name?: string };
      if (manifest.name === name) return directory;
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    const parent = dirname(directory);
    if (parent === directory) throw new Error(`Cannot locate runtime dependency '${name}'`);
    directory = parent;
  }
}

/** Explicit package grants work for both nested and npm-hoisted dependency layouts. */
export async function componentWorkerReadRoots(from = import.meta.url): Promise<string[]> {
  const ownRoot = await realpath(resolve(dirname(fileURLToPath(from)), "../.."));
  const require = createRequire(from);
  const [typescript, toml, nodeTypes] = await Promise.all([
    packageRoot("typescript", require), packageRoot("smol-toml", require), packageRoot("@types/node", require),
  ]);
  // Resolve the type dependency from its consumer, including nested-version installs.
  const undiciTypes = await packageRoot("undici-types", createRequire(resolve(nodeTypes, "package.json")));
  return [...new Set([ownRoot, typescript, toml, nodeTypes, undiciTypes])];
}
