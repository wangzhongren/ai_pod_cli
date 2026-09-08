import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { basename, resolve } from "node:path";

import type { Contract } from "../contracts.js";
import { analyzePipelineContracts } from "../contracts.js";
import { listUtilities, type UtilityEntry } from "../utilities.js";
import type { ComponentPlan, InterfacePlan, RoutePlan, StageName } from "./types.js";

export interface ProjectBean {
  id: string;
  category: "model" | "provider" | "service";
  file: string;
  description: string;
  dependencies: string[];
  inputs: Contract;
  outputs: Contract;
}

export interface ProjectManifest {
  schemaVersion: 1;
  beans: ProjectBean[];
  routes: (RoutePlan & { file: string })[];
  interfaces: InterfacePlan[];
  /** Derived construction context; never persisted into aipod.json. */
  projectRoot?: string;
  utilities?: UtilityEntry[];
}

export const manifestPath = (projectRoot: string) => resolve(projectRoot, "aipod.json");

export async function loadProject(projectRoot: string): Promise<ProjectManifest> {
  const parsed = JSON.parse(await readFile(manifestPath(projectRoot), "utf8")) as ProjectManifest;
  if (parsed.schemaVersion !== 1) throw new Error("Unsupported aipod.json schemaVersion");
  parsed.beans ??= [];
  parsed.routes ??= [];
  parsed.interfaces ??= [];
  parsed.projectRoot = resolve(projectRoot);
  parsed.utilities = await listUtilities(projectRoot);
  return parsed;
}

export async function saveProject(projectRoot: string, project: ProjectManifest): Promise<void> {
  const target = manifestPath(projectRoot);
  const temporary = `${target}.tmp`;
  const { projectRoot: _projectRoot, utilities: _utilities, ...manifest } = project;
  await writeFile(temporary, `${JSON.stringify(manifest, null, 2)}\n`);
  await rename(temporary, target);
}

export function visibleLedger(project: ProjectManifest, stage: StageName): Record<string, unknown> {
  if (stage === "interfaces") {
    return {
      utilities: project.utilities ?? [],
      routes: project.routes.map(({ name, description, execution, services }) => ({
        name, description, execution,
        contract: analyzePipelineContracts(services, project.beans.filter(
          (bean) => bean.category === "service",
        )),
      })),
    };
  }
  const categories = stage === "models"
    ? []
    : stage === "providers"
      ? ["model"]
      : stage === "services"
        ? ["model", "provider"]
        : ["service"];
  return {
    utilities: project.utilities ?? [],
    beans: project.beans
      .filter((bean) => categories.includes(bean.category))
      .filter((bean) => !(stage === "services" && bean.id === "PipelineRunner")),
  };
}

export function applyComponents(
  project: ProjectManifest,
  stage: Extract<StageName, "models" | "providers" | "services">,
  components: ComponentPlan[],
): void {
  const category = stage.slice(0, -1) as ProjectBean["category"];
  for (const component of components) {
    const bean: ProjectBean = {
      ...component,
      category,
      file: `src/${stage}/${basename(component.file)}`,
    };
    const index = project.beans.findIndex((item) => item.id === bean.id);
    if (index >= 0) project.beans[index] = bean;
    else project.beans.push(bean);
  }
}

export async function ensureProjectDirectories(projectRoot: string): Promise<void> {
  await Promise.all([
    "models", "providers", "services", "pipelines", "interfaces",
  ].map((directory) => mkdir(resolve(projectRoot, "src", directory), { recursive: true })));
}
