import { createHash, randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { cp, link, mkdir, mkdtemp, readFile, realpath, rename, rm, writeFile, readdir, lstat, symlink } from "node:fs/promises";
import { basename, dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";
import ts from "typescript";

import { typeCheckProject, formatSemanticDiagnostic } from "./semantic-check.js";

export interface UtilityCase {
  method: string;
  args: unknown[];
  kwargs?: Record<string, unknown>;
  expected?: unknown;
  raises?: string;
}
export interface UtilityMethod { name: string; signature: string }
export interface UtilityEntry {
  id: string;
  language: "typescript";
  path: string;
  symbol: string;
  description: string;
  methods: UtilityMethod[];
  sha256: string;
  cases: UtilityCase[];
}
export interface UtilityRegistry { schema_version: 1; utilities: UtilityEntry[] }
export const utilityPath = (id: string) => `src/utils/${id}.ts`;
const hash = (source: string) => createHash("sha256").update(source).digest("hex");
const identifier = /^[A-Za-z_$][\w$]*$/;
const record = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);

async function inside(root: string, path: string, create = false): Promise<string> {
  if (isAbsolute(path) || /^[A-Za-z]:/.test(path) || path.includes("\\") || path.includes("\0") || path.split("/").includes("..")) {
    throw new Error(`Utility path escapes project: ${path}`);
  }
  const actualRoot = await realpath(root);
  let directory = actualRoot;
  for (const part of path.split("/").slice(0, -1).filter((item) => item && item !== ".")) {
    directory = resolve(directory, part);
    if (create) await mkdir(directory).catch((error: NodeJS.ErrnoException) => { if (error.code !== "EEXIST") throw error; });
    directory = await realpath(directory);
    const local = relative(actualRoot, directory);
    if (!local || local === ".." || local.startsWith(`..${sep}`) || isAbsolute(local)) throw new Error(`Utility directory escapes project: ${path}`);
  }
  const target = resolve(directory, basename(path));
  try { if ((await lstat(target)).isSymbolicLink()) throw new Error(`Utility files cannot be symlinks: ${path}`); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  return target;
}

export async function loadUtilityRegistry(root: string): Promise<UtilityRegistry> {
  let raw: unknown;
  try { raw = JSON.parse(await readFile(await inside(root, "utility_registry.json"), "utf8")); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return { schema_version: 1, utilities: [] }; throw error; }
  if (!record(raw) || raw.schema_version !== 1 || !Array.isArray(raw.utilities)) throw new Error("Invalid utility_registry.json schema");
  const ids = new Set<string>();
  const paths = new Set<string>();
  for (const entry of raw.utilities) {
    if (!record(entry) || typeof entry.id !== "string" || !identifier.test(entry.id) || entry.language !== "typescript"
      || entry.symbol !== entry.id || typeof entry.path !== "string" || !/^src\/utils\/[A-Za-z0-9_$/-]+\.ts$/.test(entry.path)
      || typeof entry.description !== "string" || !entry.description.trim() || typeof entry.sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(entry.sha256) || !Array.isArray(entry.methods) || !Array.isArray(entry.cases)
      || ids.has(entry.id) || paths.has(entry.path)) throw new Error("Invalid or duplicated utility registry entry");
    ids.add(entry.id); paths.add(entry.path);
  }
  return raw as unknown as UtilityRegistry;
}

async function sourceFiles(directory: string): Promise<string[]> {
  try {
    const entries = await readdir(directory, { withFileTypes: true });
    return (await Promise.all(entries.filter((entry) => !entry.isSymbolicLink()).map((entry) => {
      const path = resolve(directory, entry.name);
      return entry.isDirectory() ? sourceFiles(path) : Promise.resolve(path.endsWith(".ts") ? [path] : []);
    }))).flat();
  } catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return []; throw error; }
}

function importsOf(source: string, file: string): string[] {
  const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
  const imports: string[] = [];
  const visit = (node: ts.Node): void => {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) imports.push(node.moduleSpecifier.text);
    if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument) && ts.isStringLiteral(node.argument.literal)) imports.push(node.argument.literal.text);
    if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(node.expression) && node.expression.text === "require"))) {
      const argument = node.arguments[0];
      if (argument && ts.isStringLiteral(argument)) imports.push(argument.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(ast);
  return imports;
}

export async function readUtility(root: string, id: string): Promise<UtilityEntry & { source: string; callers: string[] }> {
  root = await realpath(root);
  const registry = await loadUtilityRegistry(root);
  const entry = registry.utilities.find((item) => item.id === id);
  if (!entry) throw new Error(`Unknown utility '${id}'`);
  const target = await inside(root, entry.path);
  const source = await readFile(target, "utf8");
  if (hash(source) !== entry.sha256) throw new Error(`Utility '${id}' hash drift detected; reconcile it explicitly before reuse`);
  const methods = analyzeUtilitySource(source, entry.id, entry.path, registry.utilities);
  if (JSON.stringify(methods) !== JSON.stringify(entry.methods)) throw new Error(`Utility '${id}' method registry drift detected`);
  const callers: string[] = [];
  for (const path of await sourceFiles(resolve(root, "src"))) {
    if (path === target) continue;
    if (importsOf(await readFile(path, "utf8"), path).some((specifier) => specifier.startsWith(".")
      && resolve(dirname(path), specifier.replace(/\.js$/, ".ts")) === target)) {
      callers.push(relative(root, path).replaceAll("\\", "/"));
    }
  }
  return { ...entry, source, callers };
}

export async function listUtilities(root: string, query = ""): Promise<UtilityEntry[]> {
  const entries = (await loadUtilityRegistry(root)).utilities;
  const checked = await Promise.all(entries.map((entry) => readUtility(root, entry.id)));
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  return checked.filter((entry) => words.every((word) => `${entry.id} ${entry.description} ${entry.methods.map((method) => method.signature).join(" ")}`.toLowerCase().includes(word)))
    .map(({ source: _source, callers: _callers, ...entry }) => entry);
}

const pureImports: Record<string, Set<string>> = {
  "node:path": new Set(["basename", "dirname", "extname", "format", "isAbsolute", "join", "normalize", "parse", "sep", "delimiter"]),
  "node:util": new Set(["isDeepStrictEqual"]),
};
const forbidden = new Set(["process", "global", "globalThis", "window", "document", "require", "module", "eval", "Function", "Reflect", "Proxy", "console", "fetch", "XMLHttpRequest", "WebSocket", "Worker", "setTimeout", "setInterval", "setImmediate", "queueMicrotask", "Date", "performance", "crypto", "Atomics", "SharedArrayBuffer", "WebAssembly", "PipelineContext", "PipelineRunner", "Container", "OpenAI", "ConfigStore", "ModelRepository"]);

/** An ordinary static helper class. Runtime wiring and hidden side effects have no place here. */
export function analyzeUtilitySource(source: string, id: string, file: string, registered: UtilityEntry[]): UtilityMethod[] {
  const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
  const methods: UtilityMethod[] = [];
  const classes = ast.statements.filter(ts.isClassDeclaration);
  if (classes.length !== 1 || classes[0]!.name?.text !== id || !classes[0]!.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)
    || classes[0]!.heritageClauses?.length) throw new Error(`Utility must export exactly one standalone static class '${id}'`);
  for (const statement of ast.statements) {
    if (ts.isImportDeclaration(statement)) {
      if (!ts.isStringLiteral(statement.moduleSpecifier) || !statement.importClause || statement.importClause.name
        || !statement.importClause.namedBindings || !ts.isNamedImports(statement.importClause.namedBindings)) throw new Error("Utility imports must use explicit named symbols");
      const specifier = statement.moduleSpecifier.text;
      const names = statement.importClause.namedBindings.elements.map((item) => item.propertyName?.text ?? item.name.text);
      const dependency = registered.find((entry) => resolve(dirname(file), specifier.replace(/\.js$/, ".ts")) === resolve(entry.path));
      if (!(pureImports[specifier] && names.every((name) => pureImports[specifier]!.has(name)))
        && !(specifier.startsWith(".") && dependency && names.every((name) => name === dependency.symbol))) {
        throw new Error(`Utility cannot import '${specifier}'; only registered utilities and approved pure standard functions are allowed`);
      }
    } else if (!ts.isClassDeclaration(statement) && !ts.isInterfaceDeclaration(statement) && !ts.isTypeAliasDeclaration(statement)) {
      throw new Error("Utility files cannot contain top-level execution or mutable state");
    }
  }
  for (const member of classes[0]!.members) {
    if (!ts.isMethodDeclaration(member) || !member.body || !member.type || member.asteriskToken
      || !member.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.StaticKeyword)
      || member.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.AsyncKeyword)
      || !ts.isIdentifier(member.name) || member.parameters.some((parameter) => !parameter.type)) {
      throw new Error("Utilities contain synchronous static methods with explicit parameter and return types, no fields or constructors");
    }
    if (!member.modifiers.some((modifier) => [ts.SyntaxKind.PrivateKeyword, ts.SyntaxKind.ProtectedKeyword].includes(modifier.kind))) {
      methods.push({ name: member.name.text, signature: `${member.name.text}(${member.parameters.map((parameter) => parameter.getText(ast)).join(", ")}): ${member.type.getText(ast)}` });
    }
  }
  if (!methods.length || new Set(methods.map((method) => method.name)).size !== methods.length) throw new Error("Utility needs uniquely named public static methods");
  const visit = (node: ts.Node): void => {
    if (ts.isDecorator(node) || ts.isImportEqualsDeclaration(node) || ts.isWithStatement(node)
      || ts.isClassExpression(node) || (ts.isClassDeclaration(node) && node !== classes[0])
      || node.kind === ts.SyntaxKind.ThisKeyword || node.kind === ts.SyntaxKind.AnyKeyword
      || ts.isTypeAssertionExpression(node) || (ts.isAsExpression(node) && node.type.getText(ast) !== "const")
      || (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword)) throw new Error("Utility contains an unsafe runtime construct");
    if (ts.isIdentifier(node) && forbidden.has(node.text)) throw new Error(`Utility cannot access '${node.text}'`);
    if ((ts.isPropertyAccessExpression(node) && ["constructor", "prototype", "__proto__", "random"].includes(node.name.text))
      || (ts.isElementAccessExpression(node) && ts.isStringLiteral(node.argumentExpression)
        && ["constructor", "prototype", "__proto__", "random"].includes(node.argumentExpression.text))) throw new Error("Utility cannot access runtime prototypes or nondeterminism");
    ts.forEachChild(node, visit);
  };
  visit(ast);
  return methods;
}

