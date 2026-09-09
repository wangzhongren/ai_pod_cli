import { createHash, randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { cp, mkdir, mkdtemp, readFile, readdir, realpath, rename, rm, writeFile } from "node:fs/promises";
import { dirname, resolve, relative } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import { sourceArtifactInstruction, decodeSourceArtifact } from "./agent/source-codec.js";
import type { ComponentPlan, ModelClient, StageName } from "./agent/types.js";
import type { ProjectManifest } from "./agent/project.js";
import { TEST_SANDBOX_API } from "./test-sandbox.js";
import { formatSemanticDiagnostic, typeCheckProject } from "./semantic-check.js";
import { componentWorkerReadRoots } from "./runtime-dependencies.js";

export interface ComponentTestRecord {
  id: string;
  stage: "models" | "providers" | "services";
  path: string;
  sha256: string;
  planHash: string;
  scenarios: string[];
  revisionToken?: string;
}
export const digest = (source: string) => createHash("sha256").update(source).digest("hex");
const registryFile = (root: string) => resolve(root, ".aipod/component-tests.json");
export async function componentTestRecords(root: string): Promise<ComponentTestRecord[]> {
  try {
    const raw = JSON.parse(await readFile(registryFile(root), "utf8")) as { version: number; records: ComponentTestRecord[] };
    if (raw.version !== 1 || !Array.isArray(raw.records)) throw new Error("Invalid component test registry");
    const ids = new Set<string>();
    for (const record of raw.records) {
      if (!/^[A-Za-z_$][\w$]*$/.test(record.id) || ids.has(record.id) || !["models", "providers", "services"].includes(record.stage)
        || !/^tests\/components\/[A-Za-z0-9_$.-]+\.test\.ts$/.test(record.path) || record.path.includes("..")
        || !/^[a-f0-9]{64}$/.test(record.sha256) || !/^[a-f0-9]{64}$/.test(record.planHash)
        || !Array.isArray(record.scenarios) || !record.scenarios.length || record.scenarios.some((name) => !/^test_[A-Za-z0-9_]+$/.test(name))) {
        throw new Error("Invalid or duplicated frozen component test record");
      }
      ids.add(record.id);
    }
    return raw.records;
  } catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return []; throw error; }
}

export function validateComponentTestSource(source: string, scenarios: string[]): string[] {
  const errors: string[] = [];
  const ast = ts.createSourceFile("tests.ts", source, ts.ScriptTarget.Latest, true);
  for (const node of ast.statements) {
    if (ts.isImportDeclaration(node) && (!ts.isStringLiteral(node.moduleSpecifier) || node.moduleSpecifier.text !== "aipod-node")) {
      errors.push("Component tests must import only the public AIPod test SDK");
    }
  }
  let suite: ts.ArrayLiteralExpression | undefined;
  for (const node of ast.statements) {
    if (ts.isExportAssignment(node) && ts.isCallExpression(node.expression)
      && node.expression.expression.getText(ast) === "defineComponentTests"
      && node.expression.arguments[0] && ts.isArrayLiteralExpression(node.expression.arguments[0])) suite = node.expression.arguments[0];
  }
  if (!suite?.elements.length) errors.push("Tests must export default defineComponentTests with a nonempty literal case array");
  const names = suite?.elements.map((entry) => {
    if (!ts.isObjectLiteralExpression(entry)) return "";
    const name = entry.properties.find((property) => ts.isPropertyAssignment(property) && property.name.getText(ast) === "name");
    return name && ts.isPropertyAssignment(name) && ts.isStringLiteral(name.initializer) ? name.initializer.text : "";
  }) ?? [];
  if (names.length !== scenarios.length || new Set(names).size !== names.length || names.some((name) => !scenarios.includes(name))) {
    errors.push("Test cases must cover exactly the frozen planned scenario names");
  }
  const forbidden = new Set(["process", "globalThis", "global", "require", "eval", "Function", "Reflect", "Proxy", "TestSandbox"]);
  let calls = 0;
  let assertions = 0;
  const constant = (node: ts.Expression): boolean => ts.isLiteralExpression(node)
    || [ts.SyntaxKind.TrueKeyword, ts.SyntaxKind.FalseKeyword, ts.SyntaxKind.NullKeyword].includes(node.kind)
    || (ts.isIdentifier(node) && ["undefined", "NaN", "Infinity"].includes(node.text))
    || (ts.isParenthesizedExpression(node) && constant(node.expression))
    || (ts.isPrefixUnaryExpression(node) && constant(node.operand))
    || (ts.isBinaryExpression(node) && constant(node.left) && constant(node.right))
    || (ts.isArrayLiteralExpression(node) && node.elements.every((item) => ts.isExpression(item) && constant(item)))
    || (ts.isObjectLiteralExpression(node) && node.properties.every((item) => ts.isPropertyAssignment(item) && constant(item.initializer)));
  const visit = (node: ts.Node): void => {
    if (ts.isIdentifier(node) && forbidden.has(node.text)) errors.push(`Tests cannot access '${node.text}'`);
    if (ts.isImportEqualsDeclaration(node) || (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword)) errors.push("Tests cannot dynamically import implementations or runtime internals");
    if (ts.isPropertyAccessExpression(node) && ["skip", "only", "todo", "prototype", "__proto__", "constructor", "create", "close", "targetCalls"].includes(node.name.text)) errors.push(`Tests cannot bypass execution through '${node.name.text}'`);
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
      if (["run", "callProvider", "model"].includes(node.expression.name.text)) calls += 1;
      if (["ok", "equal", "notEqual", "deepEqual", "notDeepEqual", "match", "throws", "rejects"].includes(node.expression.name.text)) assertions += 1;
      if (["ok", "equal", "notEqual", "deepEqual", "notDeepEqual", "match"].includes(node.expression.name.text)
        && node.arguments.length && node.arguments.every(constant)) errors.push("Tests cannot use constant-only assertions as behavior evidence");
    }
    ts.forEachChild(node, visit);
  };
  visit(ast);
  if (!calls) errors.push("Tests must call the tested component through run, callProvider or model");
  if (!assertions) errors.push("Tests must contain behavior assertions");
  return [...new Set(errors)];
}

