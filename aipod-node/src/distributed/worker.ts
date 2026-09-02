import type { PipelineRunner } from "../runner.js";
import type { ClaimedMessage, StreamTransport } from "./types.js";

export interface WorkerMetrics {
  claimed: number;
  acknowledged: number;
  retried: number;
  dead: number;
  leaseLost: number;
}

const wait = (milliseconds: number, signal?: AbortSignal) => new Promise<void>((accept) => {
  if (signal?.aborted) { accept(); return; }
  const timeout = setTimeout(accept, milliseconds);
  signal?.addEventListener("abort", () => {
    clearTimeout(timeout);
    accept();
  }, { once: true });
});

export class DistributedWorker {
  readonly metrics: WorkerMetrics = {
    claimed: 0, acknowledged: 0, retried: 0, dead: 0, leaseLost: 0,
  };

  constructor(
    readonly transport: StreamTransport,
    readonly runner: PipelineRunner,
    readonly options: {
      stream: string;
      group: string;
      consumer: string;
      route: string;
      concurrency?: number;
      leaseMs?: number;
      maxAttempts?: number;
      pollMs?: number;
    },
  ) {
    if ((options.concurrency ?? 1) < 1) throw new Error("Worker concurrency must be positive");
  }

  async run(signal?: AbortSignal): Promise<WorkerMetrics> {
    const concurrency = this.options.concurrency ?? 1;
    await Promise.all(Array.from({ length: concurrency }, (_, index) =>
      this.#loop(index, signal)
    ));
    return { ...this.metrics };
  }

  async #loop(index: number, signal?: AbortSignal): Promise<void> {
    const consumer = `${this.options.consumer}-${index + 1}`;
    while (!signal?.aborted) {
      const message = await this.transport.claim({
        stream: this.options.stream,
        group: this.options.group,
        consumer,
        leaseMs: this.options.leaseMs ?? 30_000,
        maxAttempts: this.options.maxAttempts ?? 3,
      });
      if (!message) {
        await wait(this.options.pollMs ?? 250, signal);
        continue;
      }
      this.metrics.claimed += 1;
      await this.#process(message);
    }
  }

  async #process(message: ClaimedMessage): Promise<void> {
    const leaseMs = this.options.leaseMs ?? 30_000;
    const heartbeat = setInterval(() => {
      void this.transport.heartbeat(message, leaseMs).then((extended) => {
        if (!extended) this.metrics.leaseLost += 1;
      }).catch(() => { this.metrics.leaseLost += 1; });
    }, Math.max(100, Math.floor(leaseMs / 3)));
    heartbeat.unref();
    try {
      const execution = await this.runner.run(this.options.route, {
        ...message.payload,
        messageId: message.id,
        messageKey: message.key,
        messageHeaders: message.headers,
        deliveryAttempt: message.attempt,
      });
      if (execution.result.status === "failure") {
        await this.#fail(message, execution.result.error.message);
        return;
      }
      if (await this.transport.ack(message)) this.metrics.acknowledged += 1;
      else this.metrics.leaseLost += 1;
    } catch (error) {
      await this.#fail(message, error instanceof Error ? error.message : String(error));
    } finally {
      clearInterval(heartbeat);
    }
  }

  async #fail(message: ClaimedMessage, error: string): Promise<void> {
    const disposition = await this.transport.fail(
      message, error, this.options.maxAttempts ?? 3,
    );
    this.metrics[disposition === "dead" ? "dead" : "retried"] += 1;
  }
}

export class DistributedStreamPublisher {
  constructor(readonly transport: StreamTransport, readonly stream: string) {}

  publish(
    payload: Record<string, unknown>,
    options: { key?: string; headers?: Record<string, string> } = {},
  ) {
    return this.transport.publish(this.stream, payload, options);
  }
}
