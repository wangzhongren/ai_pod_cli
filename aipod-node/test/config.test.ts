import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  ConfigStore,
  applySharedEnvironment,
  loadGlobalEnvironment,
  loadProjectConfiguration,
  removeGlobalEnvironmentKey,
  saveGlobalEnvironment,
  planStage,
  type ModelClient,
} from "../src/index.js";

test("Node and Python share global [env] TOML with environment priority", async () => {
  const root = await mkdtemp(join(tmpdir(), "aipod-shared-config-"));
  const global = join(root, ".aipod", "config.toml");
  const project = join(root, "project");
  await mkdir(join(root, ".aipod"), { recursive: true });
  await mkdir(project, { recursive: true });
  await writeFile(global, `
# keep this comment
[env]
OPENAI_API_KEY = "global-key"
OPENAI_MODEL = "global-model"
OPENAI_BASE_URL = "https://global.example/v1"

[studio]
theme = "dark"
`);
  await writeFile(join(project, ".env"), "OPENAI_MODEL=local-model\nOPENAI_API_KEY=local-key\n");
  const environment: NodeJS.ProcessEnv = { OPENAI_API_KEY: "process-key" };
  try {
    await applySharedEnvironment(project, environment, global);
    assert.equal(environment.OPENAI_API_KEY, "process-key");
    assert.equal(environment.OPENAI_MODEL, "local-model");
    assert.equal(environment.OPENAI_BASE_URL, "https://global.example/v1");

    await saveGlobalEnvironment({ OPENAI_TIMEOUT_SECONDS: "45" }, global);
    assert.equal((await loadGlobalEnvironment(global)).OPENAI_TIMEOUT_SECONDS, "45");
    assert.equal(await removeGlobalEnvironmentKey("OPENAI_MODEL", global), true);
    assert.equal((await loadGlobalEnvironment(global)).OPENAI_MODEL, undefined);
    const preserved = await readFile(global, "utf8");
    assert.match(preserved, /# keep this comment/);
    assert.match(preserved, /\[studio\]\ntheme = "dark"/);
    assert.equal(preserved.includes("OPENAI_MODEL ="), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Agent sees project configuration keys without secret values", async () => {
  let prompt = "";
  const client: ModelClient = {
    complete: async (system) => {
      prompt = system;
      return { summary: "empty", components: [] };
    },
  };
  await planStage(
    client,
    "services",
    "Build service",
    { schemaVersion: 1, beans: [], routes: [], interfaces: [] },
    [],
    { database: { url: "sqlite:///app.db", password: "do-not-leak" } },
  );
  assert.match(prompt, /sqlite:\/\/\/app\.db/);
  assert.equal(prompt.includes("do-not-leak"), false);
  assert.match(prompt, /\[REDACTED\]/);
});

test("ConfigStore prefers shared project config.toml and falls back to config.json", async () => {
  const root = await mkdtemp(join(tmpdir(), "aipod-project-config-"));
  try {
    await writeFile(join(root, "config.json"), JSON.stringify({ server: { port: 3000 } }));
    assert.equal((await new ConfigStore(root).load()).get("server.port"), 3000);

    await writeFile(join(root, "config.toml"), "[server]\nport = 8080\n[database]\nurl = 'sqlite:///shared.db'\n");
    const configuration = await loadProjectConfiguration(root);
    const store = await new ConfigStore(root).load();
    assert.equal(store.get("server.port"), 8080);
    assert.equal(store.get("database.url"), "sqlite:///shared.db");
    assert.deepEqual(store.sections().sort(), ["database", "server"]);
    assert.equal((configuration.server as { port: number }).port, 8080);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
