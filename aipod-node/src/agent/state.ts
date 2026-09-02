import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

import { type AgentState, type StageRecord } from "./types.js";

export const statePath = (projectRoot: string) => resolve(projectRoot, ".aipod", "plan.json");

export function newState(objective: string): AgentState {
  const stage = (): StageRecord => ({
    status: "pending", attempts: 0, artifacts: [], evidence: [],
  });
  return {
    version: 1,
    objective,
    status: "running",
    currentStage: "models",
    stages: {
      models: stage(),
      providers: stage(),
      services: stage(),
      pipelines: stage(),
      interfaces: stage(),
    },
    verification: { status: "pending", evidence: [], repairs: 0 },
    history: [],
  };
}

export async function loadState(projectRoot: string, objective: string): Promise<AgentState> {
  try {
    const parsed = JSON.parse(await readFile(statePath(projectRoot), "utf8")) as AgentState;
    if (parsed.version === 1 && parsed.objective === objective) {
      parsed.verification.repairs ??= 0;
      return parsed;
    }
    return newState(objective);
  } catch {
    return newState(objective);
  }
}

export async function loadCurrentState(projectRoot: string): Promise<AgentState | undefined> {
  try {
    const parsed = JSON.parse(await readFile(statePath(projectRoot), "utf8")) as AgentState;
    if (parsed.version !== 1 || !parsed.objective) return undefined;
    parsed.verification.repairs ??= 0;
    return parsed;
  } catch {
    return undefined;
  }
}

export async function saveState(projectRoot: string, state: AgentState): Promise<void> {
  const target = statePath(projectRoot);
  await mkdir(dirname(target), { recursive: true });
  const temporary = `${target}.tmp`;
  await writeFile(temporary, `${JSON.stringify(state, null, 2)}\n`);
  await rename(temporary, target);
}
