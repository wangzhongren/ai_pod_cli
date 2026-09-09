import { existsSync, readFileSync, realpathSync, readdirSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import ts from "typescript";
import { validateServiceSource } from "../contracts.js";
import { validateArtifactContent } from "./artifacts.js";
import type { ProjectBean } from "./project.js";

export const AREAS = ["contracts", "impl", "public"] as const;
export function area(file: string): [string, string] {
  const parts = file.split("/");
  return parts.length > 2 && parts[0] === "src" && ["providers", "services"].includes(parts[1]!)
    ? [parts[1]!, (AREAS as readonly string[]).includes(parts[2]!) ? parts[2]! : "legacy"] : ["", ""];
}
interface Definition { entry: string; file: string; symbol: string }

/** Resolve project imports/exports as syntax, never by executing generated modules. */
export class SourceGraph {
  readonly root: string;
  private cache = new Map<string, ts.SourceFile>();
  private edges = new Map<string, string[]>();
  constructor(root: string) { this.root = realpathSync(root); }
  source(file: string): ts.SourceFile {
    if (!this.cache.has(file)) {
      const target = resolve(this.root, file);
      if (isAbsolute(file) || relative(this.root, target).startsWith("..") || realpathSync(target) !== target) throw new Error(`Source escapes project or uses symlinks: ${file}`);
      this.cache.set(file, ts.createSourceFile(file, readFileSync(target, "utf8"), ts.ScriptTarget.Latest, true));
    }
    return this.cache.get(file)!;
  }
  moduleFile(file: string, specifier: string): string | undefined {
    if (!specifier.startsWith(".") && !specifier.startsWith("src/")) return;
    const base = specifier.startsWith(".") ? resolve(this.root, dirname(file), specifier) : resolve(this.root, specifier);
    for (const target of [base.replace(/\.js$/, ".ts"), `${base}.ts`, resolve(base, "index.ts")]) {
      if (!target.endsWith(".ts") || !existsSync(target)) continue;
      const local = relative(this.root, target).replaceAll("\\", "/");
      this.source(local);
      return local;
    }
    throw new Error(`Unresolved project import in ${file}: ${specifier}`);
  }
  imports(file: string): string[] {
    if (this.edges.has(file)) return this.edges.get(file)!;
    const found: string[] = [];
    const visit = (node: ts.Node): void => {
      let specifier: ts.Node | undefined;
      if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier) specifier = node.moduleSpecifier;
      if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument)) specifier = node.argument.literal;
      if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword || ts.isIdentifier(node.expression) && node.expression.text === "require")) specifier = node.arguments[0];
      if (specifier && ts.isStringLiteral(specifier)) {
        const target = this.moduleFile(file, specifier.text);
        if (target) found.push(target);
      }
      ts.forEachChild(node, visit);
    };
    visit(this.source(file));
    this.edges.set(file, [...new Set(found)]);
    return this.edges.get(file)!;
  }
  exported(file: string, symbol: string, seen = new Set<string>(), local = false): { file: string; symbol: string } {
    const key = `${file}:${symbol}:${local}`;
    if (seen.has(key)) throw new Error(`Cyclic public export: ${file}:${symbol}`);
    seen = new Set([...seen, key]);
    const statements = this.source(file).statements;
    if (area(file)[1] === "public" && statements.some((node) => !ts.isImportDeclaration(node) && !ts.isExportDeclaration(node) && !ts.isEmptyStatement(node))) {
      throw new Error(`public/ contains only named imports/exports; put implementation in impl/: ${file}`);
    }
    for (const node of statements) {
      if ((ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node)) && node.name &&
          (node.name.text === symbol || symbol === "default" && node.modifiers?.some((m) => m.kind === ts.SyntaxKind.DefaultKeyword)) &&
          (local || node.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword))) return { file, symbol: node.name.text };
      if (!local && ts.isExportDeclaration(node) && !node.isTypeOnly && node.exportClause && ts.isNamedExports(node.exportClause)) {
        const binding = node.exportClause.elements.find((item) => item.name.text === symbol && !item.isTypeOnly);
        if (!binding) continue;
        const original = binding.propertyName?.text ?? binding.name.text;
        if (node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
          const target = this.moduleFile(file, node.moduleSpecifier.text);
          if (!target || target.split("/").slice(0, 2).join("/") !== file.split("/").slice(0, 2).join("/")) throw new Error("Public exports must resolve to the same layer");
          return this.exported(target, original, seen);
        }
        return this.exported(file, original, seen, true);
      }
      if (local && ts.isImportDeclaration(node) && node.importClause && !node.importClause.isTypeOnly && ts.isStringLiteral(node.moduleSpecifier)) {
        const bindings = node.importClause.namedBindings;
        const binding = bindings && ts.isNamedImports(bindings) ? bindings.elements.find((item) => item.name.text === symbol && !item.isTypeOnly) : undefined;
        const original = binding ? binding.propertyName?.text ?? binding.name.text : node.importClause.name?.text === symbol ? "default" : undefined;
        if (original) {
          const target = this.moduleFile(file, node.moduleSpecifier.text);
          if (target && target.split("/")[1] === file.split("/")[1]) return this.exported(target, original, seen);
        }
      }
    }
    throw new Error(`Cannot resolve export ${symbol} from ${file}; use explicit named exports`);
  }
  component(bean: Pick<ProjectBean, "file" | "id" | "category">): Definition {
    const section = area(bean.file)[1];
    if (["contracts", "impl"].includes(section)) throw new Error("Register components through public/, not contracts/ or impl/");
    const definition = this.exported(bean.file, bean.id);
    if (section === "public" && area(definition.file)[1] !== "impl") throw new Error("Public components must resolve to a class in impl/");
    const declaration = this.source(definition.file).statements.find((node) =>
      (ts.isClassDeclaration(node) || bean.category === "model" && (ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node))) && node.name?.text === definition.symbol);
    if (!declaration) throw new Error(`Source must export ${bean.category === "model" ? "model" : "class"} ${bean.id}`);
    if (bean.category === "service" && ts.isClassDeclaration(declaration) && !declaration.members.some((member) => ts.isMethodDeclaration(member) && member.name.getText() === "execute")) throw new Error("Service must implement execute(context)");
    return { entry: bean.file, ...definition };
  }
  hasExport(file: string, symbol: string): boolean {
    return this.source(file).statements.some((node) =>
      ts.isExportDeclaration(node) ? !node.exportClause || ts.isNamedExports(node.exportClause) && node.exportClause.elements.some((item) => item.name.text === symbol) :
      (ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node) || ts.isFunctionDeclaration(node)) && node.name?.text === symbol && Boolean(node.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword)));
  }
  closure(file: string): Set<string> {
    const found = new Set<string>(), pending = [file];
    while (pending.length) {
      const current = pending.pop()!;
      if (found.has(current)) continue;
      found.add(current);
      pending.push(...this.imports(current).filter((target) => target.split("/").slice(0, 2).join("/") === file.split("/").slice(0, 2).join("/")));
    }
    return found;
  }
}