function validateCases(cases: UtilityCase[], methods: UtilityMethod[]): void {
  if (!Array.isArray(cases) || !cases.length) throw new Error("Utility requires explicit method verification cases");
  for (const item of cases) {
    if (!record(item) || !methods.some((method) => method.name === item.method) || !Array.isArray(item.args)
      || (item.kwargs && (!record(item.kwargs) || Object.keys(item.kwargs).length))
      || (Object.hasOwn(item, "expected") === Object.hasOwn(item, "raises"))
      || (item.raises !== undefined && (typeof item.raises !== "string" || !identifier.test(item.raises)))) {
      throw new Error("Each Utility case needs a public method, JSON args, and exactly one expected or raises value; Node kwargs must be empty");
    }
    assertJson(item);
  }
  for (const method of methods) if (!cases.some((item) => item.method === method.name)) throw new Error(`Missing verification case for '${method.name}'`);
}

function assertJson(value: unknown, seen = new Set<object>()): void {
  if (value === null || typeof value === "string" || typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value))) return;
  if (typeof value !== "object" || value === null || seen.has(value)) throw new Error("Utility cases require finite, acyclic JSON data");
  seen.add(value);
  if (Array.isArray(value)) {
    if (Object.keys(value).length !== value.length || Object.keys(value).some((key, index) => key !== String(index))) throw new Error("Utility cases require dense JSON arrays");
    value.forEach((item) => assertJson(item, seen));
  } else {
    if (Object.getPrototypeOf(value) !== Object.prototype || Object.getOwnPropertySymbols(value).length) throw new Error("Utility cases require plain JSON objects");
    for (const field of Object.values(Object.getOwnPropertyDescriptors(value))) {
      if (!("value" in field) || !field.enumerable) throw new Error("Utility cases cannot contain accessors or hidden fields");
      assertJson(field.value, seen);
    }
  }
  seen.delete(value);
}

