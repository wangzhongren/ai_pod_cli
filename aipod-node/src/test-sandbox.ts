import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

import { loadProject, type ProjectManifest } from "./agent/project.js";
import { ConfigStore } from "./config-store.js";
import { Container, type BeanDefinition } from "./container.js";
import { PipelineContext } from "./context.js";
import { compileProjectSources } from "./loader.js";
import { service } from "./pipeline.js";
import { ModelRepository } from "./repository.js";
import { typeCheckProject, formatSemanticDiagnostic } from "./semantic-check.js";
import type { Result } from "./result.js";
export interface TestAssertions {
  ok(value: unknown, message?: string): void;
  equal(actual: unknown, expected: unknown, message?: string): void;
  notEqual(actual: unknown, expected: unknown, message?: string): void;
  deepEqual(actual: unknown, expected: unknown, message?: string): void;
  notDeepEqual(actual: unknown, expected: unknown, message?: string): void;
  match(value: string, expected: RegExp, message?: string): void;
  throws(operation: () => unknown, expected?: RegExp | object, message?: string): void;
  rejects(operation: (() => Promise<unknown>) | Promise<unknown>, expected?: RegExp | object, message?: string): Promise<void>;
}
export interface ComponentTestCase {
  name: string;
  config?: Record<string, unknown>;
  providers?: Record<string, unknown>;
  run(sandbox: TestSandbox, assertions: TestAssertions): unknown | Promise<unknown>;
}
export function defineComponentTests(cases: ComponentTestCase[]): readonly ComponentTestCase[] { return cases; }
export class TestSetupError extends Error {
  constructor(message: string) { super(message); this.name = "TestSetupError"; }
}

/** Explicit fixtures, real component calls, and an empty private data store per case. */
export class TestSandbox {
  readonly #repository: ModelRepository;
  readonly #project: ProjectManifest;
  readonly #container: Container;
  #targetCalls = 0;
  readonly #setupErrors: string[] = [];

  private constructor(readonly root: string, readonly target: string, project: ProjectManifest, container: Container, repository: ModelRepository) {
    this.#project = project; this.#container = container; this.#repository = repository;
    Object.freeze(this);
  }

