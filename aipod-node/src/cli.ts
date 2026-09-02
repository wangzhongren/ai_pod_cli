#!/usr/bin/env node

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { spawn } from "node:child_process";

import { ConstructionAgent } from "./agent/agent.js";
import { OpenAICompatibleClient } from "./agent/client.js";
import { loadProject } from "./agent/project.js";
import type { ProjectBean } from "./agent/project.js";
import {
  loadInterface, runInterfaceLifecycle, smokeInterface, verifyInterface,
} from "./interface.js";
import { loadRunner, runRoute } from "./loader.js";
import { inspectProject } from "./project-model.js";
import { addBean, composeRoutes, createComponents } from "./operations.js";
import type { Contract } from "./contracts.js";
import { startStudio } from "./studio.js";
import { runVerificationCommand } from "./verification.js";
import {
  DistributedStreamPublisher, DistributedWorker, HttpStreamTransport,
  startStreamBroker,
} from "./distributed/index.js";
import {
  applySharedEnvironment, globalConfigPath, loadGlobalEnvironment,
  removeGlobalEnvironmentKey, saveGlobalEnvironment,
} from "./shared-config.js";

const [, , command = "help", ...args] = process.argv;

async function init(target = "."): Promise<void> {
  const root = resolve(target);
  for (const directory of ["models", "providers", "services", "pipelines", "interfaces"]) {
    await mkdir(resolve(root, "src", directory), { recursive: true });
  }
  const project = {
    schemaVersion: 1,
    beans: [{
      id: "ConfigStore",
      category: "provider",
      file: "aipod:config-store",
      description: "Built-in project configuration provider",
      dependencies: [],
      inputs: {},
      outputs: {},
    }, {
      id: "ModelRepository",
      category: "provider",
      file: "aipod:model-repository",
      description: "Built-in atomic JSON model repository",
      dependencies: [],
      inputs: {},
      outputs: {},
    }],
    routes: [],
    interfaces: [],
  };
  await writeFile(resolve(root, "aipod.json"), `${JSON.stringify(project, null, 2)}\n`, { flag: "wx" });
  await writeFile(
    resolve(root, "config.toml"),
    "# Shared AIPod project configuration (Python and Node.js)\n",
    { flag: "wx" },
  );
  console.log(`Initialized AIPod Node project at ${root}`);
}

async function inspect(target = "."): Promise<void> {
  const root = resolve(target);
  console.log(JSON.stringify(await inspectProject(root), null, 2));
}

function option(name: string): string | undefined {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
}

function configuredClient(): OpenAICompatibleClient {
  const apiKey = process.env.OPENAI_API_KEY;
  const model = process.env.OPENAI_MODEL ?? "deepseek-chat";
  if (!apiKey) throw new Error("OPENAI_API_KEY is required");
  const timeoutMs = process.env.OPENAI_TIMEOUT_MS
    ? Number(process.env.OPENAI_TIMEOUT_MS)
    : process.env.OPENAI_TIMEOUT_SECONDS
      ? Number(process.env.OPENAI_TIMEOUT_SECONDS) * 1_000
      : undefined;
  return new OpenAICompatibleClient({
    apiKey,
    model,
    ...(process.env.OPENAI_BASE_URL ? { baseUrl: process.env.OPENAI_BASE_URL } : {}),
    ...(timeoutMs ? { timeoutMs } : {}),
  });
}

async function pod(): Promise<void> {
  const projectRoot = resolve(option("--project-root") ?? ".");
  const file = option("--file");
  const excluded = new Set<number>();
  for (const flag of ["--project-root", "--file", "--stage"]) {
    const index = args.indexOf(flag);
    if (index >= 0) {
      excluded.add(index);
      excluded.add(index + 1);
    }
  }
  const objective = file
    ? await readFile(resolve(file), "utf8")
    : args.filter((_, index) => !excluded.has(index)).join(" ").trim();
  const client = configuredClient();
  const agent = new ConstructionAgent(projectRoot, client, (event) => {
    console.log(`[${event.stage}] ${event.action}: ${event.message}`);
  });
  const stage = option("--stage");
  const state = stage
    ? await agent.revise(objective, stage as "auto" | "models" | "providers" | "services" | "pipelines" | "interfaces")
    : await agent.run(objective);
  console.log(JSON.stringify({ status: state.status, verification: state.verification }, null, 2));
}

