import type { ProjectManifest } from "./project.js";
import { visibleLedger } from "./project.js";
import type {
  ComponentPlan, InterfacePlan, ModelClient, RoutePlan, StageName, StagePlan,
} from "./types.js";

const identifier = /^[A-Za-z_$][\w$]*$/;
const fileName = /^[a-z0-9][a-z0-9_-]*\.ts$/;

const publicConfiguration = (value: unknown, key = ""): unknown => {
  if (/key|secret|token|password|authorization/i.test(key)) return "[REDACTED]";
  if (Array.isArray(value)) return value.map((item) => publicConfiguration(item));
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value).map(([name, item]) => [name, publicConfiguration(item, name)]),
    );
  }
  return value;
};

function component(value: unknown): ComponentPlan {
  const item = value as Partial<ComponentPlan>;
  return {
    id: String(item.id ?? ""),
    description: String(item.description ?? ""),
    file: String(item.file ?? ""),
    dependencies: Array.isArray(item.dependencies) ? item.dependencies.map(String) : [],
    inputs: typeof item.inputs === "object" && item.inputs ? item.inputs : {},
    outputs: typeof item.outputs === "object" && item.outputs ? item.outputs : {},
  };
}

function route(value: unknown): RoutePlan {
  const item = value as Partial<RoutePlan>;
  const execution = item.execution ?? { mode: "sequential" };
  return {
    name: String(item.name ?? ""),
    description: String(item.description ?? ""),
    services: Array.isArray(item.services) ? item.services.map(String) : [],
    execution: {
      mode: execution.mode ?? "sequential",
      ...(execution.untilField ? { untilField: String(execution.untilField) } : {}),
      ...(execution.maxIterationsField ? { maxIterationsField: String(execution.maxIterationsField) } : {}),
      ...(execution.outputField ? { outputField: String(execution.outputField) } : {}),
      ...(execution.concurrency ? { concurrency: Number(execution.concurrency) } : {}),
      ...(execution.merge ? { merge: execution.merge } : {}),
    },
  };
}

function interfacePlan(value: unknown): InterfacePlan {
  const item = value as Partial<InterfacePlan>;
  return {
    name: String(item.name ?? ""),
    description: String(item.description ?? ""),
    file: String(item.file ?? ""),
    route: String(item.route ?? ""),
    kind: item.kind ?? "cli",
    artifacts: Array.isArray(item.artifacts) ? item.artifacts.map((artifact) => ({
      path: String(artifact.path ?? ""),
      role: artifact.role ?? "resource",
      format: artifact.format ?? "text",
      instruction: String(artifact.instruction ?? ""),
    })) : [],
    lifecycle: {
      ...(item.lifecycle?.run ? { run: item.lifecycle.run.map(String) } : {}),
      ...(item.lifecycle?.install ? { install: item.lifecycle.install.map(String) } : {}),
      ...(item.lifecycle?.uninstall ? { uninstall: item.lifecycle.uninstall.map(String) } : {}),
    },
    permissions: Array.isArray(item.permissions) ? item.permissions.map(String) : [],
    verify: Array.isArray(item.verify) ? item.verify.map((check) => ({
      name: String(check.name ?? ""),
      command: Array.isArray(check.command) ? check.command.map(String) : [],
      timeoutMs: Number(check.timeoutMs ?? 30_000),
      required: check.required ?? true,
    })) : [],
  };
}

export function normalizeStagePlan(stage: StageName, raw: Record<string, unknown>): StagePlan {
  const base = { summary: String(raw.summary ?? "") };
  if (["models", "providers", "services"].includes(stage)) {
    return { ...base, components: Array.isArray(raw.components) ? raw.components.map(component) : [] };
  }
  if (stage === "pipelines") {
    return { ...base, routes: Array.isArray(raw.routes) ? raw.routes.map(route) : [] };
  }
  return { ...base, interfaces: Array.isArray(raw.interfaces) ? raw.interfaces.map(interfacePlan) : [] };
}

