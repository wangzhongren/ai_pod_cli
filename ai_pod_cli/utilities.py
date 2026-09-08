"""Project-owned pure utility classes, verified examples, and explicit registration.

Utilities are ordinary Python imports, not DI beans or another runtime layer.
Static checks and example execution constrain supported code; they are not a
formal proof of arbitrary Python purity or of every possible caller behavior.
"""

from __future__ import annotations

import ast
import hashlib
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any


REGISTRY_FILE = "utility_registry.json"
_LOCK_FILE = ".utility_registry.lock"
_PURE_MODULES = {"math", "cmath", "statistics", "re", "decimal", "fractions",
                 "collections", "itertools", "functools", "operator", "typing",
                 "string", "bisect", "heapq", "unicodedata", "json"}
_BLOCKED_NAMES = {"open", "input", "print", "eval", "exec", "compile", "__import__",
                  "getattr", "setattr", "delattr", "globals", "locals", "vars",
                  "breakpoint", "help", "exit", "quit", "PipelineContext", "Pod",
                  "PipelineRunner", "ConfigStore", "ModelRepository", "call_llm"}
_BLOCKED_ATTRIBUTES = {"open", "read", "write", "read_text", "write_text", "read_bytes",
                       "write_bytes", "load", "dump", "connect", "send", "recv", "request",
                       "urlopen", "system", "popen", "spawn", "fork", "execute", "execute_all",
                       "execute_async", "execute_all_async", "run_route", "record_step",
                       "load_beans", "build_container", "call_llm", "params", "attrgetter",
                       "methodcaller"}
_SKIP_DIRS = {".git", ".aipod", ".venv", "venv", "node_modules", "__pycache__"}


def _identity(identifier: str) -> str:
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Z][A-Za-z0-9_]*", identifier) or keyword.iskeyword(identifier):
        raise ValueError("Utility id must be a public Python class name beginning with an uppercase letter")
    return f"modules/utils/{identifier.lower()}.py"


def _path(root: Path, relative: str) -> Path:
    candidate = root
    parts = Path(relative).parts
    if Path(relative).is_absolute() or not parts or any(part in {"..", "."} for part in parts) or "\\" in relative:
        raise ValueError("Utility paths must be exact project-relative paths")
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("Utility paths cannot traverse symlinks")
    if not candidate.resolve().is_relative_to(root):
        raise ValueError("Utility path escapes its project")
    return candidate


@contextmanager
def _lock(root: Path, *, writing=False):
    if writing:
        root.mkdir(parents=True, exist_ok=True)
    elif not root.exists():
        yield
        return
    target = _path(root, _LOCK_FILE)
    if not writing and (not target.exists() or os.name == "nt"):
        # Readers never create coordination files. With no existing lock, each
        # source hash still has to match the registry snapshot that was read.
        yield
        return
    with target.open("a+b" if writing else "rb") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX if writing else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _registry(root: Path) -> dict:
    file = _path(root, REGISTRY_FILE)
    if not file.exists():
        return {"schema_version": 1, "utilities": []}
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Utility registry is unreadable") from error
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1 or not isinstance(value.get("utilities"), list):
        raise ValueError("Utility registry requires schema_version=1 and a utilities array")
    ids, paths, symbols = set(), set(), set()
    for entry in value["utilities"]:
        if not isinstance(entry, dict):
            raise ValueError("Utility registry contains an invalid entry")
        identifier = entry.get("id")
        expected = _identity(identifier)
        if entry.get("language") != "python" or entry.get("path") != expected or entry.get("symbol") != identifier:
            raise ValueError("Utility registry entry has an invalid language, symbol, or path")
        if identifier in ids or expected.casefold() in paths or identifier in symbols:
            raise ValueError("Utility registry contains a duplicate id, symbol, or path")
        ids.add(identifier)
        paths.add(expected.casefold())
        symbols.add(identifier)
        _path(root, expected)
    return value


def _annotation(value) -> str:
    return ast.unparse(value) if value is not None else ""


