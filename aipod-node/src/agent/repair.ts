import { readFile, rename, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { validateServiceSource } from "../contracts.js";
import { validateArtifactContent } from "./artifacts.js";
import type { ModelClient } from "./types.js";

export interface CodePatch {
  oldText: string;
  newText: string;
}

const exportsOf = (source: string) => new Set(
  [...source.matchAll(/export\s+(?:default\s+)?(?:class|interface|type|function|const)\s+([A-Za-z_$][\w$]*)/g)]
    .map((match) => match[1]!),
);

export function applyCodePatches(
  source: string,
  patches: CodePatch[],
  options: { maxChangedCharacters?: number } = {},
): string {
  if (!patches.length) throw new Error("Repair returned no patches");
  const changed = patches.reduce(
    (total, patch) => total + patch.oldText.length + patch.newText.length,
    0,
  );
  if (changed > (options.maxChangedCharacters ?? 4_000)) {
    throw new Error("Repair patch exceeds the allowed size");
  }
  let updated = source;
  for (const patch of patches) {
    if (!patch.oldText || patch.oldText === patch.newText) throw new Error("Repair patch is empty");
    const first = updated.indexOf(patch.oldText);
    const last = updated.lastIndexOf(patch.oldText);
    if (first < 0) throw new Error("Repair oldText was not found exactly");
    if (first !== last) throw new Error("Repair oldText must match exactly once");
    updated = `${updated.slice(0, first)}${patch.newText}${updated.slice(first + patch.oldText.length)}`;
  }
  const before = exportsOf(source);
  const after = exportsOf(updated);
  const removed = [...before].filter((name) => !after.has(name));
  if (removed.length) throw new Error(`Repair removed public exports: ${removed.join(", ")}`);
  return updated;
}

export async function repairArtifact(
  client: ModelClient,
  projectRoot: string,
  file: string,
  evidence: string[],
  options: { service?: boolean } = {},
): Promise<void> {
  const path = resolve(projectRoot, file);
  const source = await readFile(path, "utf8");
  const raw = await client.complete(
    `REPAIR_ARTIFACT:${file}\nRepair only this TypeScript file. Return strict JSON {"patches":[{"oldText":"exact unique source text","newText":"replacement"}]}. Keep every public export. Do not regenerate the whole file.`,
    `Validation evidence:\n${JSON.stringify(evidence)}\n\nCurrent source:\n${source}`,
  );
  const patches = Array.isArray(raw.patches)
    ? raw.patches.map((item) => ({
      oldText: String((item as { oldText?: unknown }).oldText ?? ""),
      newText: String((item as { newText?: unknown }).newText ?? ""),
    }))
    : [];
  const updated = applyCodePatches(source, patches);
  const errors = validateArtifactContent(file, updated);
  if (options.service) errors.push(...validateServiceSource(updated));
  if (errors.length) throw new Error(`Repair validation failed: ${errors.join("; ")}`);
  const temporary = `${path}.repair.tmp`;
  await writeFile(temporary, updated);
  await rename(temporary, path);
}
