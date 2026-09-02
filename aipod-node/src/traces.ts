import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { randomUUID } from "node:crypto";

import type { PipelineContext } from "./context.js";
import type { Result } from "./result.js";

const sensitive = /token|secret|password|api[_-]?key|authorization/i;

export function redact(value: unknown, key = ""): unknown {
  if (sensitive.test(key)) return "[REDACTED]";
  if (Array.isArray(value)) return value.map((item) => redact(item));
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value).map(([name, item]) => [name, redact(item, name)]),
    );
  }
  if (typeof value === "string" && /(?:sk-|Bearer\s+)[A-Za-z0-9._-]{8,}/i.test(value)) {
    return "[REDACTED]";
  }
  return value;
}

export async function writeRunTrace(
  projectRoot: string,
  route: string,
  params: Record<string, unknown>,
  result: Result,
  context: PipelineContext,
  durationMs: number,
): Promise<Record<string, unknown>> {
  const id = `${new Date().toISOString().replace(/[:.]/g, "-")}-${randomUUID().slice(0, 8)}`;
  const directory = resolve(projectRoot, ".aipod", "runs");
  await mkdir(directory, { recursive: true });
  const trace = redact({
    id, route, status: result.status, startedAt: new Date().toISOString(),
    durationMs, params, result, context: context.summary(),
  }) as Record<string, unknown>;
  const path = resolve(directory, `${id}.json`);
  await writeFile(path, `${JSON.stringify(trace, null, 2)}\n`);
  return { ...trace, tracePath: path };
}