export function validateStagePlan(
  stage: StageName,
  plan: StagePlan,
  project: ProjectManifest,
): string[] {
  const errors: string[] = [];
  if (["models", "providers", "services"].includes(stage)) {
    const components = plan.components ?? [];
    const ids = new Set<string>();
    const known = new Map(project.beans.map((bean) => [bean.id, bean.category]));
    for (const item of components) {
      if (!identifier.test(item.id)) errors.push(`Invalid component ID '${item.id}'`);
      if (!fileName.test(item.file)) errors.push(`Invalid component file '${item.file}'`);
      if (ids.has(item.id)) errors.push(`Duplicate component '${item.id}'`);
      ids.add(item.id);
      if (stage === "models" && item.dependencies.length) errors.push(`Model '${item.id}' cannot have dependencies`);
      for (const dependency of item.dependencies) {
        const category = known.get(dependency);
        if (!category) errors.push(`'${item.id}' references unknown dependency '${dependency}'`);
        if (stage === "services" && (category === "service" || dependency === "PipelineRunner")) {
          errors.push(`Service '${item.id}' cannot see '${dependency}'`);
        }
      }
    }
  } else if (stage === "pipelines") {
    const serviceIds = new Set(project.beans.filter((bean) => bean.category === "service").map((bean) => bean.id));
    for (const item of plan.routes ?? []) {
      if (!identifier.test(item.name)) errors.push(`Invalid route '${item.name}'`);
      if (!item.services.length) errors.push(`Route '${item.name}' has no Services`);
      for (const service of item.services) {
        if (!serviceIds.has(service)) errors.push(`Route '${item.name}' references unknown Service '${service}'`);
      }
      if (item.execution.mode === "repeat" && !item.execution.untilField && !item.execution.maxIterationsField) {
        errors.push(`Repeat route '${item.name}' needs untilField or maxIterationsField`);
      }
    }
  } else {
    const routes = new Set(project.routes.map((item) => item.name));
    for (const item of plan.interfaces ?? []) {
      if (!identifier.test(item.name)) errors.push(`Invalid Interface '${item.name}'`);
      if (!fileName.test(item.file)) errors.push(`Invalid Interface file '${item.file}'`);
      if (!routes.has(item.route)) errors.push(`Interface '${item.name}' references unknown route '${item.route}'`);
      const prefix = `interfaces/${item.name}/`;
      for (const artifact of item.artifacts ?? []) {
        if (!artifact.path.startsWith(prefix) || artifact.path.includes("..")) {
          errors.push(`Interface Artifact must remain inside '${prefix}'`);
        }
      }
      for (const check of item.verify ?? []) {
        if (!check.name || !check.command.length || check.timeoutMs < 1) {
          errors.push(`Interface '${item.name}' has an invalid verification check`);
        }
      }
    }
  }
  return errors;
}

export async function planStage(
  client: ModelClient,
  stage: StageName,
  objective: string,
  project: ProjectManifest,
  evidence: string[] = [],
  configuration: Record<string, unknown> = {},
): Promise<StagePlan> {
  const visibility = JSON.stringify(visibleLedger(project, stage), null, 2);
  const rules = stage === "services"
    ? "A Service sees only Contracts, Models, and Providers. dependencies may contain Provider IDs only. Never mention, import, inject, instantiate, or call another Service or PipelineRunner."
    : stage === "interfaces"
      ? "An Interface sees route names and public descriptions only. It never imports Services."
      : "Use only IDs visible in the supplied frozen ledger.";
  const shape = ["models", "providers", "services"].includes(stage)
    ? '{"summary":"...","components":[{"id":"PascalCase","file":"lowercase.ts","description":"...","dependencies":[],"inputs":{},"outputs":{}}]}'
    : stage === "pipelines"
      ? '{"summary":"...","routes":[{"name":"routeName","description":"...","services":["ServiceId"],"execution":{"mode":"sequential|parallel|repeat"}}]}'
      : '{"summary":"...","interfaces":[{"name":"appCli","file":"app-cli.ts","description":"...","route":"routeName","kind":"cli|web|desktop|worker|consumer","artifacts":[{"path":"interfaces/appCli/install.sh","role":"installer|uninstaller|adapter_module|metadata|resource","format":"shell|json|typescript|text","instruction":"..."}],"lifecycle":{"install":["sh","interfaces/appCli/install.sh"],"uninstall":[]},"permissions":[],"verify":[{"name":"smoke","command":["node","--version"],"timeoutMs":30000,"required":true}]}]}';
  const raw = await client.complete(
    `PLAN_STAGE:${stage}\nYou plan exactly one AIPod Node stage. ${rules}\nFrozen visible ledger:\n${visibility}\nAvailable shared project configuration:\n${JSON.stringify(publicConfiguration(configuration), null, 2)}\nReturn strict JSON shaped as ${shape}`,
    `Objective:\n${objective}\nPrevious public validation evidence:\n${JSON.stringify(evidence)}`,
  );
  return normalizeStagePlan(stage, raw);
}
