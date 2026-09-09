"""Explicit fixtures and governed component calls for the owned test worker.

The SDK is available only while ai_pod_cli.component_tests runs a project test.
It never loads the project's original configuration, environment, or database.
This isolates test data and normal framework access; it is not an OS security
sandbox for hostile Python code or hard-coded external resource access.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import tempfile
from copy import deepcopy
from pathlib import Path

from ai_pod_cli.config_store import ConfigStore
from ai_pod_cli.container import _ComponentRef, build_container
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.model import Model
from ai_pod_cli.repository import ModelRepository
from ai_pod_cli.result import Failure, normalize_result


class TestSetupError(ValueError):
    """The test's fixture, SDK invocation, or dependency override is invalid."""


SDK_METHODS = {"seed", "run", "call_provider", "model", "rows", "count", "snapshot", "path"}
SDK_PROMPT = """Write ordinary Python unittest tests in class ComponentTests(unittest.TestCase).
Use: from ai_pod_cli.testing import Sandbox
Each test uses `with Sandbox(config={}, provider_overrides={}) as s:`.
The SDK starts with an EMPTY temporary database. No users, roles, business rows,
or input defaults are invented. config contains only explicit test values; the
database URL is always replaced by a unique temporary SQLite database.
Public API (use exact registered Bean IDs, never class paths):
- s.seed(model_id: str, data: dict) -> validated table Model saved in the temporary database.
- s.model(model_id: str, data: dict) -> actual Model.model_validate(data), without saving.
- s.run(service_id: str, params: dict | None = None) -> dict of actual produced outputs.
  These are the governed runtime outputs: a Model field may already be serialized
  as a dict by the component. Do not assume attribute access such as result['row'].title;
  normalize a Model with model_dump when needed, then assert its field values.
- s.call_provider(provider_id: str, method: str, **params) -> the actual public method result.
- s.rows(model_id: str) -> list[dict]; s.count(model_id: str) -> int.
- s.snapshot() -> all temporary SQLModel table rows, suitable for before/after assertions.
- s.path(relative_path: str) -> Path inside this Sandbox's temporary resource directory.
provider_overrides maps a declared dependency Provider ID to a fake INSTANCE.
It cannot replace the component under test, Services, Models, ConfigStore, or
ModelRepository. Put fake dependency classes in the test file if needed.
Do not import modules.models/providers/services or the target implementation;
all component access uses SDK IDs. There is no s.get, s.repo, s.container, s.ctx,
s.execute, s.call, s.invoke, or s.provider API. Do not guess extra methods.
Use meaningful self.assert* assertions on real returned values or database state.
Use self.assertRaises for required error paths; an expected exception is valid
execution evidence. For a required no-write failure, compare s.snapshot() before
and after. Each declared test must actually invoke the target and assert behavior;
pass, skips, constant assertions, or testing only a dependency cannot substitute.
Do not change candidate code, the registry, or the test file to make a test pass.
External network connections, listeners, and child processes are unavailable in
component unit tests. Replace an external dependency Provider with an explicit fake.
"""

_CONTEXT = None


def _class_for(bean: dict):
    module, name = bean["class_path"].rsplit(".", 1)
    return getattr(importlib.import_module(module), name)


def _callable_identity(value):
    return getattr(value, "__func__", value)


def _configure_context(root: Path, bean: dict, recorder):
    global _CONTEXT
    registry = json.loads((root / "beans_config.json").read_text(encoding="utf-8"))
    target_class = _class_for(bean)
    original_methods = {name: _callable_identity(getattr(target_class, name))
                        for name in dir(target_class) if not name.startswith("_") and callable(getattr(target_class, name))}
    _CONTEXT = {"root": root, "registry": registry, "target": bean,
                "target_class": target_class, "methods": original_methods,
                "recorder": recorder, "sandboxes": []}


def _clear_context():
    global _CONTEXT
    if _CONTEXT is not None:
        for sandbox in reversed(_CONTEXT["sandboxes"]):
            sandbox.__exit__(None, None, None)
    _CONTEXT = None