async function runCommand(root: string, command: string[], timeoutMs: number): Promise<void> {
  if (!command.length || command.some((item) => typeof item !== "string" || !item)) throw new Error("Verification command must be a nonempty argument array");
  await new Promise<void>((resolvePromise, reject) => {
    const child = spawn(command[0]!, command.slice(1), { cwd: root, stdio: ["ignore", "pipe", "pipe"], detached: process.platform !== "win32" });
    let output = "";
    const append = (chunk: Buffer) => { output = `${output}${chunk.toString()}`.slice(-8000); };
    child.stdout.on("data", append); child.stderr.on("data", append);
    const timer = setTimeout(() => {
      if (child.pid && process.platform !== "win32") { try { process.kill(-child.pid, "SIGKILL"); } catch { /* already exited */ } }
      else child.kill("SIGKILL");
      reject(new Error("Utility verification timed out"));
    }, timeoutMs);
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("exit", (code) => { clearTimeout(timer); if (code === 0) resolvePromise(); else reject(new Error(`Utility verification failed (${code}): ${output}`)); });
  });
}

async function verificationFingerprint(root: string): Promise<Record<string, string>> {
  const files: Record<string, string> = {};
  const walk = async (directory: string): Promise<void> => {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const file = resolve(directory, entry.name);
      const local = relative(root, file).replaceAll("\\", "/");
      if ([".git", "node_modules", "dist"].some((name) => local === name || local.startsWith(`${name}/`))
        || /^\.aipod\/(build|utility-cases)(\/|$)/.test(local)) continue;
      if (entry.isDirectory()) await walk(file);
      else if (local === "utility_registry.json" || /\.[cm]?[jt]sx?$/.test(entry.name)) {
        if (entry.isSymbolicLink()) throw new Error(`Candidate source cannot become a symlink: ${local}`);
        files[local] = hash(await readFile(file, "utf8"));
      }
    }
  };
  await walk(root);
  return Object.fromEntries(Object.entries(files).sort(([a], [b]) => a.localeCompare(b)));
}

