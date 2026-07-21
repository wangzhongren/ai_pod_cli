"""Dynamic Injector container builder — wires all beans as singletons at runtime."""

import importlib
import os
import sys

from injector import Injector, Module, singleton

from ai_pod_cli.context import PipelineContext


class DynamicAIContainerModule(Module):
    """Dynamically binds every bean in the config as a singleton."""

    def __init__(self, config: dict):
        super().__init__()
        self._config = config

    def configure(self, binder):
        # Ensure the cwd is on sys.path so dynamic imports from modules/ work
        cwd = os.getcwd()
        if cwd not in sys.path:
            sys.path.append(cwd)

        for bean in self._config["beans"]:
            module_path, class_name = bean["class_path"].rsplit(".", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            binder.bind(cls, to=cls, scope=singleton)


def build_container(config: dict) -> Injector:
    """Build and return a fully-wired Injector container from the bean config."""
    container = Injector([DynamicAIContainerModule(config)])
    return container


class _ComponentRef:
    """Wraps a DI-resolved component instance for pipe chaining."""

    __slots__ = ("_id", "_instance")

    def __init__(self, component_id: str, instance):
        self._id = component_id
        self._instance = instance

    def __or__(self, other):
        """Chain to the next component via |."""
        return _PipeChain([self]) | other

    def execute(self, ctx: PipelineContext) -> dict:
        return self._instance.execute(ctx)

    def execute_all(self, ctx: PipelineContext) -> dict:
        """Execute this single component and record the step (same API as _PipeChain)."""
        result = self.execute(ctx)
        ctx.record_step(self._id, result)
        return result


class _PipeChain:
    """A chain of components built via the | operator."""

    __slots__ = ("_refs",)

    def __init__(self, refs: list):
        self._refs = list(refs)

    def __or__(self, other):
        if isinstance(other, _ComponentRef):
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
            result = ref.execute(ctx)
            ctx.record_step(ref._id, result)
        return result


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
        return _ComponentRef(cls.__name__, instance)

    def get(self, cls):
        """Direct access (same as container.get) for non-pipe usage."""
        return self._container.get(cls)
