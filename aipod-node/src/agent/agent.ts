import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { validateServiceSource } from "../contracts.js";
import { smokeInterface, verifyInterface } from "../interface.js";
import { formatSemanticDiagnostic, typeCheckProject } from "../semantic-check.js";
import { loadProjectConfiguration } from "../shared-config.js";
import {
  commitArtifacts, generateArtifacts, validateArtifacts, verifyCommittedArtifact,
} from "./artifacts.js";
import {
  applyComponents, ensureProjectDirectories, loadProject, saveProject,
  type ProjectManifest,
} from "./project.js";
import { planStage, validateStagePlan } from "./planner.js";
import { repairArtifact } from "./repair.js";
import { loadCurrentState, loadState, newState, saveState } from "./state.js";
import {
  STAGES, type AgentEvent, type AgentState, type ModelClient, type StageName,
  type StagePlan,
} from "./types.js";

export type ProgressHandler = (event: AgentEvent) => void;

export class AgentCancelledError extends Error {
  constructor() { super("Pod Agent cancelled"); this.name = "AgentCancelledError"; }
}

function history(
  state: AgentState,
  stage: StageName | "verification",
  action: string,
  status: "started" | "passed" | "failed",
  summary: string,
): void {
  state.history.push({ timestamp: new Date().toISOString(), stage, action, status, summary });
  state.history = state.history.slice(-100);
}

function updateProject(project: ProjectManifest, stage: StageName, plan: StagePlan): void {
  if (stage === "models" || stage === "providers" || stage === "services") {
    applyComponents(project, stage, plan.components ?? []);
  } else if (stage === "pipelines") {
    for (const route of plan.routes ?? []) {
      const value = { ...route, file: `src/pipelines/${route.name}.ts` };
      const index = project.routes.findIndex((item) => item.name === route.name);
      if (index >= 0) project.routes[index] = value;
      else project.routes.push(value);
    }
  } else {
    for (const item of plan.interfaces ?? []) {
      const value = { ...item, file: `src/interfaces/${item.file.split(/[\\/]/).at(-1)}` };
      const index = project.interfaces.findIndex((entry) => entry.name === item.name);
      if (index >= 0) project.interfaces[index] = value;
      else project.interfaces.push(value);
    }
  }
}

function repairTarget(
  project: ProjectManifest,
  evidence: string[],
): { file: string; service: boolean } | undefined {
  for (const item of evidence) {
    for (const bean of project.beans) {
      if (
        (item.startsWith(`${bean.id}:`) || item.includes(bean.file))
        && !bean.file.startsWith("aipod:")
      ) {
        return { file: bean.file, service: bean.category === "service" };
      }
    }
    for (const route of project.routes) {
      if (item.startsWith(`${route.name}:`)) return { file: route.file, service: false };
    }
    for (const adapter of project.interfaces) {
      if (item.startsWith(`${adapter.name}:`)) {
        const artifact = (adapter.artifacts ?? []).find((value) => item.includes(value.path));
        return { file: artifact?.path ?? adapter.file, service: false };
      }
    }
  }
  return undefined;
}

export async function verifyProject(
  projectRoot: string,
  project: ProjectManifest,
): Promise<string[]> {
  const errors: string[] = [];
  const categories = new Map(project.beans.map((bean) => [bean.id, bean.category]));
  for (const bean of project.beans) {
    for (const dependency of bean.dependencies) {
      const category = categories.get(dependency);
      if (!category) errors.push(`${bean.id}: unknown dependency '${dependency}'`);
      if (bean.category === "service" && (category === "service" || dependency === "PipelineRunner")) {
        errors.push(`${bean.id}: Service cannot see '${dependency}'`);
      }
    }
    if (!bean.file.startsWith("aipod:")) {
      errors.push(...(await verifyCommittedArtifact(projectRoot, bean.file)).map((item) => `${bean.id}: ${item}`));
    }
    if (bean.category === "service" && !bean.file.startsWith("aipod:")) {
      const source = await readFile(resolve(projectRoot, bean.file), "utf8");
      errors.push(...validateServiceSource(source).map((item) => `${bean.id}: ${item}`));
    }
  }
  for (const route of project.routes) {
    errors.push(...(await verifyCommittedArtifact(projectRoot, route.file)).map((item) => `${route.name}: ${item}`));
  }
  for (const item of project.interfaces) {
    errors.push(...(await verifyCommittedArtifact(projectRoot, item.file)).map((error) => `${item.name}: ${error}`));
    for (const artifact of item.artifacts ?? []) {
      errors.push(...(await verifyCommittedArtifact(projectRoot, artifact.path)).map(
        (error) => `${item.name}: ${artifact.path}: ${error}`,
      ));
    }
  }
  errors.push(...(await typeCheckProject(projectRoot)).map(formatSemanticDiagnostic));
  return [...new Set(errors)];
}

