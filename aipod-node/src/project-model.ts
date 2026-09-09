import { access, readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { validateServiceSource } from "./contracts.js";
import { analyzePipelineContracts } from "./contracts.js";
import { loadProject } from "./agent/project.js";
import { loadState } from "./agent/state.js";
import { formatSemanticDiagnostic, typeCheckProject } from "./semantic-check.js";

export interface ProjectIssue {
  code: string;
  message: string;
  target?: string;
}

const exists = async (path: string) => {
  try { await access(path); return true; } catch { return false; }
};

export async function inspectProject(projectRoot: string): Promise<Record<string, unknown>> {
  const project = await loadProject(projectRoot);
  const issues: ProjectIssue[] = [];
  const categories = new Map(project.beans.map((bean) => [bean.id, bean.category]));
  for (const bean of project.beans) {
    if (!bean.file.startsWith("aipod:") && !await exists(resolve(projectRoot, bean.file))) {
      issues.push({ code: "missing_bean_file", target: bean.id, message: `Missing ${bean.file}` });
    }
    for (const dependency of bean.dependencies) {
      const category = categories.get(dependency);
      if (!category && dependency !== "ConfigStore") {
        issues.push({ code: "unknown_dependency", target: bean.id, message: `Unknown '${dependency}'` });
      }
      if (bean.category === "service" && (category === "service" || dependency === "PipelineRunner")) {
        issues.push({
          code: "service_visibility", target: bean.id,
          message: `Service cannot see '${dependency}'`,
        });
      }
    }
    if (bean.category === "service" && !bean.file.startsWith("aipod:") && await exists(resolve(projectRoot, bean.file))) {
      const source = await readFile(resolve(projectRoot, bean.file), "utf8");
      issues.push(...validateServiceSource(source).map((message) => ({
        code: "service_source_visibility", target: bean.id, message,
      })));
    }
  }
  const services = new Set(project.beans.filter((bean) => bean.category === "service").map((bean) => bean.id));
  for (const route of project.routes) {
    if (!await exists(resolve(projectRoot, route.file))) {
      issues.push({ code: "missing_route_file", target: route.name, message: `Missing ${route.file}` });
    }
    for (const service of route.services) {
      if (!services.has(service)) {
        issues.push({ code: "unknown_route_service", target: route.name, message: `Unknown '${service}'` });
      }
    }
    const contract = analyzePipelineContracts(
      route.services,
      project.beans.filter((bean) => bean.category === "service"),
    );
    issues.push(...contract.issues.map((issue) => ({
      code: issue.code, target: route.name, message: issue.message,
    })));
  }
  const routes = new Set(project.routes.map((route) => route.name));
  for (const item of project.interfaces) {
    if (!routes.has(item.route)) {
      issues.push({ code: "unknown_interface_route", target: item.name, message: `Unknown '${item.route}'` });
    }
    if (!await exists(resolve(projectRoot, item.file))) {
      issues.push({ code: "missing_interface_file", target: item.name, message: `Missing ${item.file}` });
    }
    for (const artifact of item.artifacts ?? []) {
      if (!await exists(resolve(projectRoot, artifact.path))) {
        issues.push({
          code: "missing_interface_artifact", target: item.name,
          message: `Missing ${artifact.path}`,
        });
      }
    }
  }
  issues.push(...(await typeCheckProject(projectRoot)).map((diagnostic) => ({
    code: `typescript_${diagnostic.code}`,
    ...(diagnostic.file ? { target: diagnostic.file } : {}),
    message: formatSemanticDiagnostic(diagnostic),
  })));
  let agent: unknown = null;
  try {
    const raw = JSON.parse(await readFile(resolve(projectRoot, ".aipod", "plan.json"), "utf8")) as { objective?: string };
    if (raw.objective) agent = await loadState(projectRoot, raw.objective);
  } catch {
    agent = null;
  }
  return {
    projectRoot: resolve(projectRoot),
    summary: {
      models: project.beans.filter((bean) => bean.category === "model").length,
      providers: project.beans.filter((bean) => bean.category === "provider").length,
      services: project.beans.filter((bean) => bean.category === "service").length,
      routes: project.routes.length,
      interfaces: project.interfaces.length,
    },
    beans: project.beans,
    routes: project.routes.map((route) => ({
      ...route,
      contract: analyzePipelineContracts(
        route.services,
        project.beans.filter((bean) => bean.category === "service"),
      ),
    })),
    interfaces: project.interfaces,
    validation: { valid: issues.length === 0, issues },
    agent,
  };
}
