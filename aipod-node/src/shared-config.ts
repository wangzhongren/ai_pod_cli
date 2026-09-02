import { homedir, platform } from "node:os";
import { dirname, resolve } from "node:path";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { parse } from "smol-toml";

export type ConfigDocument = Record<string, unknown>;

export function globalConfigPath(environment: NodeJS.ProcessEnv = process.env): string {
  const directory = platform() === "win32"
    ? resolve(environment.APPDATA ?? homedir(), "aipod")
    : resolve(homedir(), ".aipod");
  return resolve(directory, "config.toml");
}

export async function loadToml(path: string): Promise<ConfigDocument> {
  try {
    return parse(await readFile(path, "utf8")) as ConfigDocument;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

export async function loadGlobalEnvironment(
  path = globalConfigPath(),
): Promise<Record<string, string>> {
  const document = await loadToml(path);
  const values = document.env;
  if (typeof values !== "object" || values === null || Array.isArray(values)) return {};
  return Object.fromEntries(
    Object.entries(values).map(([key, value]) => [key, String(value)]),
  );
}

export async function saveGlobalEnvironment(
  values: Record<string, string>,
  path = globalConfigPath(),
): Promise<void> {
  let content = "";
  try { content = await readFile(path, "utf8"); } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  const lines = content ? content.split(/\r?\n/) : [
    "# AIPod global configuration",
    "# Shared across all projects",
    "",
  ];
  let start = lines.findIndex((line) => /^\s*\[env\]\s*(?:#.*)?$/.test(line));
  if (start < 0) {
    if (lines.at(-1)?.trim()) lines.push("");
    lines.push("[env]");
    start = lines.length - 1;
  }
  let end = lines.findIndex((line, index) => index > start && /^\s*\[[^\]]+\]/.test(line));
  if (end < 0) end = lines.length;
  for (const [key, value] of Object.entries(values)) {
    const pattern = new RegExp(`^\\s*${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*=`);
    const index = lines.findIndex((line, lineIndex) =>
      lineIndex > start && lineIndex < end && pattern.test(line)
    );
    const replacement = `${key} = ${JSON.stringify(value)}`;
    if (index >= 0) lines[index] = replacement;
    else {
      lines.splice(end, 0, replacement);
      end += 1;
    }
  }
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.tmp`;
  await writeFile(temporary, `${lines.join("\n").replace(/\n+$/, "")}\n`);
  await rename(temporary, path);
}

export async function removeGlobalEnvironmentKey(
  key: string,
  path = globalConfigPath(),
): Promise<boolean> {
  let content: string;
  try { content = await readFile(path, "utf8"); } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
  const lines = content.split(/\r?\n/);
  const start = lines.findIndex((line) => /^\s*\[env\]\s*(?:#.*)?$/.test(line));
  if (start < 0) return false;
  let end = lines.findIndex((line, index) => index > start && /^\s*\[[^\]]+\]/.test(line));
  if (end < 0) end = lines.length;
  const pattern = new RegExp(`^\\s*${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*=`);
  const index = lines.findIndex((line, lineIndex) =>
    lineIndex > start && lineIndex < end && pattern.test(line)
  );
  if (index < 0) return false;
  lines.splice(index, 1);
  const temporary = `${path}.tmp`;
  await writeFile(temporary, `${lines.join("\n").replace(/\n+$/, "")}\n`);
  await rename(temporary, path);
  return true;
}

export async function loadDotEnv(path: string): Promise<Record<string, string>> {
  try {
    const content = await readFile(path, "utf8");
    return Object.fromEntries(content.split(/\r?\n/).flatMap((line) => {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) return [];
      const match = trimmed.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
      if (!match) return [];
      let value = match[2] ?? "";
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      return [[match[1]!, value]];
    }));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

export async function applySharedEnvironment(
  projectRoot = ".",
  environment: NodeJS.ProcessEnv = process.env,
  globalPath = globalConfigPath(environment),
): Promise<NodeJS.ProcessEnv> {
  const local = await loadDotEnv(resolve(projectRoot, ".env"));
  const global = await loadGlobalEnvironment(globalPath);
  for (const [key, value] of Object.entries(local)) {
    if (!environment[key]) environment[key] = value;
  }
  for (const [key, value] of Object.entries(global)) {
    if (!environment[key]) environment[key] = value;
  }
  return environment;
}

export async function loadProjectConfiguration(projectRoot: string): Promise<ConfigDocument> {
  const tomlPath = resolve(projectRoot, "config.toml");
  try {
    return parse(await readFile(tomlPath, "utf8")) as ConfigDocument;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  try {
    return JSON.parse(await readFile(resolve(projectRoot, "config.json"), "utf8")) as ConfigDocument;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}