def _method(node: ast.FunctionDef, identifier: str) -> dict:
    parameters = []
    positional = [*node.args.posonlyargs, *node.args.args]
    defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
    for index, (argument, default) in enumerate(zip(positional, defaults)):
        parameters.append({"name": argument.arg,
                           "kind": "positional_only" if index < len(node.args.posonlyargs) else "positional_or_keyword",
                           "annotation": _annotation(argument.annotation), "default": _annotation(default) if default is not None else None})
    if node.args.vararg:
        parameters.append({"name": node.args.vararg.arg, "kind": "var_positional", "annotation": _annotation(node.args.vararg.annotation), "default": None})
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        parameters.append({"name": argument.arg, "kind": "keyword_only", "annotation": _annotation(argument.annotation), "default": _annotation(default) if default is not None else None})
    if node.args.kwarg:
        parameters.append({"name": node.args.kwarg.arg, "kind": "var_keyword", "annotation": _annotation(node.args.kwarg.annotation), "default": None})
    returns = _annotation(node.returns)
    return {"name": node.name, "signature": f"{identifier}.{node.name}({ast.unparse(node.args)})" + (f" -> {returns}" if returns else ""),
            "parameters": parameters, "returns": returns, "doc": ast.get_docstring(node) or ""}


def _module_path(entry: dict) -> str:
    return entry["path"][:-3].replace("/", ".")