export async function prepareComponentTests(client: ModelClient, root: string, stage: ComponentTestRecord["stage"], plan: ComponentPlan, project: ProjectManifest, revisionToken?: string): Promise<ComponentTestRecord> {
  if (!plan.tests?.length) throw new Error(`Component '${plan.id}' has no explicit planned test scenarios`);
  const records = await componentTestRecords(root);
  const existing = records.find((record) => record.id === plan.id);
  const planHash = digest(JSON.stringify({ stage, ...plan }));
  if (existing) {
    if (digest(await readFile(resolve(root, existing.path), "utf8")) !== existing.sha256) throw new Error(`${plan.id}: frozen component test was modified`);
    if (existing.planHash === planHash) return existing;
    if (!revisionToken || existing.revisionToken === revisionToken) throw new Error(`${plan.id}: frozen test plan differs; an explicit test-plan revision is required`);
  }
  if (!client.completeText) throw new Error("completeText is required to generate component tests before implementation");
  const path = `tests/components/${plan.id}_${planHash.slice(0, 12)}.test.ts`;
  const scenarios = plan.tests.map((test) => test.name);
  let evidence: string[] = [];
  let source: string | undefined;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const text = await client.completeText(
      `GENERATE_COMPONENT_TESTS:${stage}:${plan.id}\nWrite executable tests BEFORE the implementation. The abstract contract and planned scenarios below are frozen. Test every named scenario with explicit inputs/rows/config and assertions against the real target. Never guess user roles or silently grant privileges. The test SDK contains no business permission policy. Do not import or implement the target. Tests become immutable before source generation.\n${TEST_SANDBOX_API}\n${sourceArtifactInstruction(path)}`,
      `Target specification:\n${JSON.stringify(plan)}\nAvailable dependencies:\n${JSON.stringify(project.beans.filter((bean) => plan.dependencies.includes(bean.id)))}\nTest-source validation errors:\n${JSON.stringify(evidence)}`,
    );
    try {
      const artifact = decodeSourceArtifact(text, path);
      evidence = validateComponentTestSource(artifact.content, scenarios);
      if (!evidence.length) {
        const check = await mkdtemp(resolve(tmpdir(), "aipod-test-source-"));
        try {
          await mkdir(resolve(check, "tests"));
          await writeFile(resolve(check, "tests/component.test.ts"), artifact.content);
          evidence = (await typeCheckProject(check, ["tests/component.test.ts"])).map(formatSemanticDiagnostic);
        } finally { await rm(check, { recursive: true, force: true }); }
      }
      if (!evidence.length) { source = artifact.content; break; }
    } catch (error) { evidence = [error instanceof Error ? error.message : String(error)]; }
  }
  if (source === undefined) throw new Error(`${plan.id}: test generation failed: ${evidence.join("; ")}`);
  const record: ComponentTestRecord = { id: plan.id, stage, path, sha256: digest(source), planHash, scenarios, ...(revisionToken ? { revisionToken } : {}) };
  await mkdir(dirname(resolve(root, path)), { recursive: true });
  // An unregistered existing test cannot be silently replaced either.
  await writeFile(resolve(root, path), source, { flag: "wx" });
  await mkdir(dirname(registryFile(root)), { recursive: true });
  const temp = `${registryFile(root)}.${randomUUID()}.tmp`;
  try {
    await writeFile(temp, JSON.stringify({ version: 1, records: [...records.filter((item) => item.id !== plan.id), record] }, null, 2));
    await rename(temp, registryFile(root));
  } catch (error) { await rm(resolve(root, path), { force: true }); throw error; }
  return record;
}

