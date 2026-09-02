export interface TraceStep {
  component: string;
  result: unknown;
  durationMs?: number;
  [key: string]: unknown;
}

export type MergeStrategy = "strict" | "overwrite" | "collect";

function clone<T>(value: T): T {
  return structuredClone(value);
}

export class PipelineContext {
  readonly params: Record<string, unknown>;
  readonly data: Record<string, unknown>;
  readonly steps: TraceStep[] = [];
  readonly branchId?: string;
  readonly #baseline: Record<string, unknown>;

  constructor(
    params: Record<string, unknown> = {},
    options: { data?: Record<string, unknown>; branchId?: string } = {},
  ) {
    this.params = clone(params);
    this.data = clone(options.data ?? {});
    this.#baseline = clone(this.data);
    if (options.branchId !== undefined) this.branchId = options.branchId;
  }

  get<T = unknown>(key: string, fallback?: T): T | unknown {
    if (key in this.data) return this.data[key];
    if (key in this.params) return this.params[key];
    return fallback;
  }

  set(key: string, value: unknown): void {
    this.data[key] = value;
  }

  recordStep(step: TraceStep): void {
    this.steps.push(step);
  }

  fork(branchId?: string): PipelineContext {
    return new PipelineContext(this.params, {
      data: this.data,
      ...(branchId !== undefined ? { branchId } : {}),
    });
  }

  changes(): Record<string, unknown> {
    return Object.fromEntries(
      Object.entries(this.data).filter(
        ([key, value]) => !(key in this.#baseline) || !isDeepStrictEqual(this.#baseline[key], value),
      ),
    );
  }

  merge(branches: PipelineContext[], strategy: MergeStrategy = "strict"): Record<string, unknown> {
    const writes = new Map<string, unknown[]>();
    for (const branch of branches) {
      for (const [key, value] of Object.entries(branch.changes())) {
        writes.set(key, [...(writes.get(key) ?? []), value]);
      }
    }
    const merged: Record<string, unknown> = {};
    for (const [key, values] of writes) {
      const first = values[0];
      if (strategy === "strict" && values.some((value) => !Object.is(value, first))) {
        throw new Error(`parallel branches produced conflicting values for '${key}'`);
      }
      merged[key] = strategy === "collect" && values.length > 1
        ? values
        : strategy === "overwrite"
          ? values.at(-1)
          : first;
    }
    Object.assign(this.data, merged);
    return merged;
  }

  summary(): Record<string, unknown> {
    return { params: this.params, data: this.data, steps: this.steps };
  }
}
import { isDeepStrictEqual } from "node:util";