  static async create(projectRoot: string, target: string, options: Pick<ComponentTestCase, "config" | "providers"> = {}, enterWorkspace?: (root: string) => void): Promise<TestSandbox> {
    const caseDirectory = resolve(projectRoot, ".aipod", "test-cases");
    await mkdir(caseDirectory, { recursive: true });
    const root = await mkdtemp(resolve(caseDirectory, "case-"));
    try {
      await cp(resolve(projectRoot, "src"), resolve(root, "src"), { recursive: true });
      const project = await loadProject(projectRoot);
      const selected = project.beans.find((bean) => bean.id === target);
      if (!selected) throw new Error(`Unknown tested component '${target}'`);
      await writeFile(resolve(root, "aipod.json"), JSON.stringify(project));
      await writeFile(resolve(root, "config.json"), JSON.stringify(options.config ?? {}));
      enterWorkspace?.(root);
      const compileErrors = await compileProjectSources(root);
      if (compileErrors.length) throw new Error(compileErrors.join("; "));
      const repository = new ModelRepository(root);
      const config = await new ConfigStore(root, "config.json").load();
      const dependencies = new Set<string>();
      const visit = (id: string): void => {
        for (const dependency of project.beans.find((bean) => bean.id === id)?.dependencies ?? []) {
          if (!dependencies.has(dependency)) { dependencies.add(dependency); visit(dependency); }
        }
      };
      visit(target);
      for (const id of Object.keys(options.providers ?? {})) {
        if (["ConfigStore", "ModelRepository"].includes(id) || id === target || !dependencies.has(id) || !project.beans.some((bean) => bean.id === id && bean.category === "provider")) {
          throw new TestSetupError(`Provider double '${id}' is not an overridable dependency; tested targets, Services and built-in storage/config cannot be replaced`);
        }
      }
      const definitions: BeanDefinition[] = [{ id: "ConfigStore", category: "provider", factory: () => options.providers?.ConfigStore ?? config },
        { id: "ModelRepository", category: "provider", factory: () => options.providers?.ModelRepository ?? repository }];
      for (const bean of project.beans.filter((item) => !["ConfigStore", "ModelRepository"].includes(item.id))) {
        if (bean.category === "model") { definitions.push({ id: bean.id, category: "model", factory: () => undefined }); continue; }
        if (bean.id !== target && !dependencies.has(bean.id)) continue;
        if (bean.file.startsWith("aipod:")) continue;
        if (Object.hasOwn(options.providers ?? {}, bean.id)) {
          definitions.push({ id: bean.id, category: bean.category, dependencies: [], factory: () => options.providers![bean.id] });
          continue;
        }
        const exported = await import(pathToFileURL(resolve(root, ".aipod/build", bean.file.replace(/^src\//, "").replace(/\.ts$/, ".js"))).href) as Record<string, unknown>;
        const Constructor = exported[bean.id] as new (dependencies: Readonly<Record<string, unknown>>) => unknown;
        if (typeof Constructor !== "function") throw new Error(`Missing real class '${bean.id}'`);
        definitions.push({ id: bean.id, category: bean.category, dependencies: bean.dependencies,
          inputs: bean.inputs, outputs: bean.outputs, factory: (injected) => new Constructor(injected) });
      }
      return new TestSandbox(root, target, project, new Container(definitions), repository);
    } catch (error) { await rm(root, { recursive: true, force: true }); throw error; }
  }

  get targetCalls(): number { return this.#targetCalls; }
  get setupErrors(): readonly string[] { return [...this.#setupErrors]; }

  async seed(collection: string, rows: Record<string, unknown>[]): Promise<void> {
    try {
      if (!Array.isArray(rows)) throw new Error("seed requires an explicit array of rows with IDs");
      for (const row of rows) await this.#repository.save(collection, row);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.#setupErrors.push(message); throw new TestSetupError(message);
    }
  }
  async rows(collection: string): Promise<Record<string, unknown>[]> { return this.#repository.list(collection); }
  async count(collection: string): Promise<number> { return (await this.rows(collection)).length; }
  async snapshot(): Promise<Record<string, unknown>> {
    try { return JSON.parse(await readFile(this.path(".aipod/data.json"), "utf8")) as Record<string, unknown>; }
    catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return {}; throw error; }
  }
  path(local: string): string {
    const target = resolve(this.root, local);
    const part = relative(this.root, target);
    if (isAbsolute(local) || local.includes("\\") || /^[A-Za-z]:/.test(local) || part === ".." || part.startsWith(`..${sep}`)) {
      this.#setupErrors.push("Test path must stay within the sandbox"); throw new TestSetupError("Test path must stay within the sandbox");
    }
    return target;
  }
  async writeFile(local: string, content: string): Promise<string> {
    const path = this.path(local);
    const relativePath = relative(this.root, path).replaceAll("\\", "/");
    if (["src", "tests", ".aipod"].some((directory) => relativePath === directory || relativePath.startsWith(`${directory}/`))
      || ["aipod.json", "config.json", "config.toml"].includes(relativePath)) {
      this.#setupErrors.push("Fixture files cannot replace source, tests, registry or configuration");
      throw new TestSetupError("Fixture files cannot replace source, tests, registry or configuration");
    }
    await mkdir(dirname(path), { recursive: true }); await writeFile(path, content, "utf8");
    return path;
  }
  async run(id: string, params: Record<string, unknown> = {}): Promise<{ result: Result; context: PipelineContext }> {
    const reference = service(this.#container, id);
    const context = new PipelineContext(params);
    try { return { result: await reference.execute(context), context }; }
    finally { if (id === this.target) this.#targetCalls += 1; }
  }
  async callProvider(id: string, method: string, args: unknown[] = []): Promise<unknown> {
    if (this.#container.definition(id).category !== "provider") throw new Error(`'${id}' is not a Provider`);
    const provider = this.#container.resolve<Record<string, (...args: unknown[]) => unknown>>(id);
    if (typeof provider[method] !== "function" || ["constructor", "__proto__"].includes(method)) throw new Error(`Unknown Provider method '${id}.${method}'`);
    try { return await provider[method]!.apply(provider, args); }
    finally { if (id === this.target) this.#targetCalls += 1; }
  }
  async model(id: string, data: Record<string, unknown>): Promise<Record<string, unknown>> {
    const bean = this.#project.beans.find((item) => item.id === id && item.category === "model");
    if (!bean) throw new Error(`Unknown Model '${id}'`);
    const source = await readFile(resolve(this.root, bean.file), "utf8");
    const runtimeClass = new RegExp(`export\\s+class\\s+${id}\\b`).test(source);
    const checkFile = "src/model-test-value.ts";
    const modelImport = `./${bean.file.replace(/^src\//, "").replace(/\.ts$/, ".js")}`;
    await writeFile(this.path(checkFile), runtimeClass
      ? `import { ${id} } from ${JSON.stringify(modelImport)}; const value = new ${id}(${JSON.stringify(data)});\n`
      : `import type { ${id} } from ${JSON.stringify(modelImport)}; const value: ${id} = ${JSON.stringify(data)};\n`);
    if (id === this.target) this.#targetCalls += 1;
    try {
      const errors = (await typeCheckProject(this.root, [checkFile])).map(formatSemanticDiagnostic);
      if (errors.length) throw new Error(errors.join("; "));
    } finally { await rm(this.path(checkFile), { force: true }); }
    if (!runtimeClass) return structuredClone(data);
    const exported = await import(pathToFileURL(resolve(this.root, ".aipod/build", bean.file.replace(/^src\//, "").replace(/\.ts$/, ".js"))).href) as Record<string, new (data: Record<string, unknown>) => Record<string, unknown>>;
    return new exported[id]!(data);
  }
  async close(): Promise<void> { await rm(this.root, { recursive: true, force: true }); }
}
Object.freeze(TestSandbox.prototype);

export const TEST_SANDBOX_API = `Import { defineComponentTests } from "aipod-node". Export default defineComponentTests([{name:"test_scenario", config:{explicit: "test settings"}, providers:{DependencyProvider:{method(){return explicitValue;}}}, async run(sandbox, assert){ ... }}]). config and providers are optional; defaults are EMPTY, never production configuration or synthesized users. Each case gets a fresh project and empty ModelRepository. Doubles may replace only declared dependency Providers, never the target or a Service.
SDK signatures: seed(collection:string, rows:Record<string,unknown>[]):Promise<void> (each row needs id); run(serviceId:string, params:Record<string,unknown>):Promise<{result: Success|Failure, context:PipelineContext}>; callProvider(id:string,method:string,args:unknown[]=[]):Promise<unknown>; model(id:string,data:Record<string,unknown>):Promise<Record<string,unknown>> (interfaces/type aliases checked by the real TS compiler; runtime classes must implement constructor(data)); rows(collection):Promise<Record<string,unknown>[]>; count(collection):Promise<number>; snapshot():Promise<Record<string,unknown>>; path(relativePath:string):string; writeFile(relativePath:string,content:string):Promise<string> (writes only fixture/data files, returns their sandbox path; cannot overwrite source/tests/registry/config). Built-in ConfigStore and ModelRepository cannot be replaced by doubles.
Success={status:"success",output:Record<string,unknown>,effects:[]}; Failure={status:"failure",error:{code,message,retryable,details},effects:[]}. run returns failures for tests to assert; expected denial is a valid passing test when explicitly asserted. callProvider/model may throw; await assert.rejects(()=>sandbox.callProvider(...), /expected message/). Assertions available: assert.ok, equal, notEqual, deepEqual, notDeepEqual, match, throws, rejects. Every planned scenario must call the real target and execute an assertion. Do not catch/ignore assertions, skip scenarios, mock the target, synthesize an admin, import application components directly or modify files/source. Use only this SDK; provider interfaces and required data must come from the frozen specification.`;
