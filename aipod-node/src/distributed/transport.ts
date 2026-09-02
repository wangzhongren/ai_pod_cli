import type {
  BrokerStats, ClaimedMessage, DeadLetterRecord, DistributedMessage, StreamTransport,
} from "./types.js";

export class HttpStreamTransport implements StreamTransport {
  constructor(readonly baseUrl: string, readonly token: string) {}

  async #request<T>(path: string, body?: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        authorization: `Bearer ${this.token}`,
        "content-type": "application/json",
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const value = await response.json() as T & { error?: string };
    if (!response.ok) throw new Error(value.error ?? `Broker request failed (${response.status})`);
    return value;
  }

  publish(
    stream: string,
    payload: Record<string, unknown>,
    options: { key?: string; headers?: Record<string, string> } = {},
  ): Promise<DistributedMessage> {
    return this.#request("/publish", { stream, payload, ...options });
  }

  claim(options: {
    stream: string; group: string; consumer: string; leaseMs: number; maxAttempts: number;
  }): Promise<ClaimedMessage | undefined> {
    return this.#request<ClaimedMessage | null>("/claim", options)
      .then((value) => value ?? undefined);
  }

  ack(message: ClaimedMessage): Promise<boolean> {
    return this.#request<{ acknowledged: boolean }>("/ack", { message })
      .then((value) => value.acknowledged);
  }

  heartbeat(message: ClaimedMessage, leaseMs: number): Promise<boolean> {
    return this.#request<{ extended: boolean }>("/heartbeat", { message, leaseMs })
      .then((value) => value.extended);
  }

  fail(message: ClaimedMessage, error: string, maxAttempts: number): Promise<"retry" | "dead"> {
    return this.#request<{ disposition: "retry" | "dead" }>("/fail", {
      message, error, maxAttempts,
    }).then((value) => value.disposition);
  }

  stats(): Promise<BrokerStats> {
    return this.#request("/stats");
  }

  deadLetters(options: { stream?: string; group?: string } = {}): Promise<DeadLetterRecord[]> {
    const query = new URLSearchParams(options as Record<string, string>).toString();
    return this.#request(`/dead-letters${query ? `?${query}` : ""}`);
  }

  requeue(messageId: string, stream: string, group: string): Promise<boolean> {
    return this.#request<{ requeued: boolean }>("/requeue", { messageId, stream, group })
      .then((value) => value.requeued);
  }
}
