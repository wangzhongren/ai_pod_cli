"""Dynamic Injector container builder — wires all beans as singletons at runtime."""

import asyncio
import importlib
import inspect
import os
import sys
from dataclasses import dataclass, replace
from time import sleep
from time import perf_counter

from injector import Injector, Module, singleton

from ai_pod_cli.context import PipelineContext
from ai_pod_cli.contracts import materialize_contract_data, validate_contract_data
from ai_pod_cli.result import Failure, Success, normalize_result, serialize_result


class DynamicAIContainerModule(Module):
    """Dynamically binds every bean in the config as a singleton."""

    def __init__(self, config: dict):
        super().__init__()
        self._config = config

    def configure(self, binder):
        # Ensure the cwd is on sys.path so dynamic imports from modules/ work
        cwd = os.getcwd()
        if cwd in sys.path:
            sys.path.remove(cwd)
        sys.path.insert(0, cwd)

        for bean in self._config["beans"]:
            if bean.get("category") == "model":
                continue
            module_path, class_name = bean["class_path"].rsplit(".", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            binder.bind(cls, to=cls, scope=singleton)


def build_container(config: dict) -> Injector:
    """Build and return a fully-wired Injector container from the bean config."""
    container = Injector([DynamicAIContainerModule(config)])
    container._aipod_config = config
    return container


@dataclass(frozen=True)
class _ExecutionPolicy:
    retries: int = 0
    delay_seconds: float = 0.0
    fallback: object | None = None


class _ComponentRef:
    """Wraps a DI-resolved component instance for pipe chaining."""

    __slots__ = ("_id", "_instance", "_container", "_policy", "_inputs", "_outputs")

    def __init__(
        self, component_id: str, instance, container: Injector | None = None,
        policy=None, inputs=None, outputs=None,
    ):
        self._id = component_id
        self._instance = instance
        self._container = container
        self._policy = policy or _ExecutionPolicy()
        self._inputs = inputs or {}
        self._outputs = outputs or {}

    def __or__(self, other):
        """Chain to the next component via |."""
        return _PipeChain([self]) | other

    def execute(self, ctx: PipelineContext) -> dict:
        return self._instance.execute(ctx)

    async def execute_async(self, ctx: PipelineContext):
        """Execute either an async or legacy synchronous component."""
        execute = self._instance.execute
        if inspect.iscoroutinefunction(execute):
            return await execute(ctx)
        result = await asyncio.to_thread(execute, ctx)
        if inspect.isawaitable(result):
            return await result
        return result

    def retry(self, attempts: int = 3, delay_seconds: float = 0.0):
        """Return a ref that retries failures/exceptions before applying fallback."""
        if attempts < 1:
            raise ValueError("retry attempts must be at least 1")
        if delay_seconds < 0:
            raise ValueError("retry delay_seconds cannot be negative")
        return self._clone(retries=attempts, delay_seconds=delay_seconds)

    def fallback(self, component):
        """Return a ref that executes another component after terminal failure."""
        return self._clone(fallback=component)

    def with_policy(self, *, retry: int = 0, delay_seconds: float = 0.0, fallback=None):
        """Configure retry and fallback in one declaration."""
        if retry < 0 or delay_seconds < 0:
            raise ValueError("policy values cannot be negative")
        return self._clone(retries=retry, delay_seconds=delay_seconds, fallback=fallback)

    def _clone(self, **changes):
        return _ComponentRef(
            self._id,
            self._instance,
            self._container,
            replace(self._policy, **changes),
            self._inputs,
            self._outputs,
        )

    def _fallback_ref(self):
        fallback = self._policy.fallback
        if fallback is None:
            return None
        if isinstance(fallback, _ComponentRef):
            return fallback
        if isinstance(fallback, type) and self._container is not None:
            return _ComponentRef(fallback.__name__, self._container.get(fallback), self._container)
        raise TypeError("fallback must be a component class or S(Component) reference")

    def _execute_with_policy(self, ctx: PipelineContext):
        started = perf_counter()
        attempts = 0
        last_error = None
        raw_result = None
        normalized = None
        for attempt in range(self._policy.retries + 1):
            attempts = attempt + 1
            try:
                input_errors = validate_contract_data(
                    {**ctx.params, **ctx.data}, self._inputs, self._id,
                )
                if input_errors:
                    raise ValueError("inputs schema validation failed: " + "; ".join(input_errors))
                for key, value in materialize_contract_data(
                    {**ctx.params, **ctx.data}, self._inputs,
                ).items():
                    ctx.set(key, value)
                raw_result = self.execute(ctx)
                if inspect.isawaitable(raw_result):
                    if inspect.iscoroutine(raw_result):
                        raw_result.close()
                    raise RuntimeError(
                        f"component '{self._id}' is asynchronous; use execute_all_async(ctx)"
                    )
                normalized = normalize_result(raw_result)
                if isinstance(normalized, Success):
                    output_errors = validate_contract_data(
                        {**ctx.data, **normalized.output}, self._outputs, self._id,
                    )
                    if output_errors:
                        raise ValueError("outputs schema validation failed: " + "; ".join(output_errors))
                if isinstance(normalized, Success):
                    break
                last_error = normalized.error
            except Exception as error:
                last_error = f"{type(error).__name__}: {error}"
                if attempt >= self._policy.retries:
                    fallback_ref = self._fallback_ref()
                    if fallback_ref is None:
                        raise
                normalized = Failure(str(error), code=type(error).__name__, retryable=True)
            if attempt < self._policy.retries and self._policy.delay_seconds:
                sleep(self._policy.delay_seconds)

        fallback_ref = self._fallback_ref() if isinstance(normalized, Failure) else None
        fallback_id = None
        if fallback_ref is not None:
            fallback_id = fallback_ref._id
            raw_result, normalized = fallback_ref._execute_with_policy(ctx)[:2]

        duration_ms = (perf_counter() - started) * 1000
        return raw_result, normalized, {
            "status": "success" if isinstance(normalized, Success) else "failure",
            "attempts": attempts,
            **({"last_error": last_error} if last_error else {}),
            **({"fallback": fallback_id} if fallback_id else {}),
            "duration_ms": duration_ms,
        }

    async def _execute_with_policy_async(self, ctx: PipelineContext):
        started = perf_counter()
        attempts = 0
        last_error = None
        raw_result = None
        normalized = None
        for attempt in range(self._policy.retries + 1):
            attempts = attempt + 1
            try:
                input_errors = validate_contract_data(
                    {**ctx.params, **ctx.data}, self._inputs, self._id,
                )
                if input_errors:
                    raise ValueError("inputs schema validation failed: " + "; ".join(input_errors))
                for key, value in materialize_contract_data(
                    {**ctx.params, **ctx.data}, self._inputs,
                ).items():
                    ctx.set(key, value)
                raw_result = await self.execute_async(ctx)
                normalized = normalize_result(raw_result)
                if isinstance(normalized, Success):
                    output_errors = validate_contract_data(
                        {**ctx.data, **normalized.output}, self._outputs, self._id,
                    )
                    if output_errors:
                        raise ValueError("outputs schema validation failed: " + "; ".join(output_errors))
                    break
                last_error = normalized.error
            except Exception as error:
                last_error = f"{type(error).__name__}: {error}"
                if attempt >= self._policy.retries:
                    fallback_ref = self._fallback_ref()
                    if fallback_ref is None:
                        raise
                normalized = Failure(str(error), code=type(error).__name__, retryable=True)
            if attempt < self._policy.retries and self._policy.delay_seconds:
                await asyncio.sleep(self._policy.delay_seconds)

        fallback_ref = self._fallback_ref() if isinstance(normalized, Failure) else None
        fallback_id = None
        if fallback_ref is not None:
            fallback_id = fallback_ref._id
            raw_result, normalized = (await fallback_ref._execute_with_policy_async(ctx))[:2]

        duration_ms = (perf_counter() - started) * 1000
        return raw_result, normalized, {
            "status": "success" if isinstance(normalized, Success) else "failure",
            "attempts": attempts,
            **({"last_error": last_error} if last_error else {}),
            **({"fallback": fallback_id} if fallback_id else {}),
            "duration_ms": duration_ms,
        }

    def execute_all(self, ctx: PipelineContext) -> dict:
        """Execute this single component and record the step (same API as _PipeChain)."""
        result, normalized, metadata = self._execute_with_policy(ctx)
        if isinstance(normalized, Success):
            for key, value in normalized.output.items():
                ctx.set(key, value)
        ctx.record_step(
            self._id,
            serialize_result(result),
            metadata.pop("duration_ms"),
            **metadata,
        )
        return result

    async def execute_all_async(self, ctx: PipelineContext):
        """Asynchronously execute this component and record its trace step."""
        result, normalized, metadata = await self._execute_with_policy_async(ctx)
        if isinstance(normalized, Success):
            for key, value in normalized.output.items():
                ctx.set(key, value)
        ctx.record_step(
            self._id,
            serialize_result(result),
            metadata.pop("duration_ms"),
            **metadata,
        )
        return result


class _PipeChain:
    """A chain of components built via the | operator."""

    __slots__ = ("_refs",)

    def __init__(self, refs: list):
        self._refs = list(refs)

    def __or__(self, other):
        if isinstance(other, (_ComponentRef, _ParallelGroup)):
            self._refs.append(other)
        elif isinstance(other, _PipeChain):
            self._refs.extend(other._refs)
        else:
            return NotImplemented
        return self

    def execute_all(self, ctx: PipelineContext) -> dict:
        """Execute all components in order, recording each step."""
        result = None
        for ref in self._refs:
            if isinstance(ref, _ParallelGroup):
                result = ref.execute_all(ctx)
                normalized = normalize_result(result)
            else:
                result, normalized, metadata = ref._execute_with_policy(ctx)
                if isinstance(normalized, Success):
                    for key, value in normalized.output.items():
                        ctx.set(key, value)
                ctx.record_step(
                    ref._id,
                    serialize_result(result),
                    metadata.pop("duration_ms"),
                    **metadata,
                )
            if isinstance(normalized, Failure):
                break
        return result

    async def execute_all_async(self, ctx: PipelineContext):
        """Execute sequential nodes asynchronously, preserving ``|`` semantics."""
        result = None
        for ref in self._refs:
            if isinstance(ref, _ParallelGroup):
                result = await ref.execute_all_async(ctx)
            else:
                result = await ref.execute_all_async(ctx)
            if isinstance(normalize_result(result), Failure):
                break
        return result


class _ParallelGroup:
    """Explicit set of isolated branches executed concurrently."""

    __slots__ = ("_branches", "_merge", "_failure_policy", "_concurrency")

    def __init__(
        self, branches, *, merge="strict", failure_policy="fail_fast",
        concurrency: int | None = None,
    ):
        if len(branches) < 2:
            raise ValueError("parallel() requires at least two branches")
        if failure_policy not in {"fail_fast", "collect_all", "ignore"}:
            raise ValueError("failure_policy must be fail_fast, collect_all, or ignore")
        if concurrency is not None and concurrency < 1:
            raise ValueError("parallel concurrency must be at least 1")
        if not all(isinstance(item, (_ComponentRef, _PipeChain)) for item in branches):
            raise TypeError("parallel branches must be S(Component) refs or pipe chains")
        self._branches = tuple(branches)
        self._merge = merge
        self._failure_policy = failure_policy
        self._concurrency = concurrency or len(branches)

    def __or__(self, other):
        return _PipeChain([self]) | other

    async def _run_branch(self, index: int, branch, parent: PipelineContext, semaphore):
        branch_ctx = parent.fork(f"branch-{index + 1}")
        try:
            async with semaphore:
                result = await branch.execute_all_async(branch_ctx)
            return index, branch_ctx, result, normalize_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = Failure(str(error), code=type(error).__name__)
            return index, branch_ctx, failure, failure

    async def execute_all_async(self, ctx: PipelineContext):
        started = perf_counter()
        semaphore = asyncio.Semaphore(self._concurrency)
        tasks = [
            asyncio.create_task(self._run_branch(index, branch, ctx, semaphore))
            for index, branch in enumerate(self._branches)
        ]
        completed = []
        if self._failure_policy == "fail_fast":
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                batch = [task.result() for task in done]
                completed.extend(batch)
                if any(isinstance(item[3], Failure) for item in batch):
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    break
        else:
            completed = list(await asyncio.gather(*tasks))

        completed.sort(key=lambda item: item[0])
        failures = [item for item in completed if isinstance(item[3], Failure)]
        successful = [item for item in completed if isinstance(item[3], Success)]
        merged = {}
        merge_error = None
        try:
            merged = ctx.merge([item[1] for item in successful], strategy=self._merge)
        except Exception as error:
            merge_error = Failure(str(error), code="parallel_merge_conflict")

        if merge_error is not None:
            result = merge_error
        elif failures and self._failure_policy != "ignore":
            result = Failure(
                f"{len(failures)} parallel branch(es) failed",
                code="parallel_branch_failure",
                details={
                    "branches": [
                        {"index": item[0], "error": item[3].to_dict()["error"]}
                        for item in failures
                    ],
                },
            )
        else:
            result = Success(merged)

        branch_traces = [
            {
                "id": item[1].branch_id,
                "status": "failure" if isinstance(item[3], Failure) else "success",
                "steps": item[1].summary()["steps"],
            }
            for item in completed
        ]
        ctx.record_step(
            "parallel", result.to_dict(), (perf_counter() - started) * 1000,
            status="failure" if isinstance(result, Failure) else "success",
            mode="parallel", branch_count=len(self._branches),
            failure_policy=self._failure_policy,
            merge=self._merge if isinstance(self._merge, str) else "custom",
            branches=branch_traces,
        )
        return result

    def execute_all(self, ctx: PipelineContext):
        """Run parallel branches from synchronous pipeline code."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.execute_all_async(ctx))
        raise RuntimeError(
            "execute_all() cannot start parallel work inside an active event loop; "
            "use 'await execute_all_async(ctx)'"
        )


def parallel(*branches, merge="strict", failure_policy="fail_fast", concurrency=None):
    """Declare branches that may run concurrently with deterministic merging."""
    return _ParallelGroup(
        branches, merge=merge, failure_policy=failure_policy, concurrency=concurrency,
    )


class Pod:
    """Pipeline-friendly container wrapper. Use `S(Class)` to get pipe-able components.

    Usage in generated pipelines:

        beans = load_beans()
        container = build_container(beans)
        S = Pod(container)

        # Pipe syntax: execute StockChecker, then StockNotifier in sequence
        (S(StockChecker) | S(StockNotifier)).execute_all(ctx)
    """

    def __init__(self, container: Injector):
        self._container = container

    def __call__(self, cls) -> _ComponentRef:
        """Resolve a component from the container and wrap it for pipe chaining."""
        instance = self._container.get(cls)
        config = getattr(self._container, "_aipod_config", {})
        bean = next(
            (item for item in config.get("beans", []) if item.get("id") == cls.__name__),
            {},
        )
        return _ComponentRef(
            cls.__name__, instance, self._container,
            inputs=bean.get("inputs"), outputs=bean.get("outputs"),
        )

    def get(self, cls):
        """Direct access (same as container.get) for non-pipe usage."""
        return self._container.get(cls)
