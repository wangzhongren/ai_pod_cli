"""Bounded asynchronous streaming pipelines for AIPod components."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from time import perf_counter
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping

from ai_pod_cli.context import PipelineContext
from ai_pod_cli.contracts import validate_contract_data
from ai_pod_cli.result import Failure, Success, normalize_result


@dataclass(frozen=True)
class StreamItem:
    """One immutable event moving through a streaming pipeline."""

    sequence: int
    data: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class _MapStage:
    component: object
    concurrency: int
    failure_policy: str


@dataclass(frozen=True)
class _BatchStage:
    size: int
    output_key: str


class StreamPipeline:
    """A lazy stream with bounded concurrency and backpressure by consumption."""

    def __init__(self, source, *, item_key: str = "item", stages=()):
        if not hasattr(source, "_instance"):
            raise TypeError("stream source must be an S(Component) reference")
        self._source = source
        self._item_key = item_key
        self._stages = tuple(stages)

    def map(self, component, *, concurrency: int = 1, failure_policy: str = "stop"):
        """Transform items with a component while bounding in-flight work."""
        if not hasattr(component, "execute_all_async"):
            raise TypeError("stream map component must be an S(Component) reference")
        if concurrency < 1:
            raise ValueError("stream concurrency must be at least 1")
        if failure_policy not in {"stop", "skip"}:
            raise ValueError("stream failure_policy must be stop or skip")
        return StreamPipeline(
            self._source, item_key=self._item_key,
            stages=(*self._stages, _MapStage(component, concurrency, failure_policy)),
        )

    def through(self, component, *, concurrency: int = 1, failure_policy: str = "stop"):
        """Alias for ``map`` that reads naturally for sink-like components."""
        return self.map(
            component, concurrency=concurrency, failure_policy=failure_policy,
        )

    def batch(self, size: int, *, output_key: str = "batch"):
        """Group up to ``size`` events without removing backpressure."""
        if size < 1:
            raise ValueError("stream batch size must be at least 1")
        return StreamPipeline(
            self._source, item_key=self._item_key,
            stages=(*self._stages, _BatchStage(size, output_key)),
        )

    async def _source_items(self, ctx: PipelineContext) -> AsyncIterator[StreamItem]:
        instance = self._source._instance
        source_fn = getattr(instance, "stream", None) or instance.execute
        source_ctx = ctx.fork("stream-source")
        if inspect.iscoroutinefunction(source_fn) or inspect.isasyncgenfunction(source_fn):
            produced = source_fn(source_ctx)
        else:
            produced = await asyncio.to_thread(source_fn, source_ctx)
        if inspect.isawaitable(produced):
            produced = await produced

        if hasattr(produced, "__aiter__"):
            sequence = 0
            async for raw in produced:
                yield self._normalize_item(sequence, raw)
                sequence += 1
            return
        if hasattr(produced, "__iter__") and not isinstance(produced, (str, bytes, dict)):
            iterator = iter(produced)
            sentinel = object()
            sequence = 0
            while True:
                raw = await asyncio.to_thread(next, iterator, sentinel)
                if raw is sentinel:
                    break
                yield self._normalize_item(sequence, raw)
                sequence += 1
            return
        raise TypeError(
            "stream source must define stream(ctx) or execute(ctx) returning an iterable"
        )

    def _normalize_item(self, sequence: int, raw) -> StreamItem:
        if isinstance(raw, StreamItem):
            return raw
        if isinstance(raw, Failure):
            raise RuntimeError(f"stream source failed: {raw.error}")
        if isinstance(raw, Success):
            data = raw.output
        elif isinstance(raw, dict):
            data = raw
        else:
            data = {self._item_key: raw}
        errors = validate_contract_data(data, self._source._outputs, self._source._id)
        if errors:
            raise ValueError("stream item schema validation failed: " + "; ".join(errors))
        return StreamItem(sequence, dict(data))

    async def _map_one(self, item: StreamItem, stage: _MapStage):
        branch = PipelineContext(data=dict(item.data), branch_id=f"stream-{item.sequence}")
        result = await stage.component.execute_all_async(branch)
        normalized = normalize_result(result)
        if isinstance(normalized, Failure):
            return None, normalized
        data = dict(item.data)
        data.update(branch.data)
        data.update(normalized.output)
        return StreamItem(item.sequence, data, dict(item.metadata)), None

    async def _map_items(self, incoming, stage: _MapStage, counters) -> AsyncIterator[StreamItem]:
        pending = []
        async for item in incoming:
            pending.append(self._map_one(item, stage))
            if len(pending) < stage.concurrency:
                continue
            results = await _gather_ordered(pending)
            pending = []
            for mapped, failure in results:
                if failure is not None:
                    counters["failed"] += 1
                    if stage.failure_policy == "stop":
                        raise RuntimeError(f"stream component failed: {failure.error}")
                elif mapped is not None:
                    yield mapped
        for mapped, failure in await _gather_ordered(pending):
            if failure is not None:
                counters["failed"] += 1
                if stage.failure_policy == "stop":
                    raise RuntimeError(f"stream component failed: {failure.error}")
            elif mapped is not None:
                yield mapped

    async def _batch_items(self, incoming, stage: _BatchStage) -> AsyncIterator[StreamItem]:
        items = []
        async for item in incoming:
            items.append(item)
            if len(items) == stage.size:
                yield self._make_batch(items, stage.output_key)
                items = []
        if items:
            yield self._make_batch(items, stage.output_key)

    @staticmethod
    def _make_batch(items: list[StreamItem], output_key: str) -> StreamItem:
        return StreamItem(
            items[0].sequence,
            {output_key: [dict(item.data) for item in items]},
            {"batch_size": len(items), "last_sequence": items[-1].sequence},
        )

    async def iter_all(self, ctx: PipelineContext) -> AsyncIterator[StreamItem]:
        """Yield processed events lazily and record a bounded stream trace."""
        started = perf_counter()
        counters = {"emitted": 0, "failed": 0, "last_sequence": None}
        incoming = self._source_items(ctx)
        for stage in self._stages:
            if isinstance(stage, _MapStage):
                incoming = self._map_items(incoming, stage, counters)
            else:
                incoming = self._batch_items(incoming, stage)
        status = "success"
        error = None
        try:
            async for item in incoming:
                counters["emitted"] += 1
                counters["last_sequence"] = item.sequence
                yield item
        except Exception as caught:
            status = "failure"
            error = caught
            raise
        finally:
            result = {
                "status": status,
                "emitted": counters["emitted"],
                "failed": counters["failed"],
                "last_sequence": counters["last_sequence"],
                **({"error": str(error)} if error else {}),
            }
            ctx.record_step(
                "stream", result, (perf_counter() - started) * 1000,
                status=status, mode="stream", stream_count=counters["emitted"],
                failed_count=counters["failed"],
                last_sequence=counters["last_sequence"],
            )

    async def execute_all_async(self, ctx: PipelineContext):
        """Consume the stream without retaining its events and return statistics."""
        async for _item in self.iter_all(ctx):
            pass
        step = ctx.steps[-1]
        summary = {
            "stream_count": step["stream_count"],
            "failed_count": step["failed_count"],
            "last_sequence": step["last_sequence"],
        }
        ctx.data.update(summary)
        return Success(summary)


async def _gather_ordered(awaitables):
    if not awaitables:
        return []
    return await asyncio.gather(*awaitables)


def stream(source, *, item_key: str = "item") -> StreamPipeline:
    """Start a bounded asynchronous stream from an S(Component) reference."""
    return StreamPipeline(source, item_key=item_key)


__all__ = ["StreamItem", "StreamPipeline", "stream"]
