import { access, readdir } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

async function walk(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  return (await Promise.all(entries.map(async (entry) => {
    const path = resolve(directory, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  }))).flat();
}

const available = async (path: string) => {
  try { await access(path); return true; } catch { return false; }
};

function runtimeTypesPath(): string {
  const here = dirname(fileURLToPath(import.meta.url));
  return resolve(here, "index.d.ts");
}

export interface SemanticDiagnostic {
  file?: string;
  line?: number;
  column?: number;
  code: number;
  message: string;
}

export async function typeCheckProject(
  projectRoot: string,
  sourceFiles?: readonly string[],
): Promise<SemanticDiagnostic[]> {
  const sourceRoot = resolve(projectRoot, "src");
  if (sourceFiles === undefined && !await available(sourceRoot)) return [];
  const rootNames = sourceFiles === undefined
    ? (await walk(sourceRoot)).filter((path) => path.endsWith(".ts"))
    : sourceFiles.filter((path) => path.endsWith(".ts")).map((path) => resolve(projectRoot, path));
  if (!rootNames.length) return [];
  const runtimeTypes = runtimeTypesPath();
  const options: ts.CompilerOptions = {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.NodeNext,
    moduleResolution: ts.ModuleResolutionKind.NodeNext,
    strict: true,
    noEmit: true,
    skipLibCheck: true,
    allowSyntheticDefaultImports: true,
    baseUrl: projectRoot,
    paths: {
      "aipod-node": [runtimeTypes],
      "aipod-node/*": [resolve(dirname(runtimeTypes), "*")],
    },
  };
  const program = ts.createProgram({ rootNames, options });
  return ts.getPreEmitDiagnostics(program).map((diagnostic) => {
    const position = diagnostic.file && diagnostic.start !== undefined
      ? diagnostic.file.getLineAndCharacterOfPosition(diagnostic.start)
      : undefined;
    return {
      ...(diagnostic.file ? { file: relative(projectRoot, diagnostic.file.fileName).replaceAll("\\", "/") } : {}),
      ...(position ? { line: position.line + 1, column: position.character + 1 } : {}),
      code: diagnostic.code,
      message: ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
    };
  });
}

export const formatSemanticDiagnostic = (diagnostic: SemanticDiagnostic) => {
  const location = diagnostic.file
    ? `${diagnostic.file}${diagnostic.line ? `:${diagnostic.line}:${diagnostic.column ?? 1}` : ""}`
    : "TypeScript";
  return `${location} TS${diagnostic.code}: ${diagnostic.message}`;
};
