import { loadProject } from "./agent/project.js";
import { loadRunner } from "./loader.js";
import type { PipelineRunner } from "./runner.js";
import { runVerificationCommand, type CommandEvidence } from "./verification.js";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";

export class InterfaceContext {
  #runner?: PipelineRunner;

  constructor(readonly projectRoot: string) {}

  async runner(): Promise<PipelineRunner> {
    this.#runner ??= await loadRunner(this.projectRoot);
    return this.#runner;
  }

  async routeNames(): Promise<string[]> {
    return (await this.runner()).routeNames();
  }

  async runRoute(route: string, params: Record<string, unknown> = {}) {
    return (await this.runner()).run(route, params);
  }
}

export interface InterfaceAdapter {
  requiredRoutes(): string[];
  start(payload?: Record<string, unknown>): Promise<unknown> | unknown;
}

export async function loadInterface(
  projectRoot: string,
  name: string,
): Promise<{ adapter: InterfaceAdapter; context: InterfaceContext }> {
  const project = await loadProject(projectRoot);
  const definition = project.interfaces.find((item) => item.name === name);
  if (!definition) throw new Error(`Unknown Interface '${name}'`);
  const runner = await loadRunner(projectRoot);
  const buildFile = definition.file
    .replace(/^src[\\/]interfaces[\\/]/, ".aipod/build/interfaces/")
    .replace(/\.ts$/, ".js");
  const path = resolve(projectRoot, buildFile);
  const version = createHash("sha256").update(await readFile(path)).digest("hex");
  const module = await import(`${pathToFileURL(path).href}?v=${version}`) as Record<string, unknown>;
  const className = `${definition.name}Adapter`;
  const Constructor = module[className] as (new (runner: PipelineRunner) => InterfaceAdapter) | undefined;
  if (typeof Constructor !== "function") throw new Error(`${definition.file} does not export '${className}'`);
  return { adapter: new Constructor(runner), context: new InterfaceContext(projectRoot) };
}

export async function smokeInterface(projectRoot: string, name: string): Promise<Record<string, unknown>> {
  const { adapter, context } = await loadInterface(projectRoot, name);
  const available = new Set(await context.routeNames());
  const required = adapter.requiredRoutes();
  const missing = required.filter((route) => !available.has(route));
  return {
    status: missing.length ? "failed" : "passed",
    requiredRoutes: required,
    missingRoutes: missing,
  };
}

const expandCommand = (command: string[], projectRoot: string) => command.map((item) => {
  if (item === "{node}") return process.execPath;
  if (item === "{projectRoot}") return projectRoot;
  return item.replaceAll("{projectRoot}", projectRoot);
});

export async function runInterfaceLifecycle(
  projectRoot: string,
  name: string,
  action: "install" | "uninstall",
): Promise<CommandEvidence | { status: "skipped"; action: string; reason: string }> {
  const project = await loadProject(projectRoot);
  const definition = project.interfaces.find((item) => item.name === name);
  if (!definition) throw new Error(`Unknown Interface '${name}'`);
  const command = definition.lifecycle?.[action];
  if (!command?.length) return { status: "skipped", action, reason: "not declared" };
  return runVerificationCommand(projectRoot, expandCommand(command, projectRoot));
}

export async function verifyInterface(
  projectRoot: string,
  name: string,
): Promise<Record<string, unknown>> {
  const project = await loadProject(projectRoot);
  const definition = project.interfaces.find((item) => item.name === name);
  if (!definition) throw new Error(`Unknown Interface '${name}'`);
  const results = [];
  for (const check of definition.verify ?? []) {
    const result = await runVerificationCommand(
      projectRoot,
      expandCommand(check.command, projectRoot),
      check.timeoutMs,
    );
    results.push({ name: check.name, required: check.required, ...result });
  }
  const failed = results.filter((item) => item.required && item.status !== "passed");
  return { status: failed.length ? "failed" : "passed", checks: results };
}
