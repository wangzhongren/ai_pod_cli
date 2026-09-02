import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  Container,
  DistributedStreamPublisher,
  DistributedWorker,
  HttpStreamTransport,
  PersistentStreamBroker,
  PipelineRunner,
  failure,
  service,
  startStreamBroker,
} from "../src/index.js";

const pause = (milliseconds: number) => new Promise((accept) => setTimeout(accept, milliseconds));

test("persistent Broker supports dedupe, consumer groups, leases, and dead letters", async () => {
  const root = await mkdtemp(join(tmpdir(), "aipod-broker-"));
  const file = join(root, "broker.json");
  try {
    const broker = await PersistentStreamBroker.open(file);
    const first = await broker.publish("orders", { orderId: "o-1" }, { key: "o-1" });
    const duplicate = await broker.publish("orders", { orderId: "ignored" }, { key: "o-1" });
    assert.equal(duplicate.id, first.id);

    const groupA = await broker.claim({
      stream: "orders", group: "billing", consumer: "a", leaseMs: 100, maxAttempts: 2,
    });
    assert.ok(groupA);
    assert.equal(await broker.ack(groupA), true);
    const groupB = await broker.claim({
      stream: "orders", group: "analytics", consumer: "b", leaseMs: 100, maxAttempts: 2,
    });
    assert.equal(groupB?.id, first.id);

    await pause(120);
    const recovered = await broker.claim({
      stream: "orders", group: "analytics", consumer: "c", leaseMs: 100, maxAttempts: 2,
    });
    assert.equal(recovered?.attempt, 2);
    assert.equal(await broker.fail(recovered!, "bad event", 2), "dead");
    assert.equal(broker.stats().deadLetters, 1);

    const reopened = await PersistentStreamBroker.open(file);
    assert.equal(reopened.stats().streams.orders, 1);
    assert.equal(reopened.stats().deadLetters, 1);
    assert.equal(reopened.deadLetters({ group: "analytics" })[0]?.message.id, first.id);
    assert.equal(await reopened.requeue(first.id, "orders", "analytics"), true);
    const replayed = await reopened.claim({
      stream: "orders", group: "analytics", consumer: "fixed", leaseMs: 100,
      maxAttempts: 2,
    });
    assert.equal(replayed?.attempt, 1);
    assert.equal(reopened.stats().deadLetters, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("distributed Worker executes routes with retry and dead-letter semantics", async () => {
  const root = await mkdtemp(join(tmpdir(), "aipod-worker-"));
  try {
    const broker = await PersistentStreamBroker.open(join(root, "broker.json"));
    const processed: string[] = [];
    const container = new Container([{
      id: "Process", category: "service", factory: () => ({
        execute: (context: { get(key: string): unknown }) => {
          const id = String(context.get("id"));
          if (context.get("fail")) return failure(`cannot process ${id}`);
          processed.push(id);
          return { processed: true };
        },
      }),
    }]);
    const runner = new PipelineRunner([{
      name: "process", pipeline: service(container, "Process"),
    }]);
    const publisher = new DistributedStreamPublisher(broker, "jobs");
    await publisher.publish({ id: "a" }, { key: "a" });
    await publisher.publish({ id: "b" }, { key: "b" });
    await publisher.publish({ id: "bad", fail: true }, { key: "bad" });

    const controller = new AbortController();
    const worker = new DistributedWorker(broker, runner, {
      stream: "jobs", group: "processors", consumer: "worker",
      route: "process", concurrency: 2, leaseMs: 200, maxAttempts: 2, pollMs: 5,
    });
    const running = worker.run(controller.signal);
    for (let attempt = 0; attempt < 200; attempt += 1) {
      if (worker.metrics.acknowledged === 2 && worker.metrics.dead === 1) break;
      await pause(5);
    }
    controller.abort();
    const metrics = await running;

    assert.deepEqual(processed.sort(), ["a", "b"]);
    assert.equal(metrics.acknowledged, 2);
    assert.equal(metrics.retried, 1);
    assert.equal(metrics.dead, 1);
    assert.equal(broker.stats().deadLetters, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("HTTP Transport connects remote publishers and consumers", async (context) => {
  const root = await mkdtemp(join(tmpdir(), "aipod-http-broker-"));
  let server: Awaited<ReturnType<typeof startStreamBroker>>;
  try {
    server = await startStreamBroker({ file: join(root, "broker.json") });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EPERM") {
      context.skip("sandbox does not permit loopback listeners");
      await rm(root, { recursive: true, force: true });
      return;
    }
    throw error;
  }
  try {
    const transport = new HttpStreamTransport(server.url, server.token);
    const published = await transport.publish("events", { value: 42 });
    const claimed = await transport.claim({
      stream: "events", group: "g", consumer: "remote", leaseMs: 1_000, maxAttempts: 3,
    });
    assert.equal(claimed?.id, published.id);
    assert.equal(await transport.ack(claimed!), true);
    assert.equal((await transport.stats()).deliveries.acked, 1);
  } finally {
    await server.close();
    await rm(root, { recursive: true, force: true });
  }
});
