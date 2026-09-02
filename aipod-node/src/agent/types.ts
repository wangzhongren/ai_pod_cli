import type { Contract } from "../contracts.js";

export const STAGES = ["models", "providers", "services", "pipelines", "interfaces"] as const;
export type StageName = typeof STAGES[number];
export type StageStatus = "pending" | "planning" | "generating" | "complete" | "failed";

export interface ComponentPlan {
  id: string;
  description: string;
  file: string;
  dependencies: string[];
  inputs: Contract;
  outputs: Contract;
}

export interface RoutePlan {
  name: string;
  description: string;
  services: string[];
  execution: {
    mode: "sequential" | "parallel" | "repeat";
    untilField?: string;
    maxIterationsField?: string;
    outputField?: string;
    concurrency?: number;
    merge?: "strict" | "overwrite" | "collect";
  };
}

export interface InterfacePlan {
  name: string;
  description: string;
  file: string;
  route: string;
  kind: "cli" | "web" | "desktop" | "worker" | "consumer";
  artifacts?: InterfaceArtifactPlan[];
  lifecycle?: {
    run?: string[];
    install?: string[];
    uninstall?: string[];
  };
  permissions?: string[];
  verify?: InterfaceVerificationPlan[];
}

export interface InterfaceArtifactPlan {
  path: string;
  role: "adapter_module" | "installer" | "uninstaller" | "metadata" | "resource";
  format: "typescript" | "javascript" | "json" | "shell" | "text";
  instruction: string;
}

export interface InterfaceVerificationPlan {
  name: string;
  command: string[];
  timeoutMs: number;
  required: boolean;
}

export interface StagePlan {
  summary: string;
  components?: ComponentPlan[];
  routes?: RoutePlan[];
  interfaces?: InterfacePlan[];
}

export interface StageRecord {
  status: StageStatus;
  attempts: number;
  plan?: StagePlan;
  artifacts: string[];
  evidence: string[];
}

export interface AgentHistoryItem {
  timestamp: string;
  stage: StageName | "verification";
  action: string;
  status: "started" | "passed" | "failed";
  summary: string;
}

export interface AgentState {
  version: 1;
  objective: string;
  status: "running" | "complete" | "failed" | "cancelled";
  currentStage: StageName | "verification";
  stages: Record<StageName, StageRecord>;
  verification: {
    status: "pending" | "passed" | "failed";
    evidence: string[];
    repairs: number;
  };
  history: AgentHistoryItem[];
}

export interface AgentEvent {
  stage: StageName | "verification";
  action: "planning" | "generating" | "validating" | "committing" | "repairing" | "complete" | "failed" | "cancelled";
  message: string;
  artifact?: string;
}

export interface ModelClient {
  complete(system: string, user: string): Promise<Record<string, unknown>>;
}
