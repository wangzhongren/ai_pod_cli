import { isDeepStrictEqual } from "node:util";
import { validateContract, validateContractValue, type Contract, type InferContract } from "./contracts.js";

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

  /** A checked view: reads use input contracts, writes use output contracts. */
  typed<const I extends Contract, const O extends Contract>(inputs: I, outputs: O): ContractContext<I, O> {
    return new ContractContext(this, inputs, outputs);
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

export class ContractContext<I extends Contract, O extends Contract> {
  readonly #inputs: I;
  readonly #outputs: O;

  constructor(readonly context: PipelineContext, inputs: I, outputs: O) {
    this.#inputs = clone(inputs);
    this.#outputs = clone(outputs);
    const errors = validateContract({ ...context.params, ...context.data }, inputs);
    if (errors.length) throw new Error(errors.join("; "));
  }

  get<K extends keyof InferContract<I> & string>(key: K): InferContract<I>[K] {
    if (!Object.hasOwn(this.#inputs, key)) throw new Error(`Undeclared input '${key}'`);
    // Recheck reads because other steps may have changed the shared Context.
    const values = { ...this.context.params, ...this.context.data };
    const errors = validateContract(values, { [key]: this.#inputs[key]! });
    if (errors.length) throw new Error(errors.join("; "));
    return clone(values[key]) as InferContract<I>[K];
  }

  set<K extends keyof InferContract<O> & string>(key: K, value: InferContract<O>[K]): void {
    if (!Object.hasOwn(this.#outputs, key)) throw new Error(`Undeclared output '${key}'`);
    const errors = validateContractValue(value, this.#outputs[key]!, `$.${key}`);
    if (errors.length) throw new Error(errors.join("; "));
    this.context.set(key, clone(value));
  }

  output(value: InferContract<O>): InferContract<O> {
    const errors = validateContract(value as Record<string, unknown>, this.#outputs);
    if (errors.length) throw new Error(errors.join("; "));
    return clone(value);
  }
}