export class ConstructionAgent {
  constructor(
    readonly projectRoot: string,
    readonly client: ModelClient,
    readonly onProgress: ProgressHandler = () => undefined,
    readonly isCancelled: () => boolean = () => false,
  ) {}

  #checkCancelled(): void {
    if (this.isCancelled()) throw new AgentCancelledError();
  }

  async revise(
    instruction: string,
    requestedStage: StageName | "auto" = "auto",
  ): Promise<AgentState> {
    if (!instruction.trim()) throw new Error("Revision instruction is required");
    const current = await loadCurrentState(this.projectRoot);
    if (!current) throw new Error("No existing Agent state is available to revise");
    const project = await loadProject(this.projectRoot);
    let fromStage: StageName;
    if (requestedStage === "auto") {
      const raw = await this.client.complete(
        `CLASSIFY_REVISION_STAGE\nChoose the earliest affected stage. Return {"stage":"models|providers|services|pipelines|interfaces","summary":"public reason"}. Service behavior changes belong to services; orchestration belongs to pipelines; presentation and transport belong to interfaces.`,
        `Change:\n${instruction}\nCurrent project:\n${JSON.stringify({
          beans: project.beans.map(({ id, category, inputs, outputs }) => ({ id, category, inputs, outputs })),
          routes: project.routes,
          interfaces: project.interfaces,
        })}`,
      );
      const candidate = String(raw.stage ?? "") as StageName;
      if (!STAGES.includes(candidate)) throw new Error(`Invalid revision stage '${candidate}'`);
      fromStage = candidate;
    } else {
      if (!STAGES.includes(requestedStage)) {
        throw new Error(`Invalid revision stage '${requestedStage}'`);
      }
      fromStage = requestedStage;
    }
    const state = newState(instruction.trim());
    const start = STAGES.indexOf(fromStage);
    for (let index = 0; index < start; index += 1) {
      const stage = STAGES[index]!;
      state.stages[stage] = structuredClone(current.stages[stage]);
      state.stages[stage].status = "complete";
    }
    state.currentStage = fromStage;
    await saveState(this.projectRoot, state);
    return this.run(instruction.trim());
  }

  async run(objective: string): Promise<AgentState> {
    if (!objective.trim()) throw new Error("Objective is required");
    await ensureProjectDirectories(this.projectRoot);
    const project = await loadProject(this.projectRoot);
    const configuration = await loadProjectConfiguration(this.projectRoot);
    const state = await loadState(this.projectRoot, objective.trim());
    state.status = "running";

    try {
      for (const stage of STAGES) {
        this.#checkCancelled();
        const record = state.stages[stage];
        if (record.status === "complete") continue;
        state.currentStage = stage;
        record.attempts += 1;
        record.status = "planning";
        history(state, stage, "plan", "started", `Planning ${stage}`);
        this.onProgress({ stage, action: "planning", message: `Planning ${stage}` });
        await saveState(this.projectRoot, state);

        const plan = record.plan ?? await planStage(
          this.client, stage, objective, project, record.evidence,
          configuration,
        );
        this.#checkCancelled();
        const planErrors = validateStagePlan(stage, plan, project);
        if (planErrors.length) {
          record.status = "failed";
          record.evidence = planErrors;
          delete record.plan;
          history(state, stage, "plan", "failed", planErrors.join("; "));
          throw new Error(`${stage} plan rejected: ${planErrors.join("; ")}`);
        }
        record.plan = plan;
        record.status = "generating";
        await saveState(this.projectRoot, state);

        this.onProgress({ stage, action: "generating", message: `Generating ${stage}` });
        const artifacts = await generateArtifacts(this.client, stage, plan, project);
        this.#checkCancelled();
        for (const artifact of artifacts) {
          this.onProgress({
            stage, action: "validating", artifact: artifact.path,
            message: `Validating ${artifact.path}`,
          });
        }
        const artifactErrors = validateArtifacts(artifacts);
        if (artifactErrors.length) {
          record.status = "failed";
          record.evidence = artifactErrors;
          delete record.plan;
          history(state, stage, "generate", "failed", artifactErrors.join("; "));
          throw new Error(`${stage} artifacts rejected: ${artifactErrors.join("; ")}`);
        }

        this.onProgress({ stage, action: "committing", message: `Committing ${stage}` });
        this.#checkCancelled();
        await commitArtifacts(this.projectRoot, stage, artifacts);
        updateProject(project, stage, plan);
        await saveProject(this.projectRoot, project);
        record.status = "complete";
        record.artifacts = artifacts.map((artifact) => artifact.path);
        record.evidence = [];
        history(state, stage, "build", "passed", `${artifacts.length} artifact(s) committed`);
        await saveState(this.projectRoot, state);
        this.onProgress({ stage, action: "complete", message: `${stage} complete` });
      }

      state.currentStage = "verification";
      this.#checkCancelled();
      this.onProgress({ stage: "verification", action: "validating", message: "Verifying project" });
      let evidence = await verifyProject(this.projectRoot, project);
      if (!evidence.length) {
        for (const item of project.interfaces) {
          try {
            const smoke = await smokeInterface(this.projectRoot, item.name);
            if (smoke.status !== "passed") evidence.push(`${item.name}: smoke failed`);
            const checks = await verifyInterface(this.projectRoot, item.name);
            if (checks.status !== "passed") evidence.push(`${item.name}: required verification failed`);
          } catch (error) {
            evidence.push(`${item.name}: ${error instanceof Error ? error.message : String(error)}`);
          }
        }
      }
      while (evidence.length && state.verification.repairs < 2) {
        this.#checkCancelled();
        const target = repairTarget(project, evidence);
        if (!target) break;
        this.onProgress({
          stage: "verification", action: "repairing",
          artifact: target.file, message: `Repairing ${target.file}`,
        });
        try {
          await repairArtifact(
            this.client, this.projectRoot, target.file, evidence,
            { service: target.service },
          );
          state.verification.repairs += 1;
          await saveState(this.projectRoot, state);
          evidence = await verifyProject(this.projectRoot, project);
        } catch (error) {
          evidence = [
            ...evidence,
            `Repair failed: ${error instanceof Error ? error.message : String(error)}`,
          ];
          break;
        }
      }
      state.verification = {
        status: evidence.length ? "failed" : "passed",
        evidence,
        repairs: state.verification.repairs,
      };
      history(
        state, "verification", "verify",
        evidence.length ? "failed" : "passed",
        evidence.length ? evidence.join("; ") : "Project verification passed",
      );
      state.status = evidence.length ? "failed" : "complete";
      await saveState(this.projectRoot, state);
      if (evidence.length) throw new Error(`Project verification failed: ${evidence.join("; ")}`);
      this.onProgress({ stage: "verification", action: "complete", message: "Pod complete" });
      return state;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const cancelled = error instanceof AgentCancelledError;
      state.status = cancelled ? "cancelled" : "failed";
      if (state.currentStage !== "verification") {
        const record = state.stages[state.currentStage];
        if (record.status !== "complete") {
          record.status = cancelled ? "pending" : "failed";
          if (!cancelled && !record.evidence.length) record.evidence = [message];
          history(state, state.currentStage, "build", "failed", message);
        }
      }
      await saveState(this.projectRoot, state);
      this.onProgress({
        stage: state.currentStage,
        action: cancelled ? "cancelled" : "failed",
        message,
      });
      throw error;
    }
  }
}
