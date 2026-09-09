import { mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { dirname, extname, relative, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

import { loadProject, type ProjectManifest } from "./agent/project.js";
import { ConfigStore } from "./config-store.js";
import { Container, type BeanDefinition } from "./container.js";
import { parallel, repeat, sequence, service, type ExecutionNode } from "./pipeline.js";
import { PipelineRunner } from "./runner.js";
import { ModelRepository } from "./repository.js";
import { writeRunTrace } from "./traces.js";
import { formatSemanticDiagnostic, typeCheckProject } from "./semantic-check.js";

async function walk(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const path = resolve(directory, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  }));
  return nested.flat();
}

const buildRoot = (projectRoot: string) => resolve(projectRoot, ".aipod", "build");

export async function compileProjectSources(projectRoot: string): Promise<string[]> {
  const sourceRoot = resolve(projectRoot, "src");
  const outputRoot = buildRoot(projectRoot);
  await rm(outputRoot, { recursive: true, force: true });
  await mkdir(outputRoot, { recursive: true });
  await writeFile(resolve(outputRoot, "package.json"), '{"type":"module"}\n');
  const errors: string[] = (await typeCheckProject(projectRoot)).map(formatSemanticDiagnostic);
  for (const source of (await walk(sourceRoot)).filter((path) => extname(path) === ".ts")) {
    const content = await readFile(source, "utf8");
    const output = ts.transpileModule(content, {
      fileName: source,
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.ES2022,
        sourceMap: true,
      },
      reportDiagnostics: true,
    });
    errors.push(...(output.diagnostics ?? []).map((diagnostic) =>
      `${relative(projectRoot, source)}: ${ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")}`
    ));
    const local = relative(sourceRoot, source).replace(/\.ts$/, ".js");
    const target = resolve(outputRoot, local);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, output.outputText);
  }
  return errors;
}

function compiledPath(projectRoot: string, sourceFile: string): string {
  const sourceRoot = resolve(projectRoot, "src");
  const source = resolve(projectRoot, sourceFile);
  const local = relative(sourceRoot, source);
  if (local.startsWith("..")) throw new Error(`Source is outside src/: ${sourceFile}`);
  return resolve(buildRoot(projectRoot), local.replace(/\.ts$/, ".js"));
}

async function importExport(projectRoot: string, file: string, id: string): Promise<unknown> {
  const path = compiledPath(projectRoot, file);
  const modified = (await stat(path)).mtimeMs;
  const module = await import(`${pathToFileURL(path).href}?v=${modified}`) as Record<string, unknown>;
  const exported = module[id];
  if (typeof exported !== "function") throw new Error(`${file} does not export class '${id}'`);
  return exported;
}

export async function loadContainer(
  projectRoot: string,
  project?: ProjectManifest,
): Promise<Container> {
  const manifest = project ?? await loadProject(projectRoot);
  const compileErrors = await compileProjectSources(projectRoot);
  if (compileErrors.length) throw new Error(`TypeScript compilation failed: ${compileErrors.join("; ")}`);
  const configStore = await new ConfigStore(projectRoot).load();
  const modelRepository = new ModelRepository(projectRoot);
  const definitions: BeanDefinition[] = [{
    id: "ConfigStore", category: "provider", dependencies: [],
    factory: () => configStore,
  }, {
    id: "ModelRepository", category: "provider", dependencies: [],
    factory: () => modelRepository,
  }];
  for (const bean of manifest.beans.filter(
    (item) => !["ConfigStore", "ModelRepository"].includes(item.id),
  )) {
    if (bean.file.startsWith("aipod:")) continue;
    if (bean.category === "model") {
      definitions.push({
        id: bean.id,
        category: "model",
        dependencies: [],
        inputs: bean.inputs,
        outputs: bean.outputs,
        factory: () => undefined,
      });
      continue;
    }
    const Constructor = await importExport(projectRoot, bean.file, bean.id) as new (
      dependencies?: Readonly<Record<string, unknown>>,
    ) => unknown;
    definitions.push({
      id: bean.id,
      category: bean.category,
      dependencies: bean.dependencies,
      inputs: bean.inputs,
      outputs: bean.outputs,
      factory: (dependencies) => new Constructor(dependencies),
    });
  }
  return new Container(definitions);
}

function routeNode(container: Container, route: ProjectManifest["routes"][number]): ExecutionNode {
  const refs = route.services.map((id) => service(container, id));
  if (route.execution.mode === "parallel") {
    return parallel(refs, {
      ...(route.execution.concurrency ? { concurrency: route.execution.concurrency } : {}),
      ...(route.execution.merge ? { merge: route.execution.merge } : {}),
    });
  }
  const body = sequence(...refs);
  if (route.execution.mode === "repeat") {
    return repeat(body, {
      ...(route.execution.untilField ? { untilField: route.execution.untilField } : {}),
      ...(route.execution.maxIterationsField ? {
        maxIterationsField: route.execution.maxIterationsField,
      } : {}),
      ...(route.execution.outputField ? { outputField: route.execution.outputField } : {}),
    });
  }
  return body;
}

export async function loadRunner(projectRoot: string): Promise<PipelineRunner> {
  const project = await loadProject(projectRoot);
  const container = await loadContainer(projectRoot, project);
  return new PipelineRunner(project.routes.map((route) => ({
    name: route.name,
    description: route.description,
    pipeline: routeNode(container, route),
  })));
}

export async function runRoute(
  projectRoot: string,
  route: string,
  params: Record<string, unknown> = {},
): Promise<Record<string, unknown>> {
  const runner = await loadRunner(projectRoot);
  const started = performance.now();
  const execution = await runner.run(route, params);
  return writeRunTrace(
    projectRoot, route, params, execution.result, execution.context,
    performance.now() - started,
  );
}
