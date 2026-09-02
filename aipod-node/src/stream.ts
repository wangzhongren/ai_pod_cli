import { PipelineContext } from "./context.js";
import type { ExecutionNode } from "./pipeline.js";
import { failure, success, type Result } from "./result.js";

export interface StreamItem {
  sequence: number;
  data: Readonly<Record<string, unknown>>;
  metadata: Readonly<Record<string, unknown>>;
}

type Stage =
  | { kind: "map"; node: ExecutionNode; concurrency: number; failurePolicy: "stop" | "skip" }
  | { kind: "batch"; size: number; outputKey: string };

export class StreamPipeline {
  readonly #source: () => AsyncIterable<Record<string, unknown>>;
  readonly #stages: Stage[];

  constructor(source: () => AsyncIterable<Record<string, unknown>>, stages: Stage[] = []) {
    this.#source = source;
    this.#stages = stages;
  }

  map(
    node: ExecutionNode,
    options: { concurrency?: number; failurePolicy?: "stop" | "skip" } = {},
  ): StreamPipeline {
    const concurrency = options.concurrency ?? 1;
    if (concurrency < 1) throw new Error("stream concurrency must be positive");
    return new StreamPipeline(this.#source, [
      ...this.#stages,
      { kind: "map", node, concurrency, failurePolicy: options.failurePolicy ?? "stop" },
    ]);
  }

  batch(size: number, outputKey = "batch"): StreamPipeline {
    if (size < 1) throw new Error("batch size must be positive");
    return new StreamPipeline(this.#source, [...this.#stages, { kind: "batch", size, outputKey }]);
  }

  async *items(context: PipelineContext): AsyncGenerator<StreamItem> {
    let incoming: AsyncIterable<StreamItem> = this.#sourceItems();
    const counters = { failed: 0 };
    for (const stage of this.#stages) {
      incoming = stage.kind === "map"
        ? this.#mapItems(incoming, stage, context, counters)
        : this.#batchItems(incoming, stage);
    }
    const started = performance.now();
    let emitted = 0;
    let lastSequence: number | undefined;
    try {
      for await (const item of incoming) {
        emitted += 1;
        lastSequence = item.sequence;
        yield item;
      }
    } finally {
      context.recordStep({
        component: "stream",
        result: { emitted, failed: counters.failed, lastSequence },
        durationMs: performance.now() - started,
        status: "success",
        mode: "stream",
      });
    }
  }

  async execute(context: PipelineContext): Promise<Result> {
    for await (const _item of this.items(context)) {
      // Consumption is intentionally bounded and does not retain events.
    }
    const trace = context.steps.at(-1);
    const output = {
      streamCount: Number((trace?.result as { emitted?: number } | undefined)?.emitted ?? 0),
      failedCount: Number((trace?.result as { failed?: number } | undefined)?.failed ?? 0),
    };
    Object.entries(output).forEach(([key, value]) => context.set(key, value));
    return success(output);
  }

  async *#sourceItems(): AsyncGenerator<StreamItem> {
    let sequence = 0;
    for await (const data of this.#source()) {
      yield Object.freeze({ sequence, data: Object.freeze({ ...data }), metadata: Object.freeze({}) });
      sequence += 1;
    }
  }

  async *#mapItems(
    incoming: AsyncIterable<StreamItem>,
    stage: Extract<Stage, { kind: "map" }>,
    parent: PipelineContext,
    counters: { failed: number },
  ): AsyncGenerator<StreamItem> {
    let pending: Promise<{ item?: StreamItem; failure?: Result }>[] = [];
    const flush = async () => {
      const values = await Promise.all(pending);
      pending = [];
      return values;
    };
    const process = async (item: StreamItem) => {
      const branch = new PipelineContext(parent.params, {
        data: { ...parent.data, ...item.data },
        branchId: `stream-${item.sequence}`,
      });
      const result = await stage.node.execute(branch);
      if (result.status === "failure") return { failure: result };
      return {
        item: Object.freeze({
          sequence: item.sequence,
          data: Object.freeze({ ...item.data, ...branch.changes(), ...result.output }),
          metadata: item.metadata,
        }),
      };
    };
    const emit = async function* (values: Awaited<ReturnType<typeof flush>>) {
      for (const value of values) {
        if (value.failure) {
          counters.failed += 1;
          if (stage.failurePolicy === "stop") {
            throw new Error(value.failure.status === "failure" ? value.failure.error.message : "stream failed");
          }
        } else if (value.item) {
          yield value.item;
        }
      }
    };
    for await (const item of incoming) {
      pending.push(process(item));
      if (pending.length >= stage.concurrency) yield* emit(await flush());
    }
    if (pending.length) yield* emit(await flush());
  }

  async *#batchItems(
    incoming: AsyncIterable<StreamItem>,
    stage: Extract<Stage, { kind: "batch" }>,
  ): AsyncGenerator<StreamItem> {
    let batch: StreamItem[] = [];
    for await (const item of incoming) {
      batch.push(item);
      if (batch.length === stage.size) {
        yield this.#makeBatch(batch, stage.outputKey);
        batch = [];
      }
    }
    if (batch.length) yield this.#makeBatch(batch, stage.outputKey);
  }

  #makeBatch(items: StreamItem[], outputKey: string): StreamItem {
    const first = items[0];
    const last = items.at(-1);
    if (!first || !last) throw new Error("Cannot create an empty batch");
    return Object.freeze({
      sequence: first.sequence,
      data: Object.freeze({ [outputKey]: items.map((item) => item.data) }),
      metadata: Object.freeze({ batchSize: items.length, lastSequence: last.sequence }),
    });
  }
}

export const stream = (source: () => AsyncIterable<Record<string, unknown>>) => new StreamPipeline(source);
