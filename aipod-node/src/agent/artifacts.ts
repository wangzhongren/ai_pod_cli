import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { basename, dirname, relative, resolve, sep } from "node:path";
import ts from "typescript";

import { validateServiceSource } from "../contracts.js";
import { visibleLedger, type ProjectManifest } from "./project.js";
import type {
  ComponentPlan, InterfaceArtifactPlan, InterfacePlan, ModelClient, RoutePlan,
  StageName, StagePlan,
} from "./types.js";

export interface Artifact {
  path: string;
  content: string;
}

function safePath(projectRoot: string, path: string): string {
  const target = resolve(projectRoot, path);
  const local = relative(resolve(projectRoot), target);
  if (!local || local.startsWith("..") || local.includes(`${sep}..${sep}`)) {
    throw new Error(`Artifact path escapes project: ${path}`);
  }
  return target;
}

export function validateTypeScript(content: string, file: string): string[] {
  const transpiled = ts.transpileModule(content, {
    fileName: file,
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.NodeNext,
      strict: true,
    },
    reportDiagnostics: true,
  });
  return (transpiled.diagnostics ?? []).map((diagnostic) =>
    ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")
  );
}

function validateComponentSource(
  stage: Extract<StageName, "models" | "providers" | "services">,
  plan: ComponentPlan,
  content: string,
): string[] {
  const errors = validateTypeScript(content, plan.file);
  if (!new RegExp(`\\b${plan.id}\\b`).test(content)) errors.push(`Source does not export '${plan.id}'`);
  if (stage === "models" && !/export\s+(?:interface|type|class)\s+/.test(content)) {
    errors.push("Model must export an interface, type, or class");
  }
  if (stage === "providers" && !new RegExp(`export\\s+class\\s+${plan.id}\\b`).test(content)) {
    errors.push(`Provider must export class ${plan.id}`);
  }
  if (stage === "services") {
    if (!new RegExp(`export\\s+class\\s+${plan.id}\\b`).test(content)) {
      errors.push(`Service must export class ${plan.id}`);
    }
    if (!/\bexecute\s*\(/.test(content)) errors.push("Service must implement execute(context)");
    errors.push(...validateServiceSource(content));
  }
  return [...new Set(errors)];
}

async function generateComponent(
  client: ModelClient,
  stage: Extract<StageName, "models" | "providers" | "services">,
  plan: ComponentPlan,
  project: ProjectManifest,
): Promise<Artifact> {
  const directory = stage;
  const path = `src/${directory}/${basename(plan.file)}`;
  const visibility = JSON.stringify(visibleLedger(project, stage), null, 2);
  let evidence: string[] = [];
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const rules = stage === "services"
      ? "The Service cannot import, inject, instantiate, resolve, or execute another Service or PipelineRunner. It may use only its Contract, type-only Model imports, and declared Provider dependencies. Export one class whose optional constructor receives one dependency object keyed by exact Provider IDs and whose execute(context) returns its declared outputs."
      : stage === "models"
        ? "Generate a pure typed data declaration with no dependency injection or runtime orchestration."
        : "Generate one infrastructure Provider class. It must not orchestrate Services. Its optional constructor receives one dependency object keyed by exact Provider IDs.";
    const raw = await client.complete(
      `GENERATE_COMPONENT:${stage}:${plan.id}\nGenerate exactly one TypeScript file. ${rules}\nVisible frozen ledger:\n${visibility}\nReturn {"content":"complete source"}.`,
      `Plan:\n${JSON.stringify(plan, null, 2)}\nValidation evidence from the previous attempt:\n${JSON.stringify(evidence)}`,
    );
    const content = String(raw.content ?? raw.code ?? "");
    evidence = validateComponentSource(stage, plan, content);
    if (!evidence.length) return { path, content };
  }
  throw new Error(`Artifact '${path}' failed validation: ${evidence.join("; ")}`);
}

function pipelineSource(route: RoutePlan): string {
  const refs = route.services.map((id) => `service(container, ${JSON.stringify(id)})`);
  const sequenceSource = `sequence(${refs.join(", ")})`;
  let expression = sequenceSource;
  if (route.execution.mode === "parallel") {
    expression = `parallel([${refs.join(", ")}], ${JSON.stringify({
      concurrency: route.execution.concurrency,
      merge: route.execution.merge,
    })})`;
  } else if (route.execution.mode === "repeat") {
    expression = `repeat(${sequenceSource}, ${JSON.stringify({
      untilField: route.execution.untilField,
      maxIterationsField: route.execution.maxIterationsField,
      outputField: route.execution.outputField,
    })})`;
  }
  return `import { parallel, repeat, sequence, service, type Container, type RouteDefinition } from "aipod-node";\n\nexport function createPipeline(container: Container) {\n  return ${expression};\n}\n\nexport function createRoute(container: Container): RouteDefinition {\n  return {\n    name: ${JSON.stringify(route.name)},\n    description: ${JSON.stringify(route.description)},\n    pipeline: createPipeline(container),\n  };\n}\n`;
}

