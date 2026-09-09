import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { STAGES, type StageName, type AgentState, type AgentEvent, type ModelClient, type StagePlan } from "./types.js";
import { loadProject, saveProject, ensureProjectDirectories, type ProjectManifest, type ProjectBean } from "./project.js";
import { loadCurrentState, loadState, newState, saveState } from "./state.js";
import { validateArtifactContent } from "./artifacts.js";
import { SourceGraph, validateLayout, area } from "./component-layout.js";
import { typeCheckProject, formatSemanticDiagnostic } from "../semantic-check.js";
import { WorkspaceAgent, WorkspaceTools, pathOwner, type Owner, type Action, type ShellCheck } from "./workspace.js";
import { revisionScope, stageEntries } from "./revision.js";

export type ProgressHandler = (event: AgentEvent) => void;
export class AgentCancelledError extends Error {
  constructor() { super("Pod Agent cancelled"); this.name = "AgentCancelledError"; }
}
export interface ChangeRequest {
  id: string; requester: Owner; target: Owner; paths: string[]; reason: string; change: string;
  approved: boolean; summary: string; status: "approved" | "working" | "applied" | "denied" | "failed" | "superseded";
}
type WorkspaceState = AgentState & { mode?: "workspace"; changeRequests?: ChangeRequest[] };

/** Ordinary project checks; component-test generation is not a prerequisite. */
export async function verifyProject(root: string, project: ProjectManifest): Promise<string[]> {
  const errors: string[] = validateLayout(root, project.beans);
  for (const bean of project.beans) {
    for (const dependency of bean.dependencies) {
      const found = project.beans.find((item) => item.id === dependency);
      if (!found) errors.push(`${bean.id}: unknown dependency '${dependency}'`);
      else if (bean.category === "service" && (found.category !== "provider" || dependency === "PipelineRunner")) errors.push(`${bean.id}: cannot inject '${dependency}'`);
    }
    if (!bean.file.startsWith("aipod:")) {
      try {
        const source = await readFile(resolve(root, bean.file), "utf8");
        errors.push(...validateArtifactContent(bean.file, source).map((error) => `${bean.file}: ${error}`));
      } catch (error) { errors.push(`${bean.file}: ${String(error)}`); }
    }
  }
  errors.push(...(await typeCheckProject(root)).map(formatSemanticDiagnostic));
  return errors;
}

