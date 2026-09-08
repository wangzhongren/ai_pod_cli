import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { basename, dirname, relative, resolve, sep } from "node:path";
import ts from "typescript";

import { validateServiceSource } from "../contracts.js";
import { utilityImportIssues } from "../utilities.js";
import { visibleLedger, type ProjectManifest } from "./project.js";
import { decodeSourceArtifact, sourceArtifactInstruction } from "./source-codec.js";
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
  project: ProjectManifest,
): string[] {
  const errors = validateTypeScript(content, plan.file);
  errors.push(...utilityImportIssues(content, `src/${stage}/${basename(plan.file)}`, project.utilities ?? []));
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
  if (!client.completeText) throw new Error("ModelClient.completeText is required for XML-like source generation");
  let evidence: string[] = [];
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const rules = stage === "services"
      ? "The Service cannot import, inject, instantiate, resolve, or execute another Service or PipelineRunner. It may use its Contract, type-only Model imports, declared Provider dependencies and registered pure utility classes via normal imports. Export one class whose optional constructor receives one dependency object keyed by exact Provider IDs and whose execute(context) returns its declared outputs. Reuse relevant registered utility methods to keep this component focused; utilities are not DI dependencies."
      : stage === "models"
        ? "Generate a pure typed data declaration with no dependency injection or runtime orchestration."
        : "Generate one infrastructure Provider class. It must not orchestrate Services. Its optional constructor receives one dependency object keyed by exact Provider IDs.";
    const raw = await client.completeText(
      `GENERATE_COMPONENT:${stage}:${plan.id}\nGenerate exactly one TypeScript file. ${rules}\nRegistered utilities are public pure helpers: reuse matching static methods with normal relative imports, never as DI dependencies. Keep domain orchestration and IO in their existing layers.\nVisible frozen ledger:\n${visibility}\n${sourceArtifactInstruction(path)}`,
      `Plan:\n${JSON.stringify(plan, null, 2)}\nValidation evidence from the previous attempt:\n${JSON.stringify(evidence)}${stage === "services" ? `\nUse import type { PipelineContext } from "aipod-node" and execute(context: PipelineContext). Inside execute, create const ctx = context.typed(${JSON.stringify(plan.inputs)}, ${JSON.stringify(plan.outputs)}). Use ctx.get for declared inputs, ctx.set for declared outputs, and return ctx.output({...}) to check the complete output. Keep contracts literal for inferred field types; do not cast inputs or outputs to any.` : ""}`,
    );
    try {
      const artifact = decodeSourceArtifact(raw, path);
      evidence = validateComponentSource(stage, plan, artifact.content, project);
      if (!evidence.length) return artifact;
    } catch (error) {
      evidence = [error instanceof Error ? error.message : String(error)];
    }
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
  if (!client.completeText) throw new Error("ModelClient.completeText is required for XML-like source generation");
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const raw = await client.completeText(
      `GENERATE_INTERFACE_ARTIFACT:${owner.name}:${artifact.path}\nGenerate exactly one ${artifact.format} Artifact for an AIPod Node Interface. Keep it inside the declared path. It may use the Interface's frozen route and registered public pure utilities via normal imports; never import Services or private project internals. Installers must use the active Node executable and establish the project root. ${sourceArtifactInstruction(artifact.path)}`,
      `Interface:\n${JSON.stringify(owner, null, 2)}\nArtifact:\n${JSON.stringify(artifact, null, 2)}\nVisible routes:\n${JSON.stringify(project.routes.map(({ name, description }) => ({ name, description })))}\nRegistered public utilities:\n${JSON.stringify(project.utilities ?? [])}\nPrevious validation evidence:\n${JSON.stringify(evidence)}`,
    );
    try {
      const candidate = decodeSourceArtifact(raw, artifact.path);
      evidence = validateArtifactContent(artifact.path, candidate.content);
      if (artifact.format === "typescript" || artifact.format === "javascript") evidence.push(...utilityImportIssues(candidate.content, artifact.path, project.utilities ?? []));
      if (!evidence.length) return candidate;
    } catch (error) {
      evidence = [error instanceof Error ? error.message : String(error)];
    }
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
