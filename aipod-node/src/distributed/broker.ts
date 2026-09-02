import { randomUUID, timingSafeEqual } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { dirname, resolve } from "node:path";

import type {
  BrokerStats, ClaimedMessage, DeadLetterRecord, DistributedMessage,
} from "./types.js";

type DeliveryStatus = "ready" | "inflight" | "acked" | "dead";

interface Delivery {
  status: DeliveryStatus;
  attempts: number;
  consumer?: string;
  leaseUntil?: string;
  lastError?: string;
}

interface DeadLetter {
  messageId: string;
  stream: string;
  group: string;
  attempts: number;
  error: string;
  failedAt: string;
}

interface BrokerState {
  version: 1;
  messages: Record<string, DistributedMessage[]>;
  deliveries: Record<string, Delivery>;
  deadLetters: DeadLetter[];
}

const emptyState = (): BrokerState => ({
  version: 1, messages: {}, deliveries: {}, deadLetters: [],
});

const deliveryKey = (stream: string, group: string, messageId: string) =>
  `${stream}\u0000${group}\u0000${messageId}`;

export class PersistentStreamBroker {
  #state: BrokerState = emptyState();
  #queue: Promise<unknown> = Promise.resolve();

  private constructor(readonly file: string) {}

  static async open(file: string): Promise<PersistentStreamBroker> {
    const broker = new PersistentStreamBroker(resolve(file));
    try {
      const parsed = JSON.parse(await readFile(broker.file, "utf8")) as BrokerState;
      if (parsed.version !== 1) throw new Error("Unsupported Broker state version");
      broker.#state = parsed;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    return broker;
  }

  async publish(
    stream: string,
    payload: Record<string, unknown>,
    options: { key?: string; headers?: Record<string, string> } = {},
  ): Promise<DistributedMessage> {
    if (!stream.trim()) throw new Error("Stream name is required");
    return this.#mutate(async () => {
      this.#state.messages[stream] ??= [];
      if (options.key) {
        const existing = this.#state.messages[stream]!.find((message) => message.key === options.key);
        if (existing) return structuredClone(existing);
      }
      const message: DistributedMessage = {
        id: randomUUID(), stream,
        ...(options.key ? { key: options.key } : {}),
        payload: structuredClone(payload),
        headers: structuredClone(options.headers ?? {}),
        createdAt: new Date().toISOString(),
      };
      this.#state.messages[stream]!.push(message);
      await this.#persist();
      return structuredClone(message);
    });
  }

  async claim(options: {
    stream: string; group: string; consumer: string;
    leaseMs: number; maxAttempts: number;
  }): Promise<ClaimedMessage | undefined> {
    if (!options.group.trim() || !options.consumer.trim()) {
      throw new Error("Consumer group and consumer are required");
    }
    if (options.leaseMs < 100) throw new Error("leaseMs must be at least 100");
    if (options.maxAttempts < 1) throw new Error("maxAttempts must be positive");
    return this.#mutate(async () => {
      const now = Date.now();
      let changed = false;
      for (const message of this.#state.messages[options.stream] ?? []) {
        const key = deliveryKey(options.stream, options.group, message.id);
        const delivery = this.#state.deliveries[key] ?? { status: "ready", attempts: 0 };
        const expired = delivery.status === "inflight"
          && Date.parse(delivery.leaseUntil ?? "") <= now;
        if (delivery.status !== "ready" && !expired) continue;
        if (delivery.attempts >= options.maxAttempts) {
          this.#deadLetter(message, options.group, delivery, delivery.lastError ?? "lease expired");
          changed = true;
          continue;
        }
        const leaseUntil = new Date(now + options.leaseMs).toISOString();
        const claimed: Delivery = {
          ...delivery,
          status: "inflight",
          attempts: delivery.attempts + 1,
          consumer: options.consumer,
          leaseUntil,
        };
        this.#state.deliveries[key] = claimed;
        await this.#persist();
        return {
          ...structuredClone(message),
          group: options.group,
          consumer: options.consumer,
          attempt: claimed.attempts,
          leaseUntil,
        };
      }
      if (changed) await this.#persist();
      return undefined;
    });
  }

  async ack(message: ClaimedMessage): Promise<boolean> {
    return this.#mutate(async () => {
      const delivery = this.#delivery(message);
      if (!delivery || delivery.status !== "inflight" || delivery.consumer !== message.consumer) return false;
      delivery.status = "acked";
      delete delivery.leaseUntil;
      await this.#persist();
      return true;
    });
  }

  async heartbeat(message: ClaimedMessage, leaseMs: number): Promise<boolean> {
    return this.#mutate(async () => {
      const delivery = this.#delivery(message);
      if (!delivery || delivery.status !== "inflight" || delivery.consumer !== message.consumer) return false;
      delivery.leaseUntil = new Date(Date.now() + leaseMs).toISOString();
      await this.#persist();
      return true;
    });
  }

  async fail(
    message: ClaimedMessage,
    error: string,
    maxAttempts: number,
  ): Promise<"retry" | "dead"> {
    return this.#mutate(async () => {
      const delivery = this.#delivery(message);
      if (!delivery || delivery.status !== "inflight" || delivery.consumer !== message.consumer) {
        throw new Error("Message lease is not owned by this consumer");
      }
      delivery.lastError = error.slice(0, 2_000);
      delete delivery.leaseUntil;
      if (delivery.attempts >= maxAttempts) {
        const original = (this.#state.messages[message.stream] ?? []).find((item) => item.id === message.id);
        if (original) this.#deadLetter(original, message.group, delivery, delivery.lastError);
        await this.#persist();
        return "dead";
      }
      delivery.status = "ready";
      await this.#persist();
      return "retry";
    });
  }

  stats(): BrokerStats {
    const deliveries = { ready: 0, inflight: 0, acked: 0, dead: 0 };
    Object.values(this.#state.deliveries).forEach((delivery) => { deliveries[delivery.status] += 1; });
    return {
      streams: Object.fromEntries(
        Object.entries(this.#state.messages).map(([stream, messages]) => [stream, messages.length]),
      ),
      deliveries,
      deadLetters: this.#state.deadLetters.length,
    };
  }

  deadLetters(options: { stream?: string; group?: string } = {}): DeadLetterRecord[] {
    return this.#state.deadLetters
      .filter((item) => !options.stream || item.stream === options.stream)
      .filter((item) => !options.group || item.group === options.group)
      .flatMap((item) => {
        const message = (this.#state.messages[item.stream] ?? [])
          .find((value) => value.id === item.messageId);
        return message ? [{
          message: structuredClone(message), group: item.group,
          attempts: item.attempts, error: item.error, failedAt: item.failedAt,
        }] : [];
      });
  }

  async requeue(messageId: string, stream: string, group: string): Promise<boolean> {
    return this.#mutate(async () => {
      const key = deliveryKey(stream, group, messageId);
      const delivery = this.#state.deliveries[key];
      if (!delivery || delivery.status !== "dead") return false;
      delivery.status = "ready";
      delivery.attempts = 0;
      delete delivery.consumer;
      delete delivery.leaseUntil;
      delete delivery.lastError;
      this.#state.deadLetters = this.#state.deadLetters.filter(
        (item) => !(item.messageId === messageId && item.stream === stream && item.group === group),
      );
      await this.#persist();
      return true;
    });
  }

  #delivery(message: ClaimedMessage): Delivery | undefined {
    return this.#state.deliveries[deliveryKey(message.stream, message.group, message.id)];
  }

  #deadLetter(message: DistributedMessage, group: string, delivery: Delivery, error: string): void {
    delivery.status = "dead";
    delete delivery.leaseUntil;
    if (!this.#state.deadLetters.some((item) => item.messageId === message.id && item.group === group)) {
      this.#state.deadLetters.push({
        messageId: message.id, stream: message.stream, group,
        attempts: delivery.attempts, error, failedAt: new Date().toISOString(),
      });
    }
  }

  async #persist(): Promise<void> {
    await mkdir(dirname(this.file), { recursive: true });
    const temporary = `${this.file}.tmp`;
    await writeFile(temporary, `${JSON.stringify(this.#state, null, 2)}\n`);
    await rename(temporary, this.file);
  }

  #mutate<T>(operation: () => Promise<T>): Promise<T> {
    const next = this.#queue.then(operation, operation);
    this.#queue = next.then(() => undefined, () => undefined);
    return next;
  }
}