async function create(): Promise<void> {
  const category = option("--category") as "model" | "provider" | "service" | undefined;
  if (!category || !["model", "provider", "service"].includes(category)) {
    throw new Error("--category model|provider|service is required");
  }
  const description = option("--description") ?? option("--desc") ?? "";
  if (!description) throw new Error("--description is required");
  const root = resolve(option("--project-root") ?? ".");
  console.log(JSON.stringify({
    artifacts: await createComponents(root, configuredClient(), category, description),
  }, null, 2));
}

async function compose(): Promise<void> {
  const root = resolve(option("--project-root") ?? ".");
  const excluded = new Set<number>();
  const rootIndex = args.indexOf("--project-root");
  if (rootIndex >= 0) { excluded.add(rootIndex); excluded.add(rootIndex + 1); }
  const instruction = args.filter((_, index) => !excluded.has(index)).join(" ").trim();
  if (!instruction) throw new Error("Pipeline instruction is required");
  console.log(JSON.stringify({
    artifacts: await composeRoutes(root, configuredClient(), instruction),
  }, null, 2));
}

async function add(): Promise<void> {
  const root = resolve(option("--project-root") ?? ".");
  const bean: ProjectBean = {
    id: option("--id") ?? "",
    category: option("--category") as ProjectBean["category"],
    file: option("--file") ?? "",
    description: option("--description") ?? "",
    dependencies: JSON.parse(option("--dependencies") ?? "[]") as string[],
    inputs: parseObject(option("--inputs")) as Contract,
    outputs: parseObject(option("--outputs")) as Contract,
  };
  if (!bean.id || !bean.category || !bean.file) throw new Error("--id, --category, and --file are required");
  await addBean(root, bean);
  console.log(JSON.stringify({ status: "added", bean: bean.id }, null, 2));
}

const parseObject = (raw: string | undefined): Record<string, unknown> => {
  const value = JSON.parse(raw ?? "{}") as unknown;
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Parameters must be a JSON object");
  }
  return value as Record<string, unknown>;
};

async function run(): Promise<void> {
  const route = args[0];
  if (!route) throw new Error("Route name is required");
  const root = resolve(option("--project-root") ?? ".");
  const trace = await runRoute(root, route, parseObject(option("--params")));
  console.log(JSON.stringify(trace, null, 2));
  if (trace.status === "failure") process.exitCode = 1;
}

async function verify(): Promise<void> {
  const separator = args.indexOf("--");
  const check = separator >= 0 ? args.slice(separator + 1) : [];
  const positional = args.slice(0, separator >= 0 ? separator : args.length)
    .find((item, index, values) =>
      !item.startsWith("--") && values[index - 1] !== "--project-root"
    );
  const root = resolve(option("--project-root") ?? positional ?? ".");
  const model = await inspectProject(root) as {
    validation: { valid: boolean; issues: unknown[] };
  };
  const evidence = [...model.validation.issues];
  if (!evidence.length) {
    try { await loadRunner(root); } catch (error) {
      evidence.push(error instanceof Error ? error.message : String(error));
    }
  }
  let commandEvidence = null;
  if (!evidence.length && check.length) {
    commandEvidence = await runVerificationCommand(root, check);
    if (commandEvidence.status !== "passed") evidence.push(commandEvidence);
  }
  const result = {
    status: evidence.length ? "failed" : check.length ? "passed" : "unverified",
    evidence,
    command: commandEvidence,
  };
  console.log(JSON.stringify(result, null, 2));
  if (evidence.length) process.exitCode = 1;
}

async function interfaceCommand(): Promise<void> {
  const action = args[0] ?? "list";
  const name = args[1];
  const root = resolve(option("--project-root") ?? ".");
  if (action === "list") {
    const project = await loadProject(root);
    console.log(JSON.stringify({ interfaces: project.interfaces.map((item) => item.name) }, null, 2));
    return;
  }
  if (!name) throw new Error("Interface name is required");
  if (action === "smoke") {
    console.log(JSON.stringify(await smokeInterface(root, name), null, 2));
    return;
  }
  if (action === "run") {
    const { adapter } = await loadInterface(root, name);
    console.log(JSON.stringify(await adapter.start(parseObject(option("--payload"))), null, 2));
    return;
  }
  if (action === "install" || action === "uninstall") {
    console.log(JSON.stringify(await runInterfaceLifecycle(root, name, action), null, 2));
    return;
  }
  if (action === "verify") {
    console.log(JSON.stringify(await verifyInterface(root, name), null, 2));
    return;
  }
  throw new Error(`Unknown Interface action '${action}'`);
}

