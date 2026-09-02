import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import {
  commitArtifacts, generateArtifacts, validateArtifacts, validateTypeScript,
} from "./agent/artifacts.js";
import type { ModelClient, StageName } from "./agent/types.js";
import {
  applyComponents, loadProject, saveProject, type ProjectBean,
} from "./agent/project.js";
import { planStage, validateStagePlan } from "./agent/planner.js";
import { validateServiceSource } from "./contracts.js";
import { loadProjectConfiguration } from "./shared-config.js";

export async function createComponents(
  projectRoot: string,
  client: ModelClient,
  category: "model" | "provider" | "service",
  description: string,
): Promise<string[]> {
  const stage = `${category}s` as Extract<StageName, "models" | "providers" | "services">;
  const project = await loadProject(projectRoot);
  const plan = await planStage(
    client, stage, description, project, [],
    await loadProjectConfiguration(projectRoot),
  );
  const errors = validateStagePlan(stage, plan, project);
  if (errors.length) throw new Error(errors.join("; "));
  const artifacts = await generateArtifacts(client, stage, plan, project);
  const artifactErrors = validateArtifacts(artifacts);
  if (artifactErrors.length) throw new Error(artifactErrors.join("; "));
  await commitArtifacts(projectRoot, stage, artifacts);
  applyComponents(project, stage, plan.components ?? []);
  await saveProject(projectRoot, project);
  return artifacts.map((artifact) => artifact.path);
}

export async function composeRoutes(
  projectRoot: string,
  client: ModelClient,
  instruction: string,
): Promise<string[]> {
  const project = await loadProject(projectRoot);
  const plan = await planStage(
    client, "pipelines", instruction, project, [],
    await loadProjectConfiguration(projectRoot),
  );
  const errors = validateStagePlan("pipelines", plan, project);
  if (errors.length) throw new Error(errors.join("; "));
  const artifacts = await generateArtifacts(client, "pipelines", plan, project);
  const artifactErrors = validateArtifacts(artifacts);
  if (artifactErrors.length) throw new Error(artifactErrors.join("; "));
  await commitArtifacts(projectRoot, "pipelines", artifacts);
  for (const route of plan.routes ?? []) {
    const value = { ...route, file: `src/pipelines/${route.name}.ts` };
    const index = project.routes.findIndex((item) => item.name === route.name);
    if (index >= 0) project.routes[index] = value;
    else project.routes.push(value);
  }
  await saveProject(projectRoot, project);
  return artifacts.map((artifact) => artifact.path);
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