export async function sourceFingerprint(root: string): Promise<Record<string, string>> {
  const result: Record<string, string> = {};
  const walk = async (dir: string): Promise<void> => {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const path = resolve(dir, entry.name);
      if (entry.isDirectory()) await walk(path);
      else if (/\.[cm]?[jt]sx?$/.test(entry.name)) result[relative(root, path)] = digest(await readFile(path, "utf8"));
    }
  };
  for (const directory of ["src", "tests"]) {
    try { await walk(resolve(root, directory)); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  }
  for (const file of ["aipod.json", "config.json", "config.toml", ".aipod/component-tests.json"]) {
    try { result[file] = digest(await readFile(resolve(root, file), "utf8")); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  }
  return Object.fromEntries(Object.entries(result).sort(([a], [b]) => a.localeCompare(b)));
}

export async function runComponentTests(root: string, project: ProjectManifest, record: ComponentTestRecord,
  candidates: { path: string; content: string }[] = [], timeoutMs = 15000): Promise<string[]> {
  const frozenSource = await readFile(resolve(root, record.path), "utf8");
  if (digest(frozenSource) !== record.sha256) return [`${record.id}: frozen component test was modified`];
  const testErrors = validateComponentTestSource(frozenSource, record.scenarios);
  if (testErrors.length) return testErrors.map((error) => `TEST_SETUP: ${record.id}: ${error}`);
  const scratch = await realpath(await mkdtemp(resolve(tmpdir(), "aipod-component-tests-")));
  try {
    await cp(resolve(root, "src"), resolve(scratch, "src"), { recursive: true }).catch((error: NodeJS.ErrnoException) => { if (error.code !== "ENOENT") throw error; });
    await mkdir(resolve(scratch, "src"), { recursive: true });
    const order: StageName[] = ["models", "providers", "services", "pipelines", "interfaces"];
    for (const later of order.slice(order.indexOf(record.stage) + 1)) await rm(resolve(scratch, "src", later), { recursive: true, force: true });
    const visible = { ...project, beans: project.beans.filter((bean) => order.indexOf(`${bean.category}s` as StageName) <= order.indexOf(record.stage)), routes: [], interfaces: [] };
    await writeFile(resolve(scratch, "aipod.json"), JSON.stringify(visible));
    await writeFile(resolve(scratch, "config.json"), "{}");
    for (const candidate of candidates) { await mkdir(dirname(resolve(scratch, candidate.path)), { recursive: true }); await writeFile(resolve(scratch, candidate.path), candidate.content); }
    await mkdir(dirname(resolve(scratch, record.path)), { recursive: true });
    await writeFile(resolve(scratch, record.path), frozenSource);
    const diagnostics = await typeCheckProject(scratch, [...Object.keys(await sourceFingerprint(scratch))]);
    if (diagnostics.length) return diagnostics.map((diagnostic) => `${diagnostic.file?.startsWith("tests/") ? "TEST_SETUP: " : ""}${formatSemanticDiagnostic(diagnostic)}`);
    const builtTest = resolve(scratch, "component-test.mjs");
    const compiled = ts.transpileModule(frozenSource, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
      .replace(/(from\s+)(["'])aipod-node\2/g, (_, prefix: string) => `${prefix}${JSON.stringify(new URL("./test-sandbox.js", import.meta.url).href)}`);
    await writeFile(builtTest, compiled);
    const token = randomUUID();
    const receiptPath = resolve(scratch, `receipt-${randomUUID()}.json`);
    await writeFile(resolve(scratch, "request.json"), JSON.stringify({ projectRoot: scratch, target: record.id, test: builtTest, scenarios: record.scenarios, token, receiptPath }));
    const before = JSON.stringify(await sourceFingerprint(scratch));
    const permissionFlag = process.allowedNodeEnvironmentFlags.has("--permission") ? "--permission" : "--experimental-permission";
    const runtimeRoots = await componentWorkerReadRoots();
    const errors = await new Promise<string[]>((resolvePromise) => {
      let output = "";
      const child = spawn(process.execPath, [permissionFlag, `--allow-fs-read=${scratch}`, ...runtimeRoots.map((root) => `--allow-fs-read=${root}`), `--allow-fs-write=${scratch}`,
        fileURLToPath(new URL("./component-test-worker.js", import.meta.url)), resolve(scratch, "request.json")],
      { cwd: scratch, env: { PATH: process.env.PATH ?? "", TMPDIR: scratch, HOME: scratch }, stdio: ["ignore", "pipe", "pipe"], detached: process.platform !== "win32" });
      const append = (chunk: Buffer) => { output = `${output}${chunk.toString()}`.slice(-20000); };
      child.stdout.on("data", append); child.stderr.on("data", append);
      const timer = setTimeout(() => {
        if (child.pid && process.platform !== "win32") { try { process.kill(-child.pid, "SIGKILL"); } catch { /* exited */ } }
        else child.kill("SIGKILL");
        resolvePromise([`${record.id}: component tests timed out`]);
      }, timeoutMs);
      child.once("error", (error) => { clearTimeout(timer); resolvePromise([error.message]); });
      child.once("exit", (code) => { clearTimeout(timer); resolvePromise(code === 0 ? [] : [`${record.id}: component tests failed (${code}): ${output}`]); });
    });
    try {
      const receipt = JSON.parse(await readFile(receiptPath, "utf8")) as { version: number; token: string; target: string; status: string; kind?: string; error?: string; cases: { name: string; targetCalls: number; assertions: number }[] };
      if (receipt.version !== 1 || receipt.token !== token || receipt.target !== record.id || !Array.isArray(receipt.cases)) throw new Error("Invalid completion receipt");
      if (receipt.status !== "passed") {
        errors.push(`${receipt.kind === "TEST_SETUP" ? "TEST_SETUP: " : ""}${record.id}: ${receipt.error ?? "component tests failed"}`);
      } else if (receipt.cases.length !== record.scenarios.length || new Set(receipt.cases.map((item) => item.name)).size !== record.scenarios.length
        || receipt.cases.some((item) => !record.scenarios.includes(item.name) || !Number.isInteger(item.targetCalls) || item.targetCalls < 1 || !Number.isInteger(item.assertions) || item.assertions < 1)) {
        throw new Error("Completion receipt has missing cases, real calls or assertions");
      }
    } catch (error) { errors.push(`${record.id}: missing or invalid completed-test evidence: ${error instanceof Error ? error.message : String(error)}`); }
    if (JSON.stringify(await sourceFingerprint(scratch)) !== before) errors.push(`${record.id}: tests or implementation modified candidate sources`);
    const targetPath = project.beans.find((bean) => bean.id === record.id)?.file;
    const targetSource = candidates.find((candidate) => candidate.path === targetPath)?.content
      ?? (targetPath ? await readFile(resolve(root, targetPath), "utf8") : "");
    const resultDirectory = resolve(root, ".aipod/component-test-results");
    await mkdir(resultDirectory, { recursive: true });
    await writeFile(resolve(resultDirectory, `${record.id.replace(/[^A-Za-z0-9_$]/g, "_")}_${digest(record.planHash).slice(0, 12)}.json`), JSON.stringify({
      version: 1, component: record.id, testPath: record.path, testSha256: record.sha256,
      implementationSha256: digest(targetSource), scenarios: record.scenarios,
      status: errors.length ? "failed" : "passed", errors, checkedAt: new Date().toISOString(),
    }, null, 2));
    return errors;
  } finally { await rm(scratch, { recursive: true, force: true }); }
}

export async function verifyComponentTests(root: string, project: ProjectManifest): Promise<string[]> {
  const records = await componentTestRecords(root);
  const errors: string[] = [];
  for (const bean of project.beans.filter((item) => !item.file.startsWith("aipod:"))) {
    const record = records.find((item) => item.id === bean.id);
    if (!record) { errors.push(`${bean.id}: no frozen executable component tests; component is unverified`); continue; }
    errors.push(...await runComponentTests(root, project, record));
  }
  return errors;
}