def _source_metadata(identifier: str, source: str, entries: list[dict]) -> tuple[list[dict], list[str]]:
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Utility source must be nonempty Python text")
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError(f"Utility Python syntax is invalid at line {error.lineno}") from error
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(classes) != 1 or classes[0].name != identifier:
        raise ValueError("Utility source must define exactly the registered class")
    cls = classes[0]
    if cls.decorator_list or cls.keywords or cls.bases:
        raise ValueError("Utility classes cannot use inheritance, metaclasses, or class decorators")
    allowed_statements = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.ClassDef)
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if not isinstance(node, allowed_statements):
            raise ValueError("Utility module scope permits only imports, constants, and its class")
    methods, method_names = [], set()
    for node in cls.body:
        if isinstance(node, ast.FunctionDef):
            if node.name in method_names or node.name.startswith("__"):
                raise ValueError("Utility methods must be unique and cannot be dunder methods")
            method_names.add(node.name)
            if len(node.decorator_list) != 1 or not isinstance(node.decorator_list[0], ast.Name) or node.decorator_list[0].id != "staticmethod":
                raise ValueError("Every utility method must be a plain @staticmethod")
            if not node.name.startswith("_"):
                methods.append(_method(node, identifier))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.Pass)):
            pass
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            pass
        else:
            raise ValueError("Utility classes may contain only static methods and constants; async utilities are unsupported")
    if not methods:
        raise ValueError("Utility class must expose at least one public static method")
    by_module = {_module_path(entry): entry for entry in entries}
    by_module.setdefault(f"modules.utils.{identifier.lower()}", {"id": identifier, "symbol": identifier})
    dependencies = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.Global, ast.Nonlocal)):
            raise ValueError("Utility methods must be synchronous and cannot modify global or nonlocal state")
        if isinstance(node, ast.Name) and node.id in _BLOCKED_NAMES:
            raise ValueError(f"Utility source cannot access {node.id}")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("__") or node.attr in _BLOCKED_ATTRIBUTES):
            raise ValueError(f"Utility source cannot access attribute {node.attr}")
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            raise ValueError("Utility methods cannot mutate object or class attributes")
        if isinstance(node, ast.ImportFrom):
            if node.level:
                raise ValueError("Use explicit modules.utils imports for registered utility dependencies")
            module = node.module or ""
            if module == "__future__" and all(item.name == "annotations" for item in node.names):
                continue
            if module in by_module:
                if any(item.name != by_module[module]["symbol"] for item in node.names):
                    raise ValueError("Import only the registered utility class symbol")
                dependencies.add(by_module[module]["id"])
            elif module.split(".")[0] not in _PURE_MODULES or any(item.name == "*" or item.name in _BLOCKED_NAMES | _BLOCKED_ATTRIBUTES for item in node.names):
                raise ValueError(f"Utility import is outside supported pure modules: {module}")
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name in by_module:
                    dependencies.add(by_module[item.name]["id"])
                elif item.name.split(".")[0] not in _PURE_MODULES:
                    raise ValueError(f"Utility import is outside supported pure modules: {item.name}")
    for scope in (tree.body, cls.body):
        for node in scope:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                if any(isinstance(child, (ast.Call, ast.NamedExpr, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) for child in ast.walk(node.value)):
                    raise ValueError("Utility module and class constants cannot execute calls")
    return methods, sorted(dependencies)


def _cases(cases: Any, methods: list[dict]) -> list[dict]:
    if not isinstance(cases, list) or not cases:
        raise ValueError("Utility registration requires nonempty executable method cases")
    names = {item["name"] for item in methods}
    covered = set()
    normalized = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or case.get("method") not in names:
            raise ValueError(f"Utility case {index + 1} must select a public static method")
        if ("expected" in case) == ("raises" in case):
            raise ValueError("Each utility case must specify exactly one of expected or raises")
        arguments, kwargs = case.get("args", []), case.get("kwargs", {})
        if not isinstance(arguments, list) or not isinstance(kwargs, dict) or not all(isinstance(name, str) for name in kwargs):
            raise ValueError("Utility case args must be an array and kwargs a string-key object")
        if "raises" in case and (not isinstance(case["raises"], str) or not re.fullmatch(r"[A-Za-z]+Error", case["raises"])):
            raise ValueError("Utility raises cases must name a builtin Error class")
        item = {"method": case["method"], "args": deepcopy(arguments), "kwargs": deepcopy(kwargs),
                **({"expected": deepcopy(case["expected"])} if "expected" in case else {"raises": case["raises"]})}
        try:
            json.dumps(item, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Utility cases must contain finite JSON values") from error
        normalized.append(item)
        covered.add(case["method"])
    if covered != names:
        raise ValueError("Utility cases must cover every public method: " + ", ".join(sorted(names - covered)))
    return normalized


def _no_cycles(entries: list[dict]) -> None:
    graph = {item["id"]: item.get("dependencies", []) for item in entries}
    visiting, done = set(), set()
    def visit(name):
        if name in visiting:
            raise ValueError("Registered utility dependencies contain a cycle")
        if name in done:
            return
        visiting.add(name)
        for dependency in graph[name]:
            if dependency not in graph:
                raise ValueError("Utility dependency is not registered")
            visit(dependency)
        visiting.remove(name)
        done.add(name)
    for name in graph:
        visit(name)


def _verified_entry(root: Path, entry: dict, entries: list[dict]) -> tuple[dict, str]:
    try:
        source_bytes = _path(root, entry["path"]).read_bytes()
        source = source_bytes.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"Utility source is missing or unreadable: {entry['id']}") from error
    if hashlib.sha256(source_bytes).hexdigest() != entry.get("sha256"):
        raise ValueError(f"Utility source hash drift detected: {entry['id']}")
    methods, dependencies = _source_metadata(entry["id"], source, entries)
    if methods != entry.get("methods") or dependencies != entry.get("dependencies", []):
        raise ValueError(f"Utility registry metadata does not match its source: {entry['id']}")
    _cases(entry.get("cases"), methods)
    return deepcopy(entry), source


def _callers(root: Path, entry: dict) -> list[dict]:
    module = _module_path(entry)
    callers = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in _SKIP_DIRS and not Path(folder, name).is_symlink()]
        for name in files:
            path = Path(folder, name)
            if path.suffix != ".py" or path.is_symlink() or path == root / entry["path"]:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeError):
                continue
            lines = [node.lineno for node in ast.walk(tree) if (
                isinstance(node, ast.ImportFrom) and not node.level and node.module == module
                or isinstance(node, ast.Import) and any(item.name == module for item in node.names)
            )]
            if lines:
                callers.append({"path": path.relative_to(root).as_posix(), "lines": sorted(set(lines))})
    return sorted(callers, key=lambda item: item["path"])


