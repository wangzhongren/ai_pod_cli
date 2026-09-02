import { Container, type Service } from "./container.js";
import { PipelineContext, type MergeStrategy } from "./context.js";
import { validateContract } from "./contracts.js";
import { failure, normalizeResult, success, type Failure, type Result } from "./result.js";

export interface ExecutionNode {
  execute(context: PipelineContext): Promise<Result>;
}

export type FailurePolicy = "fail-fast" | "collect-all" | "ignore";

const delay = (milliseconds: number) => new Promise((resolve) => setTimeout(resolve, milliseconds));

export class ServiceRef implements ExecutionNode {
  readonly #container: Container;
  readonly #id: string;
  readonly #retries: number;
  readonly #delayMs: number;
  readonly #fallback?: ServiceRef;

  constructor(
    container: Container,
    id: string,
    policy: { retries?: number; delayMs?: number; fallback?: ServiceRef } = {},
  ) {
    const definition = container.definition(id);
    if (definition.category !== "service") throw new Error(`'${id}' is not a Service`);
    this.#container = container;
    this.#id = id;
    this.#retries = policy.retries ?? 0;
    this.#delayMs = policy.delayMs ?? 0;
    if (policy.fallback) this.#fallback = policy.fallback;
  }

  retry(retries = 3, delayMs = 0): ServiceRef {
    if (retries < 1 || delayMs < 0) throw new Error("Invalid retry policy");
    return new ServiceRef(this.#container, this.#id, {
      retries,
      delayMs,
      ...(this.#fallback ? { fallback: this.#fallback } : {}),
    });
  }

  fallback(reference: ServiceRef): ServiceRef {
    return new ServiceRef(this.#container, this.#id, {
      retries: this.#retries,
      delayMs: this.#delayMs,
      fallback: reference,
    });
  }

  pipe(...nodes: ExecutionNode[]): SequenceNode {
    return sequence(this, ...nodes);
  }

  async execute(context: PipelineContext): Promise<Result> {
    const definition = this.#container.definition(this.#id);
    const started = performance.now();
    let result: Result = failure("Service did not execute");
    let attempts = 0;
    for (let attempt = 0; attempt <= this.#retries; attempt += 1) {
      attempts = attempt + 1;
      const inputErrors = validateContract(
        { ...context.params, ...context.data },
        definition.inputs,
        this.#id,
      );
      if (inputErrors.length) {
        result = failure(inputErrors.join("; "), { code: "input_contract" });
        break;
      }
      try {
        const service = this.#container.resolve<Service>(this.#id);
        result = normalizeResult(await service.execute(context));
        if (result.status === "success") {
          const outputErrors = validateContract(
            { ...context.data, ...result.output },
            definition.outputs,
            this.#id,
          );
          if (outputErrors.length) {
            result = failure(outputErrors.join("; "), { code: "output_contract" });
          } else {
            Object.entries(result.output).forEach(([key, value]) => context.set(key, value));
            break;
          }
        }
      } catch (error) {
        result = failure(error instanceof Error ? error.message : String(error), {
          code: error instanceof Error ? error.name : "service_error",
          retryable: true,
        });
      }
      if (attempt < this.#retries && this.#delayMs) await delay(this.#delayMs);
    }
    let fallback: string | undefined;
    if (result.status === "failure" && this.#fallback) {
      fallback = "configured";
      result = await this.#fallback.execute(context);
    }
    context.recordStep({
      component: this.#id,
      result,
      durationMs: performance.now() - started,
      status: result.status,
      attempts,
      ...(fallback ? { fallback } : {}),
    });
    return result;
  }
}

export class SequenceNode implements ExecutionNode {
  readonly #nodes: ExecutionNode[];

  constructor(nodes: ExecutionNode[]) {
    this.#nodes = nodes;
  }

