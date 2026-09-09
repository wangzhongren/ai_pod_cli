import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import ts from "typescript";

import type { ProjectManifest } from "./project.js";
import { STAGES, type StageName, type StagePlan } from "./types.js";

export type RevisionScope = Record<StageName, string[]>;

export function stageEntries(project: ProjectManifest, stage: StageName) {
  if (stage === "pipelines") return project.routes.map((item) => ({ id: item.name, files: [item.file] }));
  if (stage === "interfaces") return project.interfaces.map((item) => ({
    id: item.name, files: [item.file, ...(item.artifacts ?? []).map((artifact) => artifact.path)],
  }));
  return project.beans.filter((bean) => `${bean.category}s` === stage)
    .map((bean) => ({ id: bean.id, files: [bean.file] }));
}

/** Unknown targets/import graphs fall back to the existing whole-stage revision. */
export async function revisionScope(
  root: string, project: ProjectManifest, stage: StageName, targets: unknown,
): Promise<RevisionScope | undefined> {
  if (!Array.isArray(targets) || !targets.length || targets.some((id) => typeof id !== "string")) return;
  // Public entries can share arbitrarily nested implementation/contracts. Let the
  // owning layer plan the revision rather than granting only an export file.
  if (project.beans.some((bean) => /^src\/(providers|services)\/public\//.test(bean.file))) return;
  const entries = STAGES.flatMap((name) => stageEntries(project, name).map((item) => ({ ...item, stage: name })));
  const key = (name: StageName, id: string) => `${name}:${id}`;
  const known = new Set(stageEntries(project, stage).filter((entry) =>
    !entry.files.some((file) => file.startsWith("aipod:"))
  ).map((item) => item.id));
  if (targets.some((id) => !known.has(id))) return;
  const owners = new Map(entries.flatMap((entry) => entry.files.map((file) =>
    [resolve(root, file), key(entry.stage, entry.id)] as const
  )));
  const dependencies = new Map<string, Set<string>>();
  for (const entry of entries) {
    const deps = new Set<string>();
    dependencies.set(key(entry.stage, entry.id), deps);
    const bean = project.beans.find((item) => item.id === entry.id && `${item.category}s` === entry.stage);
    for (const id of bean?.dependencies ?? []) {
      const dependency = project.beans.find((item) => item.id === id);
      if (dependency) deps.add(key(`${dependency.category}s` as StageName, id));
    }
    if (entry.stage === "pipelines") {
      for (const id of project.routes.find((item) => item.name === entry.id)!.services) deps.add(key("services", id));
    }
    if (entry.stage === "interfaces") {
      deps.add(key("pipelines", project.interfaces.find((item) => item.name === entry.id)!.route));
    }
    const visited = new Set<string>();
    const scan = async (file: string): Promise<void> => {
      if (visited.has(file)) return;
      visited.add(file);
      const owner = owners.get(file);
      if (owner) deps.add(owner);
      const source = await readFile(file, "utf8");
      const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
      const imports: string[] = [];
      const visit = (node: ts.Node): void => {
        if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier) {
          if (!ts.isStringLiteral(node.moduleSpecifier)) throw new Error("Unknown import");
          imports.push(node.moduleSpecifier.text);
        }
        if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument) && ts.isStringLiteral(node.argument.literal)) {
          imports.push(node.argument.literal.text);
        }
        if (ts.isExternalModuleReference(node)) {
          if (!node.expression || !ts.isStringLiteral(node.expression)) throw new Error("Unknown require dependency");
          imports.push(node.expression.text);
        }
        if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword ||
          (ts.isIdentifier(node.expression) && node.expression.text === "require"))) {
          const argument = node.arguments[0];
          if (!argument || !ts.isStringLiteral(argument)) throw new Error("Dynamic dependency");
          imports.push(argument.text);
        }
        ts.forEachChild(node, visit);
      };
      visit(ast);
      for (const specifier of imports) {
        if (specifier === "aipod-node" || specifier.startsWith("node:")) continue;
        const resolved = ts.resolveModuleName(specifier, file, {
          moduleResolution: ts.ModuleResolutionKind.NodeNext,
          module: ts.ModuleKind.NodeNext,
        }, ts.sys).resolvedModule;
        if (!resolved) throw new Error(`Unresolved dependency ${specifier}`);
        if (resolved.isExternalLibraryImport) continue;
        await scan(resolve(resolved.resolvedFileName));
      }
    };
    try {
      for (const file of entry.files) {
        if (!file.startsWith("aipod:") && /\.[cm]?[jt]sx?$/.test(file)) await scan(resolve(root, file));
      }
    } catch { return; }
  }
  const affected = new Set(targets.map((id) => key(stage, id)));
  let changed = true;
  while (changed) {
    changed = false;
    for (const [id, deps] of dependencies) {
      if (!affected.has(id) && [...deps].some((dependency) => affected.has(dependency))) {
        affected.add(id);
        changed = true;
      }
    }
  }
  // An unexpected upstream dependency requires a broader architectural revision.
  if (entries.some((entry) => STAGES.indexOf(entry.stage) < STAGES.indexOf(stage) && affected.has(key(entry.stage, entry.id)))) return;
  return Object.fromEntries(STAGES.map((name) => [name, entries.filter((entry) =>
    entry.stage === name && affected.has(key(name, entry.id))
  ).map((entry) => entry.id)])) as RevisionScope;
}

export function validateRevisionPlan(stage: StageName, plan: StagePlan, allowed: string[]): string[] {
  const ids = stage === "pipelines" ? (plan.routes ?? []).map((item) => item.name)
    : stage === "interfaces" ? (plan.interfaces ?? []).map((item) => item.name)
      : (plan.components ?? []).map((item) => item.id);
  if (ids.length !== allowed.length || new Set(ids).size !== ids.length || ids.some((id) => !allowed.includes(id))) {
    return [`Revision must update exactly these ${stage}: ${allowed.join(", ")}. Additions, removals and renames require a whole-stage revision.`];
  }
  return [];
}