const secureEqual = (left: string, right: string) => {
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
};

export async function startStreamBroker(options: {
  file: string; host?: string; port?: number; token?: string;
}): Promise<{
  url: string; token: string; close(): Promise<void>; broker: PersistentStreamBroker;
}> {
  const broker = await PersistentStreamBroker.open(options.file);
  const token = options.token ?? randomUUID();
  const server = createServer(async (request, response) => {
    const send = (status: number, value: unknown) => {
      response.writeHead(status, { "content-type": "application/json" });
      response.end(JSON.stringify(value));
    };
    try {
      if (!secureEqual(String(request.headers.authorization ?? ""), `Bearer ${token}`)) {
        send(403, { error: "Forbidden" }); return;
      }
      const url = new URL(request.url ?? "/", "http://localhost");
      if (request.method === "GET" && url.pathname === "/stats") {
        send(200, broker.stats()); return;
      }
      if (request.method === "GET" && url.pathname === "/dead-letters") {
        send(200, broker.deadLetters({
          ...(url.searchParams.get("stream") ? { stream: url.searchParams.get("stream")! } : {}),
          ...(url.searchParams.get("group") ? { group: url.searchParams.get("group")! } : {}),
        })); return;
      }
      let raw = "";
      for await (const chunk of request) {
        raw += String(chunk);
        if (raw.length > 1_000_000) throw new Error("Request body is too large");
      }
      const value = raw ? JSON.parse(raw) as Record<string, unknown> : {};
      if (request.method === "POST" && url.pathname === "/publish") {
        send(200, await broker.publish(
          String(value.stream ?? ""),
          (value.payload ?? {}) as Record<string, unknown>,
          {
            ...(value.key ? { key: String(value.key) } : {}),
            ...(value.headers ? { headers: value.headers as Record<string, string> } : {}),
          },
        )); return;
      }
      if (request.method === "POST" && url.pathname === "/claim") {
        send(200, await broker.claim({
          stream: String(value.stream ?? ""), group: String(value.group ?? ""),
          consumer: String(value.consumer ?? ""), leaseMs: Number(value.leaseMs ?? 30_000),
          maxAttempts: Number(value.maxAttempts ?? 3),
        }) ?? null); return;
      }
      const message = value.message as ClaimedMessage;
      if (request.method === "POST" && url.pathname === "/ack") {
        send(200, { acknowledged: await broker.ack(message) }); return;
      }
      if (request.method === "POST" && url.pathname === "/heartbeat") {
        send(200, { extended: await broker.heartbeat(message, Number(value.leaseMs ?? 30_000)) }); return;
      }
      if (request.method === "POST" && url.pathname === "/fail") {
        send(200, { disposition: await broker.fail(
          message, String(value.error ?? "failed"), Number(value.maxAttempts ?? 3),
        ) }); return;
      }
      if (request.method === "POST" && url.pathname === "/requeue") {
        send(200, { requeued: await broker.requeue(
          String(value.messageId ?? ""), String(value.stream ?? ""), String(value.group ?? ""),
        ) }); return;
      }
      send(404, { error: "Not found" });
    } catch (error) {
      send(400, { error: error instanceof Error ? error.message : String(error) });
    }
  });
  await new Promise<void>((accept, reject) => {
    server.once("error", reject);
    server.listen(options.port ?? 0, options.host ?? "127.0.0.1", accept);
  });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Cannot resolve Broker address");
  return {
    url: `http://${address.address}:${address.port}`,
    token,
    broker,
    close: () => new Promise<void>((accept, reject) =>
      server.close((error) => error ? reject(error) : accept())
    ),
  };
}
