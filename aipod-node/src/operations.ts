import { ConstructionAgent, type ConstructionOptions } from "./agent/agent.js";
import type { ModelClient, StageName } from "./agent/types.js";
import {
  loadProject, saveProject, type ProjectBean,
} from "./agent/project.js";
import { validateLayout } from "./agent/component-layout.js";

export async function createComponents(projectRoot: string, client: ModelClient, category: "model" | "provider" | "service", description: string, options: ConstructionOptions = {}): Promise<string[]> {
  return new ConstructionAgent(projectRoot, client, undefined, undefined, options).runStage(`${category}s` as StageName, description);
}

export async function composeRoutes(projectRoot: string, client: ModelClient, instruction: string, options: ConstructionOptions = {}): Promise<string[]> {
  return new ConstructionAgent(projectRoot, client, undefined, undefined, options).runStage("pipelines", instruction);
}

export async function addBean(projectRoot: string, bean: ProjectBean): Promise<void> {
  const project = await loadProject(projectRoot);
  if (!["model", "provider", "service"].includes(bean.category)) {
    throw new Error(`Invalid Bean category '${bean.category}'`);
  }
  if (project.beans.some((item) => item.id === bean.id)) throw new Error(`Bean '${bean.id}' already exists`);
  const errors = validateLayout(projectRoot, [...project.beans, bean], `${bean.category}s`);
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
