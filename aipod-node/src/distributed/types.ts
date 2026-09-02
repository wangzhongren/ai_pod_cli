export interface DistributedMessage {
  id: string;
  stream: string;
  key?: string;
  payload: Record<string, unknown>;
  headers: Record<string, string>;
  createdAt: string;
}

export interface ClaimedMessage extends DistributedMessage {
  group: string;
  consumer: string;
  attempt: number;
  leaseUntil: string;
}

export interface BrokerStats {
  streams: Record<string, number>;
  deliveries: {
    ready: number;
    inflight: number;
    acked: number;
    dead: number;
  };
  deadLetters: number;
}

export interface DeadLetterRecord {
  message: DistributedMessage;
  group: string;
  attempts: number;
  error: string;
  failedAt: string;
}

export interface StreamTransport {
  publish(
    stream: string,
    payload: Record<string, unknown>,
    options?: { key?: string; headers?: Record<string, string> },
  ): Promise<DistributedMessage>;
  claim(options: {
    stream: string;
    group: string;
    consumer: string;
    leaseMs: number;
    maxAttempts: number;
  }): Promise<ClaimedMessage | undefined>;
  ack(message: ClaimedMessage): Promise<boolean>;
  heartbeat(message: ClaimedMessage, leaseMs: number): Promise<boolean>;
  fail(message: ClaimedMessage, error: string, maxAttempts: number): Promise<"retry" | "dead">;
  stats(): BrokerStats | Promise<BrokerStats>;
  deadLetters(options?: { stream?: string; group?: string }): DeadLetterRecord[] | Promise<DeadLetterRecord[]>;
  requeue(messageId: string, stream: string, group: string): boolean | Promise<boolean>;
}