export function validateLayout(root: string, beans: ProjectBean[], stage?: string): string[] {
  const graph = new SourceGraph(root), errors: string[] = [], components = new Map<string, Definition>();
  for (const bean of beans.filter((item) => !item.file.startsWith("aipod:"))) {
    try { components.set(bean.id, graph.component(bean)); }
    catch (error) { if (!stage || `${bean.category}s` === stage) errors.push(`${bean.id}: ${String(error)}`); }
  }
  const serviceFiles = new Set(beans.filter((item) => item.category === "service").map((item) => components.get(item.id)?.file));
  for (const bean of beans.filter((item) => item.category === "service" && (!stage || stage === "services"))) {
    const definition = components.get(bean.id);
    if (!definition) continue;
    try {
      for (const file of graph.closure(definition.file)) {
        errors.push(...validateServiceSource(graph.source(file).text, { allowInternalImports: true }).map((error) => `${file}: ${error}`));
        if (serviceFiles.has(file) && file !== definition.file) errors.push(`${definition.file}: Service cannot import another Service (${file}); compose in a Pipeline`);
      }
    } catch (error) { errors.push(String(error)); }
  }
  const walk = (directory: string): void => {
    if (!existsSync(resolve(graph.root, directory))) return;
    for (const entry of readdirSync(resolve(graph.root, directory), { withFileTypes: true })) {
      const file = `${directory}/${entry.name}`;
      if (entry.isSymbolicLink()) { errors.push(`Source may not be a symlink: ${file}`); continue; }
      if (entry.isDirectory()) { walk(file); continue; }
      if (!file.endsWith(".ts")) continue;
      try {
        errors.push(...validateArtifactContent(file, graph.source(file).text).map((error) => `${file}: ${error}`));
        const [owner, section] = area(file);
        for (const target of graph.imports(file)) {
          const [targetOwner, targetSection] = area(target);
          if (targetSection === "impl" && owner !== targetOwner) errors.push(`${file}: import ${targetOwner} through public/ or contracts/, not ${target}`);
          if (section === "contracts" && ["impl", "public"].includes(targetSection)) errors.push(`${file}: contracts cannot depend on implementation/public (${target})`);
        }
      } catch (error) { errors.push(`${file}: ${String(error)}`); }
    }
  };
  walk(stage && stage !== "pod" ? `src/${stage}` : "src");
  return [...new Set(errors)];
}