class _TestConfig(ConfigStore):
    def __init__(self, values):
        self._initial = deepcopy(values)
        self._data = deepcopy(values)
        self._config_path = "<explicit-test-configuration>"

    def reload(self):
        self._data = deepcopy(self._initial)


class Sandbox:
    def __init__(self, config: dict | None = None, provider_overrides: dict | None = None):
        if _CONTEXT is None:
            raise TestSetupError("Sandbox is available only inside the framework component test worker")
        if config is not None and not isinstance(config, dict) or provider_overrides is not None and not isinstance(provider_overrides, dict):
            raise TestSetupError("Sandbox config and provider_overrides must be dictionaries")
        self._context = _CONTEXT
        self._configuration = deepcopy(config or {})
        self._overrides = dict(provider_overrides or {})
        self._temporary = None
        self._repository = None
        self._container = None

    def __enter__(self):
        if self._temporary is not None:
            raise TestSetupError("A Sandbox cannot be entered twice")
        self._temporary = tempfile.TemporaryDirectory(prefix="aipod-test-data-")
        self._root = Path(self._temporary.name).resolve()
        self._context["sandboxes"].append(self)
        self._beans = {item["id"]: item for item in self._context["registry"].get("beans", [])}
        target = self._context["target"]
        allowed = set()
        pending = list(target.get("dependencies") or [])
        while pending:
            identifier = pending.pop()
            if identifier in allowed:
                continue
            bean = self._beans.get(identifier)
            if bean is not None and bean.get("category") == "provider":
                allowed.add(identifier)
                pending.extend(bean.get("dependencies") or [])
        for identifier in self._overrides:
            if identifier not in allowed or identifier == target["id"] or identifier in {"ConfigStore", "ModelRepository"}:
                self.__exit__(None, None, None)
                raise TestSetupError("provider_overrides may replace only declared dependency Providers, excluding framework configuration/database providers")
        values = deepcopy(self._configuration)
        database = values.setdefault("database", {})
        if not isinstance(database, dict):
            self.__exit__(None, None, None)
            raise TestSetupError("Sandbox config.database must be a dictionary")
        database["url"] = "sqlite:///" + (self._root / "database.sqlite3").as_posix()
        self._config = _TestConfig(values)
        self._container = build_container(self._context["registry"])
        self._container.binder.bind(ConfigStore, to=self._config)
        self._repository = ModelRepository(self._config)
        self._container.binder.bind(ModelRepository, to=self._repository)
        for identifier, instance in self._overrides.items():
            if isinstance(instance, type):
                self.__exit__(None, None, None)
                raise TestSetupError("provider_overrides values must be fake instances, not classes")
            self._container.binder.bind(_class_for(self._beans[identifier]), to=instance)
        return self

    def __exit__(self, _kind, _error, _traceback):
        if self._repository is not None:
            self._repository.close()
            self._repository = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self._container = None

    def _bean(self, identifier, category):
        if self._container is None:
            raise TestSetupError("Use Sandbox inside a with block")
        bean = self._beans.get(identifier)
        if bean is None or bean.get("category") != category:
            raise TestSetupError(f"Unknown {category} Bean ID: {identifier}")
        return bean

    def _dispatch(self, identifier, method, function):
        if identifier == self._context["target"]["id"]:
            if _class_for(self._beans[identifier]) is not self._context["target_class"]:
                raise TestSetupError("The registered target class cannot be replaced by a test")
            current = _callable_identity(getattr(self._context["target_class"], method, None))
            if current is not self._context["methods"].get(method):
                raise TestSetupError("The target implementation cannot be replaced by a test")
        return self._context["recorder"].dispatch(identifier, method, function)

    def _check_target_instance(self, identifier, instance, method):
        if identifier != self._context["target"]["id"]:
            return
        if type(instance) is not self._context["target_class"]:
            raise TestSetupError("The actual target instance cannot be replaced by a test")
        actual = _callable_identity(getattr(instance, method))
        if actual is not self._context["methods"].get(method):
            raise TestSetupError("The actual target method cannot be replaced by a test")

    def model(self, model_id: str, data: dict):
        bean = self._bean(model_id, "model")
        if not isinstance(data, dict):
            raise TestSetupError("Model data must be a dictionary")
        cls = _class_for(bean)
        if not issubclass(cls, Model):
            raise TestSetupError("Registered model must inherit AIPod Model")
        with self._dispatch(model_id, "model_validate", cls.model_validate):
            return cls.model_validate(deepcopy(data))

    def seed(self, model_id: str, data: dict):
        bean = self._bean(model_id, "model")
        cls = _class_for(bean)
        if getattr(cls, "__table__", None) is None:
            raise TestSetupError("seed requires a table Model; use model for value Models")
        try:
            instance = self.model(model_id, data)
            return self._repository.save(instance)
        except Exception as error:
            raise TestSetupError(f"Fixture for {model_id} is invalid ({type(error).__name__})") from error

    def run(self, service_id: str, params: dict | None = None) -> dict:
        bean = self._bean(service_id, "service")
        if params is not None and not isinstance(params, dict):
            raise TestSetupError("Service params must be a dictionary")
        cls = _class_for(bean)
        instance = self._container.get(cls)
        self._check_target_instance(service_id, instance, "execute")
        ref = _ComponentRef(bean["id"], instance, self._container,
                            inputs=bean.get("inputs"), outputs=bean.get("outputs"))
        ctx = PipelineContext(deepcopy(params or {}))
        asynchronous = inspect.iscoroutinefunction(instance.execute)
        boundary = ref._execute_with_policy_async if asynchronous else ref._execute_with_policy
        with self._dispatch(service_id, "execute", boundary):
            returned = asyncio.run(ref.execute_all_async(ctx)) if asynchronous else ref.execute_all(ctx)
        normalized = normalize_result(returned)
        if isinstance(normalized, Failure):
            raise RuntimeError(f"Service returned Failure: {normalized.error}")
        if bean.get("outputs"):
            return {name: ctx.data[name] for name in bean["outputs"] if name in ctx.data}
        return dict(normalized.output)

    def call_provider(self, provider_id: str, method: str, **params):
        bean = self._bean(provider_id, "provider")
        if not isinstance(method, str) or method.startswith("_"):
            raise TestSetupError("Provider method must name a public API")
        if method not in (bean.get("methods") or {}):
            raise TestSetupError("Provider method is not declared in its registered API")
        cls = _class_for(bean)
        instance = self._container.get(cls)
        self._check_target_instance(provider_id, instance, method)
        function = getattr(instance, method)
        if not callable(function):
            raise TestSetupError("Provider API is not callable")
        with self._dispatch(provider_id, method, function):
            result = function(**params)
            return asyncio.run(result) if inspect.isawaitable(result) else result

    def rows(self, model_id: str) -> list[dict]:
        bean = self._bean(model_id, "model")
        cls = _class_for(bean)
        if getattr(cls, "__table__", None) is None:
            raise TestSetupError("rows requires a table Model")
        return [row.model_dump(mode="json") for row in self._repository.list(cls)]

    def count(self, model_id: str) -> int:
        return len(self.rows(model_id))

    def snapshot(self) -> dict:
        if self._container is None:
            raise TestSetupError("Use Sandbox inside a with block")
        from sqlmodel import SQLModel, select
        self._repository.init_db()
        result = {}
        with self._repository.engine.connect() as connection:
            for table in SQLModel.metadata.sorted_tables:
                rows = [dict(row._mapping) for row in connection.execute(select(table))]
                result[table.name] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))
        return result

    def path(self, relative_path: str) -> Path:
        if self._container is None:
            raise TestSetupError("Use Sandbox inside a with block")
        if not isinstance(relative_path, str) or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
            raise TestSetupError("Sandbox resource path must be a relative path without traversal")
        target = (self._root / relative_path).resolve()
        if not target.is_relative_to(self._root):
            raise TestSetupError("Sandbox resource path escapes temporary storage")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


__all__ = ["Sandbox", "TestSetupError", "SDK_PROMPT"]