def list_utilities(project_root: str | Path = ".", query: str = "") -> list[dict]:
    root = Path(project_root).resolve()
    with _lock(root):
        entries = _registry(root)["utilities"]
        _no_cycles(entries)
        result = []
        for entry in entries:
            checked, _source = _verified_entry(root, entry, entries)
            if not query or query.casefold() in json.dumps(checked, ensure_ascii=False).casefold():
                result.append(checked)
        return result


def read_utility(id: str, project_root: str | Path = ".") -> dict:
    _identity(id)
    root = Path(project_root).resolve()
    with _lock(root):
        entries = _registry(root)["utilities"]
        _no_cycles(entries)
        entry = next((item for item in entries if item["id"] == id), None)
        if entry is None:
            raise ValueError(f"Utility not registered: {id}")
        checked, source = _verified_entry(root, entry, entries)
        return {**checked, "source": source, "callers": _callers(root, checked)}


_CASE_WORKER = r'''
import builtins, copy, importlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
payload = json.load(sys.stdin)
cls = getattr(importlib.import_module(payload['module']), payload['symbol'])
def equal(actual, expected):
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(equal(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(equal(a, b) for a, b in zip(actual, expected))
    return actual == expected
for index, case in enumerate(payload['cases'], 1):
    args, kwargs = case['args'], case['kwargs']
    before = copy.deepcopy((args, kwargs))
    expected_exception = None
    if 'raises' in case:
        expected_exception = getattr(builtins, case['raises'], None)
        if not isinstance(expected_exception, type) or not issubclass(expected_exception, Exception):
            raise ValueError('Expected exception must be a builtin Error class')
    try:
        actual = getattr(cls, case['method'])(*args, **kwargs)
    except Exception as error:
        if expected_exception is None or not isinstance(error, expected_exception):
            raise AssertionError('Case %d (%s) raised unexpected %s' % (index, case['method'], type(error).__name__)) from None
    else:
        if expected_exception is not None:
            raise AssertionError('Case %d (%s) did not raise the expected exception' % (index, case['method']))
        actual = json.loads(json.dumps(actual, allow_nan=False))
        expected = case['expected']
        if not equal(actual, expected):
            raise AssertionError('Case %d (%s) did not return the expected value' % (index, case['method']))
    if (args, kwargs) != before:
        raise AssertionError('Case %d (%s) mutated its caller input' % (index, case['method']))
print(json.dumps({'status':'passed','cases_run':len(payload['cases'])}))
'''


def _run_cases(candidate: Path, entry: dict) -> None:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    payload = {"module": _module_path(entry), "symbol": entry["symbol"], "cases": entry["cases"]}
    try:
        result = subprocess.run([sys.executable, "-c", _CASE_WORKER], input=json.dumps(payload), cwd=candidate,
                                env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except subprocess.TimeoutExpired as error:
        raise ValueError("Utility method cases timed out") from error
    if result.returncode:
        # Exception messages can contain argument values. Report only the fixed
        # worker assertion text, never arbitrary utility tracebacks or values.
        diagnostic = next((line for line in reversed(result.stderr.splitlines()) if re.fullmatch(r"AssertionError: Case \d+ \([A-Za-z_]\w*\) (?:raised unexpected [A-Za-z]+|did not raise the expected exception|did not return the expected value|mutated its caller input)", line)), "Method case execution failed")
        raise ValueError("Utility verification failed: " + diagnostic)
    try:
        proof = json.loads(result.stdout)
    except ValueError as error:
        raise ValueError("Utility verification did not return an execution receipt") from error
    if proof != {"status": "passed", "cases_run": len(entry["cases"])}:
        raise ValueError("Utility verification did not complete every method case")


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".utility-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _candidate_sources(root: Path) -> dict[str, str]:
    paths = [path for path in root.rglob("*.py") if path.is_file()]
    paths.append(root / REGISTRY_FILE)
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if not set(path.relative_to(root).parts) & _SKIP_DIRS}