async function studio(): Promise<void> {
  const portIndex = args.indexOf("--port");
  const root = resolve(args.find((item, index) =>
    !item.startsWith("--") && index !== portIndex + 1
  ) ?? ".");
  const server = await startStudio(root, {
    ...(option("--port") ? { port: Number(option("--port")) } : {}),
  });
  console.log(`AIPod Node Studio: ${server.url}`);
  if (!args.includes("--no-open")) {
    const command = process.platform === "darwin"
      ? ["open", server.url]
      : process.platform === "win32"
        ? ["cmd", "/c", "start", "", server.url]
        : ["xdg-open", server.url];
    spawn(command[0]!, command.slice(1), { detached: true, stdio: "ignore" }).unref();
  }
  await new Promise<void>((resolveStop) => {
    const stop = () => resolveStop();
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
  });
  await server.close();
}

const brokerTransport = () => {
  const url = option("--broker") ?? process.env.AIPOD_BROKER_URL;
  const token = option("--token") ?? process.env.AIPOD_BROKER_TOKEN;
  if (!url || !token) throw new Error("--broker/AIPOD_BROKER_URL and --token/AIPOD_BROKER_TOKEN are required");
  return new HttpStreamTransport(url, token);
};

async function broker(): Promise<void> {
  const root = resolve(option("--project-root") ?? ".");
  const data = option("--data");
  const host = option("--host");
  const port = option("--port");
  const server = await startStreamBroker({
    file: data ? resolve(root, data) : resolve(root, ".aipod", "broker.json"),
    ...(host ? { host } : {}),
    ...(port ? { port: Number(port) } : {}),
    ...(option("--token") || process.env.AIPOD_BROKER_TOKEN
      ? { token: option("--token") ?? process.env.AIPOD_BROKER_TOKEN! }
      : {}),
  });
  console.log(JSON.stringify({ url: server.url, token: server.token }, null, 2));
  await new Promise<void>((accept) => {
    process.once("SIGINT", () => accept());
    process.once("SIGTERM", () => accept());
  });
  await server.close();
}

async function publish(): Promise<void> {
  const streamName = option("--stream");
  if (!streamName) throw new Error("--stream is required");
  const key = option("--key");
  const publisher = new DistributedStreamPublisher(brokerTransport(), streamName);
  console.log(JSON.stringify(await publisher.publish(
    parseObject(option("--payload")),
    {
      ...(key ? { key } : {}),
    },
  ), null, 2));
}

async function worker(): Promise<void> {
  const root = resolve(option("--project-root") ?? ".");
  const required = (name: string) => {
    const value = option(name);
    if (!value) throw new Error(`${name} is required`);
    return value;
  };
  const controller = new AbortController();
  process.once("SIGINT", () => controller.abort());
  process.once("SIGTERM", () => controller.abort());
  const distributed = new DistributedWorker(
    brokerTransport(),
    await loadRunner(root),
    {
      stream: required("--stream"),
      group: required("--group"),
      consumer: option("--consumer") ?? `worker-${process.pid}`,
      route: required("--route"),
      ...(option("--concurrency") ? { concurrency: Number(option("--concurrency")) } : {}),
      ...(option("--lease-ms") ? { leaseMs: Number(option("--lease-ms")) } : {}),
      ...(option("--max-attempts") ? { maxAttempts: Number(option("--max-attempts")) } : {}),
      ...(option("--poll-ms") ? { pollMs: Number(option("--poll-ms")) } : {}),
    },
  );
  console.log(JSON.stringify(await distributed.run(controller.signal), null, 2));
}

async function deadLetters(): Promise<void> {
  const transport = brokerTransport();
  console.log(JSON.stringify(await transport.deadLetters({
    ...(option("--stream") ? { stream: option("--stream")! } : {}),
    ...(option("--group") ? { group: option("--group")! } : {}),
  }), null, 2));
}

async function brokerStats(): Promise<void> {
  console.log(JSON.stringify(await brokerTransport().stats(), null, 2));
}

