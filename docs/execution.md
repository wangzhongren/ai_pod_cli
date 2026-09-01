# Async, parallel, and streaming Pipelines

AIPod keeps execution policy in its deterministic Runtime. AI may declare a mode and its
policy, but it does not generate unmanaged task scheduling, shared-state locking, or
custom retry loops.

## Asynchronous execution

A Service may implement `async def execute(self, ctx)`. An async Pipeline awaits the same
component chain through `execute_all_async`:

```python
async def run(ctx):
    S = Pod(build_container(load_beans()))
    await (S(ReadRemote) | S(StoreResult)).execute_all_async(ctx)
    return ctx.summary()
```

External callers use:

```python
result = await PipelineRunner().run_async("refresh", {"account_id": "a-1"})
```

Legacy synchronous Services run in worker threads when reached from the async Runtime, so
they do not block the event loop. Async retry delays use `asyncio.sleep`.

## Parallel branches

`parallel` accepts two or more component references or sequential chains:

```python
flow = parallel(
    S(ReadInventory),
    S(ReadPrice) | S(ApplyDiscount),
    concurrency=2,
    failure_policy="fail_fast",
    merge="strict",
) | S(BuildResponse)

await flow.execute_all_async(ctx)
```

Each branch receives `ctx.fork()`. Branch writes are merged in declaration order only
after execution:

- `strict`: reject different values written to the same key;
- `overwrite`: the last declared branch wins;
- `collect`: conflicting values become a list;
- callable reducer: receives `(key, values)` and returns the merged value.

Failure policies are `fail_fast`, `collect_all`, and `ignore`. Branch traces are nested
under one bounded `parallel` step in the parent Context.

Use `analyze_parallel_contracts()` during static planning to detect duplicate branch
outputs before execution.

## Streaming

A stream source defines `stream(ctx)` or returns an iterable from `execute(ctx)`:

```python
class QueueSource:
    async def stream(self, ctx):
        async for message in self.consumer:
            yield {"message": message}
```

Compose a bounded stream:

```python
pipeline = (
    stream(S(QueueSource))
    .map(S(ParseMessage), concurrency=8, failure_policy="skip")
    .batch(100, output_key="messages")
    .through(S(StoreBatch), concurrency=2)
)

summary = await pipeline.execute_all_async(ctx)
```

Use `async for item in pipeline.iter_all(ctx)` when the Interface needs emitted values.
`StreamItem` contains a sequence number, immutable event envelope, and metadata. The
Runtime retains counters and the last sequence in Trace rather than retaining the entire
stream. Work is admitted in bounded groups, so slow consumers provide backpressure.

`failure_policy="stop"` terminates on a failed item; `"skip"` records the failure and
continues. Cancellation propagates through the async iterator.

## What remains application-specific

The Runtime governs local execution. Queue acknowledgements, durable checkpoints,
distributed partition ownership, transactions, and exactly-once claims depend on the
chosen infrastructure and must be implemented through explicit Providers and idempotent
Services.