  pipe(...nodes: ExecutionNode[]): SequenceNode {
    return new SequenceNode([...this.#nodes, ...nodes]);
  }

  async execute(context: PipelineContext): Promise<Result> {
    let result: Result = success();
    for (const node of this.#nodes) {
      result = await node.execute(context);
      if (result.status === "failure") break;
    }
    return result;
  }
}

export class ParallelNode implements ExecutionNode {
  constructor(
    readonly branches: ExecutionNode[],
    readonly options: {
      merge?: MergeStrategy;
      failurePolicy?: FailurePolicy;
      concurrency?: number;
    } = {},
  ) {
    if (branches.length < 2) throw new Error("parallel requires at least two branches");
    if ((options.concurrency ?? branches.length) < 1) throw new Error("Invalid concurrency");
  }

  pipe(...nodes: ExecutionNode[]): SequenceNode {
    return sequence(this, ...nodes);
  }

  async execute(context: PipelineContext): Promise<Result> {
    const started = performance.now();
    const concurrency = this.options.concurrency ?? this.branches.length;
    const branchContexts = this.branches.map((_, index) => context.fork(`branch-${index + 1}`));
    const results: Result[] = new Array(this.branches.length);
    let cursor = 0;
    let stopped = false;
    const worker = async () => {
      while (!stopped) {
        const index = cursor++;
        if (index >= this.branches.length) return;
        const branch = this.branches[index];
        const branchContext = branchContexts[index];
        if (!branch || !branchContext) return;
        results[index] = await branch.execute(branchContext);
        if (results[index]?.status === "failure" && (this.options.failurePolicy ?? "fail-fast") === "fail-fast") {
          stopped = true;
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(concurrency, this.branches.length) }, worker));
    const completed = results
      .map((result, index) => ({ result, context: branchContexts[index]! }))
      .filter((item): item is { result: Result; context: PipelineContext } => item.result !== undefined);
    const failures = completed.filter(({ result }) => result.status === "failure");
    const successful = completed.filter(({ result }) => result.status === "success");
    let result: Result;
    try {
      const merged = context.merge(
        successful.map((item) => item.context),
        this.options.merge ?? "strict",
      );
      result = failures.length && (this.options.failurePolicy ?? "fail-fast") !== "ignore"
        ? failure(`${failures.length} parallel branch(es) failed`, { code: "parallel_failure" })
        : success(merged);
    } catch (error) {
      result = failure(error instanceof Error ? error.message : String(error), {
        code: "parallel_merge_conflict",
      });
    }
    context.recordStep({
      component: "parallel",
      result,
      durationMs: performance.now() - started,
      status: result.status,
      mode: "parallel",
      branches: completed.map(({ result: branchResult, context: branchContext }) => ({
        id: branchContext.branchId,
        status: branchResult.status,
        steps: branchContext.steps,
      })),
    });
    return result;
  }
}

export class RepeatNode implements ExecutionNode {
  constructor(
    readonly body: ExecutionNode,
    readonly options: {
      untilField?: string;
      maxIterations?: number;
      maxIterationsField?: string;
      outputField?: string;
      traceLimit?: number;
    },
  ) {
    if (!options.untilField && options.maxIterations === undefined && !options.maxIterationsField) {
      throw new Error("repeat requires a stop field or iteration limit");
    }
  }

  async execute(context: PipelineContext): Promise<Result> {
    const started = performance.now();
    const dynamicLimit = this.options.maxIterationsField
      ? context.get(this.options.maxIterationsField)
      : undefined;
    const limit = this.options.maxIterations ?? dynamicLimit;
    if (limit !== undefined && (!Number.isInteger(limit) || Number(limit) < 1)) {
      throw new Error("repeat iteration limit must be a positive integer");
    }
    const traces: unknown[] = [];
    const traceLimit = this.options.traceLimit ?? 20;
    let iterations = 0;
    let result: Result = success();
    let stopReason = "until";
    while (true) {
      const iteration = context.fork(`iteration-${iterations + 1}`);
      result = await this.body.execute(iteration);
      iterations += 1;
      traces.push({ iteration: iterations, status: result.status, steps: iteration.steps });
      if (traces.length > traceLimit) traces.shift();
      if (result.status === "failure") {
        stopReason = "failure";
        break;
      }
      context.merge([iteration], "overwrite");
      if (this.options.untilField && Boolean(context.get(this.options.untilField))) break;
      if (limit !== undefined && iterations >= Number(limit)) {
        stopReason = "limit";
        break;
      }
    }
    if (result.status === "success") {
      const outputField = this.options.outputField ?? "iterations";
      context.set(outputField, iterations);
      result = success({ [outputField]: iterations });
    }
    context.recordStep({
      component: "repeat",
      result,
      durationMs: performance.now() - started,
      status: result.status,
      mode: "repeat",
      iterations,
      stopReason,
      iterationTraces: traces,
    });
    return result;
  }
}

export const service = (container: Container, id: string) => new ServiceRef(container, id);
export const sequence = (...nodes: ExecutionNode[]) => new SequenceNode(nodes);
export const parallel = (
  branches: ExecutionNode[],
  options: ConstructorParameters<typeof ParallelNode>[1] = {},
) => new ParallelNode(branches, options);
export const repeat = (
  body: ExecutionNode,
  options: ConstructorParameters<typeof RepeatNode>[1],
) => new RepeatNode(body, options);