export class ConstructionAgent {
  private state!: WorkspaceState;
  private project!: ProjectManifest;
  private requests = 0;
  constructor(readonly projectRoot: string, readonly client: ModelClient,
    readonly onProgress: ProgressHandler = () => undefined, readonly isCancelled: () => boolean = () => false) {}
  private checkCancelled(): void { if (this.isCancelled()) throw new AgentCancelledError(); }
  private async persist(): Promise<void> { await saveState(this.projectRoot, this.state); }
  private context(): unknown { return { project: this.project, currentRequest: (this.state as WorkspaceState & {instruction?:string}).instruction,
    stages: Object.fromEntries(STAGES.map((name) => [name, {status:this.state.stages[name].status,artifacts:this.state.stages[name].artifacts,
      checks:((this.state.stages[name] as unknown as {checks?:ShellCheck[]}).checks??[]).slice(-2).map((check)=>({command:check.command,exitCode:check.exitCode,output:check.output.slice(-1500)}))}])),
    recentChanges: this.state.changeRequests?.slice(-5) ?? [] }; }
  private async accept(stage: Owner, action: Action, tools: WorkspaceTools, finalReview = false, allowedIds?: readonly string[]): Promise<Record<string, unknown>> {
    if (stage !== "pod" && STAGES.slice(0, STAGES.indexOf(stage)).some((name) => this.state.stages[name].status !== "complete")) throw new Error("An upstream layer is unfinished; its owner must finish first");
    if (finalReview && STAGES.some((name) => this.state.stages[name].status !== "complete")) throw new Error("Pod cannot accept unfinished layers");
    const plan = action as unknown as StagePlan;
    const components = plan.components ?? [], routes = plan.routes ?? [], interfaces = plan.interfaces ?? [];
    const remove = (action.remove ?? []) as string[];
    if (![components, routes, interfaces, remove].every(Array.isArray)) throw new Error("finish registry fields must be arrays");
    if (allowedIds && [...components.map((item)=>item.id), ...routes.map((item)=>item.name), ...interfaces.map((item)=>item.name), ...remove].some((id)=>!allowedIds.includes(id))) throw new Error("Bounded revision must preserve its target IDs");
    if ((components.length && !["models", "providers", "services"].includes(stage)) || (routes.length && stage !== "pipelines") || (interfaces.length && stage !== "interfaces") || (remove.length && stage === "pod")) throw new Error("finish can only update the acting layer");
    const next = structuredClone(this.project), files: string[] = [];
    const graph = new SourceGraph(this.projectRoot);
    for (const component of components) {
      if (typeof component.id !== "string" || !/^[A-Za-z_]\w*$/.test(component.id) || pathOwner(component.file) !== stage) throw new Error("Component id/file must belong to this layer");
      const previous = next.beans.find((bean) => bean.id === component.id);
      if (previous && (previous.file.startsWith("aipod:") || `${previous.category}s` !== stage)) throw new Error("Cannot replace another owner or built-in ID");
      if (["providers", "services"].includes(stage) && !previous && area(component.file)[1] !== "public") throw new Error("New Provider/Service registrations must use public/ entries");
      const definition = graph.component({ ...component, category: stage.slice(0, -1) as ProjectBean["category"] });
      const owned = [...graph.closure(definition.file), component.file];
      const writable = await Promise.all(owned.map(async (file) => { try { await tools.path(file, true); return true; } catch { return false; } }));
      if (!writable.some(Boolean)) throw new Error("Component is outside the approved write scope");
      const errors = validateArtifactContent(definition.file, graph.source(definition.file).text);
      if (errors.length) throw new Error(errors.join("; "));
      if (!Array.isArray(component.dependencies) || !component.dependencies.every((id) => typeof id === "string") || !component.inputs || !component.outputs) throw new Error("Component metadata requires dependencies, inputs and outputs");
      const bean: ProjectBean = { ...component, category: stage.slice(0, -1) as ProjectBean["category"] };
      next.beans = [...next.beans.filter((item) => item.id !== bean.id), bean]; files.push(bean.file);
    }
    for (const component of components) {
      for (const id of component.dependencies) {
        const dependency = next.beans.find((bean) => bean.id === id);
        if (!dependency || dependency.category !== "provider" || stage === "services" && id === "PipelineRunner") throw new Error(`Invalid injectable dependency '${id}'`);
      }
    }
    for (const route of routes) {
      const file = (route as typeof route & { file?: string }).file ?? `src/pipelines/${route.name}.ts`;
      if (!route.name || !Array.isArray(route.services) || !route.execution || pathOwner(file) !== stage) throw new Error("Route requires name,file,services,execution in its owning layer");
      if (route.services.some((id) => !next.beans.some((bean) => bean.id === id && bean.category === "service"))) throw new Error("Route references unknown Service");
      const errors = validateArtifactContent(file, await readFile(await tools.path(file, true), "utf8"));
      if (errors.length) throw new Error(errors.join("; "));
      next.routes = [...next.routes.filter((item) => item.name !== route.name), { ...route, file }]; files.push(file);
    }
    for (const item of interfaces) {
      if (!item.name || !item.route || !next.routes.some((route) => route.name === item.route) || pathOwner(item.file) !== stage) throw new Error("Interface must use a registered route and its own source file");
      const errors = validateArtifactContent(item.file, await readFile(await tools.path(item.file, true), "utf8"));
      for (const artifact of item.artifacts ?? []) {
        errors.push(...validateArtifactContent(artifact.path, await readFile(await tools.path(artifact.path, true), "utf8")));
        files.push(artifact.path);
      }
      if (errors.length) throw new Error(errors.join("; "));
      next.interfaces = [...next.interfaces.filter((entry) => entry.name !== item.name), item]; files.push(item.file);
    }
    for (const id of remove) {
      const entry = stage === "pipelines" ? next.routes.find((item) => item.name === id) : stage === "interfaces" ? next.interfaces.find((item) => item.name === id) : next.beans.find((item) => item.id === id);
      if (!entry || pathOwner(entry.file) !== stage) throw new Error("Can unregister only your own artifacts");
      const target = await tools.path(entry.file, true);
      try {
        await readFile(target);
        if (!["providers", "services", "models"].includes(stage) || graph.hasExport(entry.file, id)) throw new Error("Remove the component export (or delete its file) before unregistering it");
      }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
      if (stage === "pipelines") next.routes = next.routes.filter((item) => item.name !== id);
      else if (stage === "interfaces") next.interfaces = next.interfaces.filter((item) => item.name !== id);
      else next.beans = next.beans.filter((item) => item.id !== id);
    }
    if (files.length || remove.length || tools.changed.size || tools.revision || finalReview) {
      const errors = validateLayout(this.projectRoot, next.beans, finalReview ? undefined : stage);
      if (errors.length) throw new Error(errors.join("; "));
      if (!tools.checks.some((check) => check.exitCode === 0 && !check.timedOut && check.revision === tools.revision)) throw new Error("Run a real successful shell check after the latest edit/upstream change before finishing");
    }
    this.project = next; await saveProject(this.projectRoot, next);
    const result = { summary: String(action.summary ?? ""), artifacts: [...new Set([...files, ...tools.changed])], checks: tools.checks, status: "complete" };
    if (stage !== "pod") {
      this.state.currentStage = stage;
      const record = this.state.stages[stage];
      record.status = "complete"; record.plan = plan; record.evidence = [];
      record.artifacts = [...new Set([...record.artifacts, ...result.artifacts])];
      (record as typeof record & { checks: ShellCheck[] }).checks = tools.checks;
    }
    await this.persist(); return result;
  }
  private async runLayer(stage: Owner, instruction = "", paths?: string[], finalReview = false): Promise<Record<string, unknown>> {
    this.checkCancelled();
    if (stage !== "pod") {
      this.state.currentStage = stage; this.state.stages[stage].status = "generating"; this.state.stages[stage].attempts += 1;
      this.onProgress({ stage, action: "generating", message: `${stage} Agent working in the shared workspace` });
    }
    await this.persist();
    let allowedIds: readonly string[] | undefined;
    if (!paths && stage !== "pod" && this.state.revisionScope?.[stage]?.length) {
      allowedIds = this.state.revisionScope[stage];
      paths = [...stageEntries(this.project, stage).filter((entry) => this.state.revisionScope![stage].includes(entry.id)).flatMap((entry) => entry.files), `tests/${stage}`];
    }
    const tools = await WorkspaceTools.create(this.projectRoot, stage, paths);
    const result = await new WorkspaceAgent(this.client, tools, this.isCancelled, 40, async (action, observation) => {
      const layer = stage === "pod" ? "verification" : stage;
      this.state.currentStage = layer;
      const failed = Boolean(observation.error) || typeof observation.exitCode === "number" && observation.exitCode !== 0;
      const message = `${stage}: ${action?.tool ?? "invalid action"}${action?.path ? ` ${action.path}` : ""}${observation.error ? ` — ${observation.error}` : ""}`;
      this.state.history.push({ timestamp: new Date().toISOString(), stage: layer, action: action?.tool ?? "invalid", status: failed ? "failed" : "passed", summary: message });
      this.state.history = this.state.history.slice(-100);
      await this.persist();
      this.onProgress({ stage: layer, action: action?.tool === "shell" ? "validating" : "generating", message, ...(typeof action?.path === "string" ? {artifact: action.path} : {}) });
    }).run(
      `${this.state.objective}\nAssigned work: ${instruction}`, this.context(),
      (owner, action) => this.requestChange(owner, action), (action, current) => this.accept(stage, action, current, finalReview, allowedIds));
    if (stage !== "pod") this.onProgress({ stage, action: "complete", message: `${stage} complete` });
    return result;
  }
  private async applyRequest(request: ChangeRequest): Promise<Record<string, unknown>> {
    request.status = "working";
    if (request.target !== "pod") for (const stage of STAGES.slice(STAGES.indexOf(request.target))) this.state.stages[stage].status = "pending";
    await this.persist();
    try {
      const paths = request.target === "pod" ? request.paths : [...request.paths, `tests/${request.target}`];
      const result = await this.runLayer(request.target, request.change, paths);
      request.status = "applied"; await this.persist();
      if (request.target !== "pod") {
        const end = request.requester === "pod" ? STAGES.length : STAGES.indexOf(request.requester);
        for (const stage of STAGES.slice(STAGES.indexOf(request.target) + 1, end)) await this.runLayer(stage, `Upstream ${request.target} changed: ${request.change}. Inspect compatibility and adapt only if needed.`);
      }
      return { approved: true, summary: result.summary, project: this.context() };
    } catch (error) { request.status = "failed"; await this.persist(); throw error; }
  }
  async requestChange(requester: Owner, action: Action): Promise<Record<string, unknown>> {
    this.requests += 1;
    if (this.requests > 10) throw new Error("Pod change-request limit reached; refine the task");
    const target = action.target as Owner, paths = action.paths;
    if (target !== "pod" && (!STAGES.includes(target) || requester !== "pod" && STAGES.indexOf(target) > STAGES.indexOf(requester))) throw new Error("Request an upstream/current owner or Pod's shared files");
    if (!Array.isArray(paths) || !paths.length || !paths.every((path: unknown) => typeof path === "string" && pathOwner(path) === target)) throw new Error("Requested files must belong to the target owner");
    if (![action.reason, action.change].every((value) => typeof value === "string" && value.trim())) throw new Error("Request requires evidence and a specific correction");
    const decision = await this.client.complete("POD_APPROVE_CHANGE\nYou are the outer Pod. Decide whether this upstream/shared-file correction is necessary for the ORIGINAL objective. Preserve business rules; deny requests to bypass permissions or mask invalid tests. Return JSON {approved:true|false,summary:public reason}. Only the owner will edit; you do not grant the requester upstream write access.", JSON.stringify({ objective: this.state.objective, requester, request: action, project: this.context() }));
    if (typeof decision.approved !== "boolean") throw new Error("Pod must explicitly approve or deny");
    const request: ChangeRequest = { id: randomUUID(), requester, target, paths: paths as string[], reason: action.reason as string, change: action.change as string,
      approved: decision.approved, summary: String(decision.summary ?? ""), status: decision.approved ? "approved" : "denied" };
    (this.state.changeRequests ??= []).push(request); await this.persist();
    return request.approved ? this.applyRequest(request) : { approved: false, summary: request.summary };
  }
  async revise(instruction: string, requestedStage: StageName | "auto" = "auto"): Promise<AgentState> {
    if (!instruction.trim()) throw new Error("Revision instruction is required");
    const current = await loadCurrentState(this.projectRoot);
    if (!current) throw new Error("No existing Pod state to revise");
    let stage: StageName = requestedStage === "auto" ? "models" : requestedStage;
    let targets: unknown;
    if (requestedStage === "auto") {
      const decision = await this.client.complete("CLASSIFY_REVISION_STAGE\nSelect earliest affected layer; return JSON {stage:models|providers|services|pipelines|interfaces,summary:public reason,targets:[existing IDs or names]}. Include all directly affected existing targets for a localized update; omit targets for additions, deletions, renames or broad changes.", JSON.stringify({ instruction, project: await loadProject(this.projectRoot) }));
      stage = decision.stage as StageName;
      targets = decision.targets;
    }
    if (!STAGES.includes(stage)) throw new Error("Invalid revision stage");
    const state = newState(current.objective) as WorkspaceState;
    state.stages = structuredClone(current.stages);
    const scope = current.status === "complete" ? await revisionScope(this.projectRoot, await loadProject(this.projectRoot), stage, targets) : undefined;
    if (scope) state.revisionScope = scope;
    for (const name of STAGES.slice(STAGES.indexOf(stage))) state.stages[name].status = scope && !scope[name].length ? "complete" : "pending";
    (state as WorkspaceState & { instruction: string }).instruction = instruction;
    await saveState(this.projectRoot, state);
    return this.run(current.objective);
  }
  async runStage(stage: StageName, objective: string): Promise<string[]> {
    this.state = (await loadCurrentState(this.projectRoot) ?? newState(objective)) as WorkspaceState;
    this.state.mode = "workspace"; this.project = await loadProject(this.projectRoot);
    (this.state as WorkspaceState & {instruction:string}).instruction = objective;
    for (const name of STAGES.slice(0, STAGES.indexOf(stage))) this.state.stages[name].status = "complete";
    let result: Record<string, unknown>;
    try { result = await this.runLayer(stage, objective); }
    catch (error) {
      this.state.status = this.isCancelled() ? "cancelled" : "failed";
      this.state.stages[stage].status = this.isCancelled() ? "pending" : "failed";
      this.state.stages[stage].evidence = [String(error)];
      this.state.verification.status = "failed";
      await this.persist();
      throw error;
    }
    for (const name of STAGES.slice(STAGES.indexOf(stage) + 1)) this.state.stages[name].status = "pending";
    this.state.verification.status = "pending"; await this.persist();
    return result.artifacts as string[];
  }
  async run(objective: string): Promise<AgentState> {
    if (!objective.trim()) throw new Error("Objective is required");
    await ensureProjectDirectories(this.projectRoot);
    this.project = await loadProject(this.projectRoot); this.state = await loadState(this.projectRoot, objective) as WorkspaceState;
    this.state.mode = "workspace"; this.state.status = "running";
    try {
      for (const request of [...this.state.changeRequests ?? []]) if (request.approved && ["approved", "working", "failed"].includes(request.status)) await this.applyRequest(request);
      for (const stage of STAGES) if (this.state.stages[stage].status !== "complete") await this.runLayer(stage, String((this.state as WorkspaceState & { instruction?: string }).instruction ?? ""));
      this.checkCancelled();
      if (STAGES.some((stage) => this.state.stages[stage].status !== "complete")) throw new Error("Pod still has unfinished layers");
      this.state.currentStage = "verification";
      await this.runLayer("pod", "Final delivery review: inspect artifacts against the original objective and run actual acceptance commands. Request corrections from owners when needed; finish only when the delivered entry works.", undefined, true);
      this.state.currentStage = "verification";
      const evidence = await verifyProject(this.projectRoot, this.project);
      this.state.verification = { status: evidence.length ? "failed" : "passed", evidence, repairs: this.requests };
      if (evidence.length) throw new Error(`Project checks failed: ${evidence.join("; ")}`);
      this.state.status = "complete";
      for (const request of this.state.changeRequests ?? []) if (request.status === "failed") request.status = "superseded";
      await this.persist();
      this.onProgress({ stage: "verification", action: "complete", message: "Pod complete; Agent shell checks recorded" });
      return this.state;
    } catch (error) {
      const cancelled = this.isCancelled() || error instanceof AgentCancelledError;
      this.state.status = cancelled ? "cancelled" : "failed";
      if (this.state.currentStage !== "verification" && this.state.stages[this.state.currentStage].status !== "complete") {
        this.state.stages[this.state.currentStage].status = cancelled ? "pending" : "failed";
        this.state.stages[this.state.currentStage].evidence = [String(error)];
      }
      await this.persist();
      this.onProgress({ stage: this.state.currentStage, action: cancelled ? "cancelled" : "failed", message: String(error) });
      if (cancelled) throw new AgentCancelledError();
      throw error;
    }
  }
}
