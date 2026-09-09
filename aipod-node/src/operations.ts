import { ConstructionAgent } from "./agent/agent.js";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { validateTypeScript } from "./agent/artifacts.js";
import type { ModelClient, StageName } from "./agent/types.js";
import {
  loadProject, saveProject, type ProjectBean,
} from "./agent/project.js";
import { validateServiceSource } from "./contracts.js";

export async function createComponents(projectRoot: string, client: ModelClient, category: "model" | "provider" | "service", description: string): Promise<string[]> {
  return new ConstructionAgent(projectRoot, client).runStage(`${category}s` as StageName, description);
}

export async function composeRoutes(projectRoot: string, client: ModelClient, instruction: string): Promise<string[]> {
  return new ConstructionAgent(projectRoot, client).runStage("pipelines", instruction);
}

export async function addBean(projectRoot: string, bean: ProjectBean): Promise<void> {
  const project = await loadProject(projectRoot);
  if (!["model", "provider", "service"].includes(bean.category)) {
    throw new Error(`Invalid Bean category '${bean.category}'`);
  }
  if (project.beans.some((item) => item.id === bean.id)) throw new Error(`Bean '${bean.id}' already exists`);
  const source = await readFile(resolve(projectRoot, bean.file), "utf8");
  const errors = validateTypeScript(source, bean.file);
  if (bean.category === "service") errors.push(...validateServiceSource(source));
  const categories = new Map(project.beans.map((item) => [item.id, item.category]));
  for (const dependency of bean.dependencies) {
    const category = categories.get(dependency);
    if (!category) errors.push(`Unknown dependency '${dependency}'`);
    if (bean.category === "service" && (category === "service" || dependency === "PipelineRunner")) {
      errors.push(`Service '${bean.id}' cannot see '${dependency}'`);
    }
  }
  if (errors.length) throw new Error(errors.join("; "));
  project.beans.push(bean);
  await saveProject(projectRoot, project);
}