async function checkCandidate(root: string, registry: UtilityRegistry, candidate: UtilityEntry, source: string, verificationCommand?: string[]): Promise<void> {
  const checkRoot = await mkdtemp(resolve(tmpdir(), "aipod-utility-check-"));
  try {
    // All application sources/config/resources/tests are verified in a private
    // candidate project. The real code and registry stay untouched until it passes.
    await cp(root, checkRoot, { recursive: true, dereference: true, filter: (file) => {
      const path = relative(root, file).replaceAll("\\", "/");
      return ![".git", "node_modules"].some((name) => path === name || path.startsWith(`${name}/`))
        && !/^\.aipod\/(utility-(?:check-|write\.)|build(?:\/|$))/.test(path);
    } });
    try { await symlink(await realpath(resolve(root, "node_modules")), resolve(checkRoot, "node_modules"), "dir"); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    const target = resolve(checkRoot, candidate.path);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, source);
    const entries = [...registry.utilities.filter((item) => item.id !== candidate.id), candidate];
    await writeFile(resolve(checkRoot, "utility_registry.json"), JSON.stringify({ schema_version: 1, utilities: entries }));
    const errors = (await typeCheckProject(checkRoot)).map(formatSemanticDiagnostic);
    if (errors.length) throw new Error(`Utility type verification failed: ${errors.join("; ")}`);
    const caseRoot = resolve(checkRoot, ".aipod", "utility-cases");
    await mkdir(caseRoot, { recursive: true });
    await writeFile(resolve(caseRoot, "package.json"), '{"type":"module"}\n');
    for (const entry of entries) {
      const tsPath = resolve(checkRoot, entry.path);
      const content = await readFile(tsPath, "utf8");
      const output = ts.transpileModule(content, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
      const outputPath = resolve(caseRoot, entry.path.replace(/\.ts$/, ".js"));
      await mkdir(dirname(outputPath), { recursive: true });
      await writeFile(outputPath, output);
    }
    for (const entry of entries) {
      await writeFile(resolve(caseRoot, "request.json"), JSON.stringify({ cases: entry.cases, symbol: entry.symbol, file: resolve(caseRoot, entry.path.replace(/\.ts$/, ".js")) }));
      await runCommand(checkRoot, [process.execPath, fileURLToPath(new URL("./utility-check-worker.js", import.meta.url)), resolve(caseRoot, "request.json")], 5000);
    }
    if (verificationCommand) {
      const beforeVerification = await verificationFingerprint(checkRoot);
      const candidateCommand = verificationCommand.map((argument, index) => {
        if (!index || !isAbsolute(argument)) return argument;
        const local = relative(root, argument);
        return local !== ".." && !local.startsWith(`..${sep}`) && !isAbsolute(local) ? resolve(checkRoot, local) : argument;
      });
      await runCommand(checkRoot, candidateCommand, 30000);
      if (JSON.stringify(await verificationFingerprint(checkRoot)) !== JSON.stringify(beforeVerification)) {
        throw new Error("Utility verification modified candidate sources or registry; verification cannot rewrite the code it is meant to check");
      }
    }
  } finally { await rm(checkRoot, { recursive: true, force: true }); }
}

export async function writeUtility(root: string, request: { id: string; description: string; source: string; cases: UtilityCase[] }, options: {
  expectedSha256?: string;
  verificationCommand?: string[];
} = {}): Promise<UtilityEntry> {
  root = await realpath(root);
  if (!identifier.test(request.id) || !request.description.trim() || !request.source.trim()) throw new Error("Utility id, description and source are required");
  await inside(root, ".aipod/utility-write.lock", true);
  const lock = resolve(root, ".aipod/utility-write.lock");
  await mkdir(lock).catch((error: NodeJS.ErrnoException) => { if (error.code === "EEXIST") throw new Error("Another utility write is in progress"); throw error; });
  let original: string | undefined;
  let changed = false;
  let target: string | undefined;
  const temporaryFiles: string[] = [];
  try {
    const registry = await loadUtilityRegistry(root);
    const existing = registry.utilities.find((entry) => entry.id === request.id);
    for (const entry of registry.utilities) await readUtility(root, entry.id);
    if (existing && !options.expectedSha256 && !options.verificationCommand
      && existing.sha256 === hash(request.source) && existing.description === request.description
      && JSON.stringify(existing.cases) === JSON.stringify(request.cases)) return existing;
    if (existing && (!options.expectedSha256 || !options.verificationCommand?.length)) throw new Error(`Utility '${request.id}' exists; updates require expectedSha256 and a verification command`);
    if (!existing && options.expectedSha256) throw new Error("Cannot update an unknown Utility");
    if (existing && options.expectedSha256 !== existing.sha256) throw new Error("Utility update hash mismatch");
    const path = existing?.path ?? utilityPath(request.id);
    target = await inside(root, path, true);
    try { original = await readFile(target, "utf8"); if (!existing) throw new Error(`Utility file already exists: ${path}`); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    const methods = analyzeUtilitySource(request.source, request.id, path, registry.utilities.filter((entry) => entry.id !== request.id));
    if (existing && existing.methods.some((method) => !methods.some((candidate) => candidate.name === method.name && candidate.signature === method.signature))) {
      throw new Error("Utility updates must preserve existing public method signatures; use a new utility ID for API changes");
    }
    const allCases = [...new Map([...(existing?.cases ?? []), ...request.cases].map((item) => [JSON.stringify(item), item])).values()];
    validateCases(allCases, methods);
    const entry: UtilityEntry = { id: request.id, language: "typescript", path, symbol: request.id,
      description: request.description, methods, sha256: hash(request.source), cases: allCases };
    await checkCandidate(root, registry, entry, request.source, options.verificationCommand);
    const latest = await loadUtilityRegistry(root);
    if (JSON.stringify(latest) !== JSON.stringify(registry)) throw new Error("Utility registry changed during verification; retry from the current registry");
    for (const item of registry.utilities) await readUtility(root, item.id);
    if (existing && hash(await readFile(target, "utf8")) !== options.expectedSha256) throw new Error("Utility source changed during verification; update cancelled");
    const staging = `${target}.${randomUUID()}.tmp`;
    temporaryFiles.push(staging);
    await writeFile(staging, request.source);
    if (existing) await rename(staging, target);
    else { await link(staging, target); await rm(staging, { force: true }); }
    changed = true;
    const next: UtilityRegistry = { schema_version: 1, utilities: [...registry.utilities.filter((item) => item.id !== request.id), entry] };
    const registryPath = await inside(root, "utility_registry.json");
    const temporary = `${registryPath}.${randomUUID()}.tmp`;
    temporaryFiles.push(temporary);
    await writeFile(temporary, `${JSON.stringify(next, null, 2)}\n`);
    await rename(temporary, registryPath);
    return entry;
  } catch (error) {
    if (changed && target) {
      if (original !== undefined) await writeFile(target, original);
      else await rm(target, { force: true });
    }
    throw error;
  } finally {
    await Promise.all(temporaryFiles.map((file) => rm(file, { force: true })));
    await rm(lock, { recursive: true, force: true });
  }
}

export async function validateUtilityImports(root: string): Promise<string[]> {
  const errors: string[] = [];
  const registry = await loadUtilityRegistry(root);
  for (const entry of registry.utilities) {
    try { await readUtility(root, entry.id); } catch (error) { errors.push(error instanceof Error ? error.message : String(error)); }
  }
  const registered = new Set(registry.utilities.map((entry) => resolve(root, entry.path)));
  for (const file of await sourceFiles(resolve(root, "src"))) {
    for (const specifier of importsOf(await readFile(file, "utf8"), file)) {
      if (specifier.startsWith(".")) {
        const target = resolve(dirname(file), specifier.replace(/\.js$/, ".ts"));
        if (relative(resolve(root, "src/utils"), target).split(sep)[0] !== ".." && !registered.has(target)) {
          errors.push(`${relative(root, file)} imports unregistered Utility '${specifier}'`);
        }
      }
    }
  }
  return errors;
}

export function utilityImportIssues(source: string, file: string, entries: UtilityEntry[]): string[] {
  const registered = new Set(entries.map((entry) => resolve(entry.path)));
  const utilityRoot = resolve("src/utils");
  return importsOf(source, file).flatMap((specifier) => {
    if (!specifier.startsWith(".")) return [];
    const target = resolve(dirname(file), specifier.replace(/\.js$/, ".ts"));
    const local = relative(utilityRoot, target);
    return local !== ".." && !local.startsWith(`..${sep}`) && !registered.has(target)
      ? [`${file} imports unregistered Utility '${specifier}'`] : [];
  });
}