def write_utility(
    id: str, source: str, description: str, cases: list[dict],
    expected_sha256: str | None = None, project_root: str | Path = ".",
    allow_update: bool = False, verify_command: list[str] | None = None,
) -> dict:
    """Verify before publishing; updates require an explicit hash and project check."""
    relative = _identity(id)
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Utility description must explain its reusable purpose")
    root = Path(project_root).resolve()
    with _lock(root, writing=True):
        registry = _registry(root)
        entries = registry["utilities"]
        for item in entries:
            _verified_entry(root, item, entries)
        _no_cycles(entries)
        current = next((entry for entry in entries if entry["id"] == id), None)
        methods, dependencies = _source_metadata(id, source, entries)
        normalized_cases = _cases(cases, methods)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if current and current["sha256"] == digest:
            return {**deepcopy(current), "operation": "reused"}
        if current:
            if not allow_update:
                raise ValueError("Utility already exists; reuse it or request an explicit update")
            if expected_sha256 != current["sha256"]:
                raise ValueError("Utility update requires its current expected_sha256")
            if not isinstance(verify_command, list) or not verify_command or not all(isinstance(part, str) and part for part in verify_command):
                raise ValueError("Utility update requires an explicit project verify_command argv")
            signatures = {method["name"]: method["signature"] for method in methods}
            if any(signatures.get(method["name"]) != method["signature"] for method in current["methods"]):
                raise ValueError("Existing utility method signatures are frozen; use a new id for incompatible APIs")
            normalized_cases = _cases([*current["cases"], *normalized_cases], methods)
        target = _path(root, relative)
        if not current and (target.exists() or any(entry["path"].casefold() == relative.casefold() for entry in entries)):
            raise ValueError("Utility path is already occupied by another source or registration")
        entry = {"id": id, "language": "python", "path": relative, "symbol": id,
                 "description": description.strip(), "methods": methods, "dependencies": dependencies,
                 "sha256": digest, "cases": normalized_cases}
        updated = {**registry, "utilities": [item for item in entries if item["id"] != id] + [entry]}
        _no_cycles(updated["utilities"])
        with tempfile.TemporaryDirectory(prefix="aipod-utility-check-") as temporary:
            candidate = Path(temporary) / "project"
            shutil.copytree(root, candidate, ignore=shutil.ignore_patterns(*_SKIP_DIRS, _LOCK_FILE))
            candidate_source = candidate / relative
            candidate_source.parent.mkdir(parents=True, exist_ok=True)
            candidate_source.write_text(source, encoding="utf-8")
            (candidate / REGISTRY_FILE).write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            frozen_sources = _candidate_sources(candidate)
            _run_cases(candidate, entry)
            if current:
                command = [sys.executable if part in {"{python}", "python", "python3"} else str(candidate) if part == "{project_root}" else part for part in verify_command]
                try:
                    checked = subprocess.run(command, cwd=candidate, stdin=subprocess.DEVNULL, capture_output=True,
                                             text=True, encoding="utf-8", errors="replace", timeout=120,
                                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise ValueError("Utility project verification could not complete") from error
                if checked.returncode:
                    raise ValueError("Utility update rejected: project verification failed")
            if _candidate_sources(candidate) != frozen_sources:
                raise ValueError("Utility verification changed candidate source, registry, or frozen callers")
        registry_path = _path(root, REGISTRY_FILE)
        previous_source = target.read_bytes() if current else None
        previous_registry = registry_path.read_bytes() if registry_path.exists() else None
        try:
            _atomic_bytes(target, source.encode("utf-8"))
            _atomic_bytes(registry_path, (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        except Exception:
            if previous_source is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_bytes(target, previous_source)
            if previous_registry is None:
                registry_path.unlink(missing_ok=True)
            else:
                _atomic_bytes(registry_path, previous_registry)
            raise
        return {**deepcopy(entry), "operation": "updated" if current else "created"}


__all__ = ["list_utilities", "read_utility", "write_utility", "REGISTRY_FILE"]