function interfaceSource(plan: InterfacePlan): string {
  return `import type { PipelineRunner } from "aipod-node";\n\nexport class ${plan.name}Adapter {\n  constructor(private readonly runner: PipelineRunner) {}\n\n  requiredRoutes(): string[] {\n    return [${JSON.stringify(plan.route)}];\n  }\n\n  async start(payload: Record<string, unknown> = {}) {\n    return this.runner.run(${JSON.stringify(plan.route)}, payload);\n  }\n}\n`;
}

export function validateArtifactContent(path: string, content: string): string[] {
  if (!content.trim()) return ["Artifact is empty"];
  if (path.endsWith(".ts")) return validateTypeScript(content, path);
  if (path.endsWith(".json")) {
    try { JSON.parse(content); return []; } catch (error) {
      return [`Invalid JSON: ${error instanceof Error ? error.message : String(error)}`];
    }
  }
  if (content.includes("\0")) return ["Artifact contains a null byte"];
  if (path.endsWith(".sh") && !content.startsWith("#!")) {
    return ["Shell Artifact must start with a shebang"];
  }
  return [];
}

async function generateInterfaceArtifact(
  client: ModelClient,
  owner: InterfacePlan,
  artifact: InterfaceArtifactPlan,
  project: ProjectManifest,
): Promise<Artifact> {
  let evidence: string[] = [];
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const raw = await client.complete(
      `GENERATE_INTERFACE_ARTIFACT:${owner.name}:${artifact.path}\nGenerate exactly one ${artifact.format} Artifact for an AIPod Node Interface. Keep it inside the declared path. It may use only the Interface's frozen route; never import Services or project internals. Installers must use the active Node executable and establish the project root. Return {"content":"complete content"}.`,
      `Interface:\n${JSON.stringify(owner, null, 2)}\nArtifact:\n${JSON.stringify(artifact, null, 2)}\nVisible routes:\n${JSON.stringify(project.routes.map(({ name, description }) => ({ name, description })))}\nPrevious validation evidence:\n${JSON.stringify(evidence)}`,
    );
    const content = String(raw.content ?? raw.code ?? "");
    evidence = validateArtifactContent(artifact.path, content);
    if (!evidence.length) return { path: artifact.path, content };
  }
  throw new Error(`${artifact.path} failed validation: ${evidence.join("; ")}`);
}

export async function generateArtifacts(
  client: ModelClient,
  stage: StageName,
  plan: StagePlan,
  project: ProjectManifest,
): Promise<Artifact[]> {
  if (stage === "models" || stage === "providers" || stage === "services") {
    return Promise.all((plan.components ?? []).map((item) =>
      generateComponent(client, stage, item, project)
    ));
  }
  if (stage === "pipelines") {
    return (plan.routes ?? []).map((route) => ({
      path: `src/pipelines/${route.name}.ts`,
      content: pipelineSource(route),
    }));
  }
  const interfaces = plan.interfaces ?? [];
  const primary = interfaces.map((item) => ({
    path: `src/interfaces/${basename(item.file)}`,
    content: interfaceSource(item),
  }));
  const delivery = await Promise.all(interfaces.flatMap((item) =>
    (item.artifacts ?? []).map((artifact) =>
      generateInterfaceArtifact(client, item, artifact, project)
    )
  ));
  return [...primary, ...delivery];
}

export function validateArtifacts(artifacts: Artifact[]): string[] {
  return artifacts.flatMap((artifact) =>
    validateArtifactContent(artifact.path, artifact.content)
      .map((error) => `${artifact.path}: ${error}`)
  );
}

export async function commitArtifacts(
  projectRoot: string,
  stage: StageName,
  artifacts: Artifact[],
): Promise<void> {
  const staging = resolve(projectRoot, ".aipod", "staging", stage);
  await rm(staging, { recursive: true, force: true });
  for (const artifact of artifacts) {
    const staged = safePath(staging, artifact.path);
    await mkdir(dirname(staged), { recursive: true });
    await writeFile(staged, artifact.content);
  }
  for (const artifact of artifacts) {
    const staged = safePath(staging, artifact.path);
    const target = safePath(projectRoot, artifact.path);
    await mkdir(dirname(target), { recursive: true });
    await rename(staged, target);
  }
  await rm(staging, { recursive: true, force: true });
}

export async function verifyCommittedArtifact(projectRoot: string, path: string): Promise<string[]> {
  try {
    const content = await readFile(safePath(projectRoot, path), "utf8");
    return validateArtifactContent(path, content);
  } catch (error) {
    return [error instanceof Error ? error.message : String(error)];
  }
}