async function requeue(): Promise<void> {
  const messageId = option("--message-id");
  const streamName = option("--stream");
  const group = option("--group");
  if (!messageId || !streamName || !group) {
    throw new Error("--message-id, --stream, and --group are required");
  }
  console.log(JSON.stringify({
    requeued: await brokerTransport().requeue(messageId, streamName, group),
  }, null, 2));
}

const mask = (key: string, value: string) =>
  /KEY|SECRET|TOKEN|PASSWORD/i.test(key)
    ? value.length > 8 ? `${value.slice(0, 4)}****${value.slice(-4)}` : "****"
    : value;

async function config(): Promise<void> {
  const action = args[0] ?? "list";
  const key = String(args[1] ?? "").toUpperCase();
  const values = await loadGlobalEnvironment();
  if (action === "path") { console.log(globalConfigPath()); return; }
  if (action === "list") {
    console.log(JSON.stringify(Object.fromEntries(
      Object.entries(values).map(([name, value]) => [name, mask(name, value)]),
    ), null, 2));
    return;
  }
  if (!key) throw new Error("Configuration key is required");
  if (action === "get") {
    if (!(key in values)) throw new Error(`${key} is not configured`);
    console.log(`${key}=${mask(key, values[key]!)}`);
    return;
  }
  if (action === "set") {
    const value = args.slice(2).join(" ");
    if (!value) throw new Error("Configuration value is required");
    await saveGlobalEnvironment({ [key]: value });
    console.log(`${key}=${mask(key, value)}`);
    return;
  }
  if (action === "remove") {
    console.log(JSON.stringify({ removed: await removeGlobalEnvironmentKey(key) }));
    return;
  }
  throw new Error(`Unknown config action '${action}'`);
}

function help(): void {
  console.log(`AIPod Node

Usage:
  aipod-node init [directory]
  aipod-node inspect [directory]
  aipod-node pod "application requirement" [--project-root directory]
  aipod-node pod "change request" --stage auto|models|providers|services|pipelines|interfaces
  aipod-node pod --file requirement.md [--project-root directory]
  aipod-node create --category model|provider|service --description "..."
  aipod-node add --id ID --category TYPE --file src/path.ts [--dependencies JSON]
  aipod-node compose "pipeline instruction" [--project-root directory]
  aipod-node run <route> [--params JSON] [--project-root directory]
  aipod-node verify [directory] [-- command args...]
  aipod-node interface list [--project-root directory]
  aipod-node interface smoke <name> [--project-root directory]
  aipod-node interface run <name> [--payload JSON] [--project-root directory]
  aipod-node interface install|uninstall|verify <name> [--project-root directory]
  aipod-node studio [directory] [--port number] [--no-open]
  aipod-node broker [--project-root directory] [--host IP] [--port number]
  aipod-node publish --broker URL --token TOKEN --stream NAME --payload JSON [--key KEY]
  aipod-node worker --broker URL --token TOKEN --stream NAME --group NAME --route ROUTE
  aipod-node broker-stats --broker URL --token TOKEN
  aipod-node dead-letters --broker URL --token TOKEN [--stream NAME] [--group NAME]
  aipod-node requeue --broker URL --token TOKEN --message-id ID --stream NAME --group NAME
  aipod-node config list|set|get|remove|path [KEY] [VALUE]
  aipod-node help`);
}

try {
  const positionalProject = ["init", "inspect", "verify", "studio"].includes(command)
    && args[0] && !args[0].startsWith("--") ? args[0] : undefined;
  await applySharedEnvironment(resolve(option("--project-root") ?? positionalProject ?? "."));
  if (command === "init") await init(args[0]);
  else if (command === "inspect") await inspect(args[0]);
  else if (command === "pod") await pod();
  else if (command === "create") await create();
  else if (command === "add") await add();
  else if (command === "compose") await compose();
  else if (command === "run") await run();
  else if (command === "verify") await verify();
  else if (command === "interface") await interfaceCommand();
  else if (command === "studio") await studio();
  else if (command === "broker") await broker();
  else if (command === "publish") await publish();
  else if (command === "worker") await worker();
  else if (command === "dead-letters") await deadLetters();
  else if (command === "requeue") await requeue();
  else if (command === "broker-stats") await brokerStats();
  else if (command === "config") await config();
  else if (command === "help" || command === "--help" || command === "-h") help();
  else throw new Error(`Unknown command '${command}'`);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
