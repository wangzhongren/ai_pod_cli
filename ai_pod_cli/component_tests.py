"""Owned unittest execution for implementation-independent component test sources."""

from __future__ import annotations

import ast
import argparse
import functools
import hashlib
import importlib.util
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

from ai_pod_cli.behavior_tests import _Evidence
from ai_pod_cli.container import _ComponentRef
from ai_pod_cli.sandbox import _copy_sandbox_project, _isolate_sandbox_database
from ai_pod_cli.testing import SDK_METHODS, SDK_PROMPT, Sandbox, TestSetupError, _clear_context, _configure_context


TEST_SETUP = "[AIPOD_TEST_SETUP]"
TEST_INVALID = "[AIPOD_TEST_INVALID]"
_SCHEMA = "aipod.component-tests.v1"
_TEST_FILE = "_aipod_component_tests.py"


def _test_names(tree) -> list[str]:
    target = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ComponentTests"), None)
    return ["ComponentTests." + node.name for node in target.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")] if target else []


def validate_component_test_source(test_source: str, required_tests=None) -> list[str]:
    """Validate the stable SDK surface before test source is frozen, without executing it."""
    if not isinstance(test_source, str) or not test_source.strip():
        return [f"{TEST_SETUP} Component tests must be nonempty Python source"]
    try:
        tree = ast.parse(test_source)
    except SyntaxError as error:
        return [f"{TEST_SETUP} Test source syntax error at line {error.lineno}: {error.msg}"]
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ComponentTests"]
    if len(classes) != 1 or not any(
        isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) and base.value.id == "unittest" and base.attr == "TestCase"
        or isinstance(base, ast.Name) and base.id == "TestCase"
        for base in classes[0].bases
    ):
        return [f"{TEST_SETUP} Define class ComponentTests(unittest.TestCase)"]
    names = _test_names(tree)
    errors = []
    if not names or len(names) != len(set(names)):
        errors.append(f"{TEST_INVALID} ComponentTests must contain unique, nonempty test_ methods")
    if required_tests is not None and (
        not isinstance(required_tests, list) or not all(isinstance(name, str) for name in required_tests)
        or len(required_tests) != len(set(required_tests))
    ):
        return [f"{TEST_SETUP} required_tests must be a list of unique ClassName.test_method names"]
    for name in required_tests or []:
        if name not in names:
            errors.append(f"{TEST_INVALID} Declared test is missing: {name}")
    aliases = {"Sandbox"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "ai_pod_cli.testing":
                aliases.update(item.asname or item.name for item in node.names if item.name == "Sandbox")
            if node.level or node.module == "modules" or (node.module or "").startswith("modules."):
                errors.append(f"{TEST_SETUP} Tests must access project components through Sandbox IDs, not direct imports (line {node.lineno})")
        if isinstance(node, ast.Import) and any(item.name == "modules" or item.name.startswith("modules.") for item in node.names):
            errors.append(f"{TEST_SETUP} Tests must not import target or project implementation modules (line {node.lineno})")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "compile", "__import__"}:
            errors.append(f"{TEST_INVALID} Dynamic code/imports cannot substitute component test evidence (line {node.lineno})")
    bindings = set()
    for node in ast.walk(tree):
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in aliases:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bindings.update(ast.unparse(target) for target in targets)
        if isinstance(node, ast.With):
            for item in node.items:
                expression = item.context_expr
                if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name) and expression.func.id in aliases and item.optional_vars is not None:
                    bindings.add(ast.unparse(item.optional_vars))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in aliases:
            if len(node.args) > 2 or any(item.arg not in {"config", "provider_overrides"} for item in node.keywords):
                errors.append(f"{TEST_SETUP} Sandbox accepts only config and provider_overrides (line {node.lineno})")
        if isinstance(node, ast.Attribute) and ast.unparse(node.value) in bindings and node.attr not in SDK_METHODS:
            errors.append(f"{TEST_SETUP} Unknown Sandbox API: {node.attr} (line {node.lineno})")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func.value) in bindings and node.func.attr in SDK_METHODS
                and not any(isinstance(arg, ast.Starred) for arg in node.args)
                and all(item.arg is not None for item in node.keywords)):
            try:
                inspect.signature(getattr(Sandbox, node.func.attr)).bind(
                    None, *[None for _ in node.args], **{item.arg: None for item in node.keywords},
                )
            except TypeError:
                errors.append(f"{TEST_SETUP} Invalid arguments for Sandbox.{node.func.attr} (line {node.lineno})")
    class_calls = [node for node in ast.walk(classes[0]) if isinstance(node, ast.Call)]
    if not any(isinstance(node.func, ast.Attribute) and node.func.attr in {"run", "call_provider", "model", "seed"}
               for node in class_calls):
        errors.append(f"{TEST_INVALID} ComponentTests must invoke a component through the Sandbox SDK")
    meaningful_assertion = False
    for node in class_calls:
        if not isinstance(node.func, ast.Attribute) or not node.func.attr.startswith("assert"):
            continue
        arguments = [*node.args, *(item.value for item in node.keywords if item.arg != "msg")]
        if not arguments:
            continue
        try:
            for argument in arguments:
                ast.literal_eval(argument)
        except (ValueError, TypeError, SyntaxError, RecursionError):
            meaningful_assertion = True
            break
    if not meaningful_assertion:
        errors.append(f"{TEST_INVALID} ComponentTests must contain a non-literal unittest assertion")
    return list(dict.fromkeys(errors))


class _ComponentEvidence(_Evidence):
    def __init__(self, root, bean):
        super().__init__(root)
        self.bean = bean
        self.active = []
        self.setup_error = False
        self.boundaries = {_ComponentRef._execute_with_policy.__code__, _ComponentRef._execute_with_policy_async.__code__}

    def start(self, test):
        self.current_test = test
        self.current = {"id": test.id(), "outcome": "running", "target_calls": 0,
                        "assertions": 0, "calls": [], "violations": []}
        self.tests.append(self.current)

    @contextmanager
    def dispatch(self, identifier, method, function):
        identity = getattr(function, "__func__", function)
        code = getattr(identity, "__code__", None)
        if identifier == self.bean["id"] and self.bean["category"] == "service" and code not in self.boundaries:
            raise TestSetupError("Tests cannot replace the governed component execution boundary")
        event = {"identifier": identifier, "method": method, "code": code, "seen": False}
        self.active.append(event)
        try:
            yield
        finally:
            self.active.pop()

    def profile(self, frame, event, _value):
        if event != "call" or self.current is None or not self.active:
            return
        call = self.active[-1]
        if call["seen"] or call["identifier"] != self.bean["id"] or frame.f_code is not call["code"]:
            return
        call["seen"] = True
        self.current["target_calls"] += 1
        self.current["calls"].append({"component": call["identifier"], "method": call["method"]})


class _ComponentResult(unittest.TextTestResult):
    def __init__(self, *args, evidence, **kwargs):
        super().__init__(*args, **kwargs)
        self.evidence = evidence
        self.setup_error = False

    def startTest(self, test):
        self.evidence.start(test)
        super().startTest(test)

    def _outcome(self, value):
        if self.evidence.current is not None:
            self.evidence.current["outcome"] = value

    def addSuccess(self, test):
        self._outcome("passed")
        super().addSuccess(test)

    def addFailure(self, test, error):
        self._outcome("failed")
        super().addFailure(test, error)

    def addError(self, test, error):
        self._outcome("error")
        self.setup_error = self.setup_error or isinstance(error[1], TestSetupError) or (
            isinstance(error[1], AttributeError) and isinstance(getattr(error[1], "obj", None), Sandbox)
        )
        super().addError(test, error)

    def addSkip(self, test, reason):
        self._outcome("skipped")
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, error):
        self._outcome("expected_failure")
        super().addExpectedFailure(test, error)

    def addUnexpectedSuccess(self, test):
        self._outcome("unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, error):
        if error is not None:
            self._outcome("failed")
        super().addSubTest(test, subtest, error)

    def stopTest(self, test):
        row = self.evidence.current
        if row is not None and row["outcome"] == "passed":
            if not row["target_calls"]:
                self.evidence.violation("Test did not actually invoke the registered target component through the SDK")
            if not row["assertions"]:
                self.evidence.violation("Test requires a non-literal unittest self.assert* assertion")
        super().stopTest(test)
        self.evidence.current = None
        self.evidence.current_test = None


@contextmanager
def _instrument_component(evidence):
    assertions = {name: value for name, value in vars(unittest.TestCase).items() if name.startswith("assert") and callable(value)}
    original_profile = sys.getprofile()
    raises_context = unittest.case._AssertRaisesContext
    original_raises_exit = raises_context.__exit__
    def observe(original):
        @functools.wraps(original)
        def assertion(test, *args, **kwargs):
            outermost = evidence.assertion_depth == 0
            literal = False
            if outermost and test is evidence.current_test:
                caller = sys._getframe(1)
                literal = evidence.is_literal_assertion(caller.f_code.co_filename, caller.f_lineno, original.__name__)
                del caller
            evidence.assertion_depth += 1
            try:
                returned = original(test, *args, **kwargs)
            except Exception:
                if outermost and test is evidence.current_test:
                    evidence.violation("A unittest assertion failed, even if its exception was caught by the test")
                raise
            finally:
                evidence.assertion_depth -= 1
            if outermost and not literal and test is evidence.current_test and evidence.current is not None:
                evidence.current["assertions"] += 1
            return returned
        return assertion
    def observed_raises_exit(context, kind, error, trace):
        try:
            returned = original_raises_exit(context, kind, error, trace)
        except Exception:
            if context.test_case is evidence.current_test:
                evidence.violation("An assertRaises expectation failed, even if the test caught the assertion")
            raise
        if kind is not None and not returned and context.test_case is evidence.current_test:
            evidence.violation("An assertRaises block raised the wrong exception")
        return returned
    try:
        for name, original in assertions.items():
            setattr(unittest.TestCase, name, observe(original))
        sys.setprofile(evidence.profile)
        raises_context.__exit__ = observed_raises_exit
        yield
    finally:
        sys.setprofile(original_profile)
        raises_context.__exit__ = original_raises_exit
        for name, original in assertions.items():
            setattr(unittest.TestCase, name, original)


def _restrict_worker_resources(root: Path, evidence) -> None:
    """Block external side effects while leaving SQLite and test resources explicit."""
    blocked = {"socket.connect", "socket.bind", "socket.getaddrinfo", "subprocess.Popen",
               "os.system", "os.posix_spawn", "os.fork", "os.forkpty"}
    def reject():
        evidence.setup_error = True
        evidence.violation("External network, process, or non-sandbox database access is unavailable")
        raise TestSetupError("Use explicit test data and dependency Provider overrides instead of external resources")
    def audit(event, arguments):
        if event in blocked:
            reject()
        if event == "sqlite3.connect":
            raw = os.fsdecode(arguments[0])
            if raw == ":memory:":
                return
            if raw.startswith("file:"):
                raw = raw[5:].split("?", 1)[0]
            if raw == ":memory:":
                return
            from ai_pod_cli import testing
            allowed = [root]
            if testing._CONTEXT is not None:
                allowed.extend(sandbox._root for sandbox in testing._CONTEXT["sandboxes"] if hasattr(sandbox, "_root"))
            database = Path(raw).resolve()
            if not any(database.is_relative_to(parent) for parent in allowed):
                reject()
    sys.addaudithook(audit)


def _worker(bean: dict, test_name: str) -> dict:
    root = Path.cwd().resolve()
    evidence = _ComponentEvidence(root, bean)
    report = {"schema": _SCHEMA, "status": "failed", "tests_run": 0, "tests": [], "marker": ""}
    captured = io.StringIO()
    _restrict_worker_resources(root, evidence)
    try:
        with redirect_stdout(captured), redirect_stderr(captured):
            _configure_context(root, bean, evidence)
            with _instrument_component(evidence):
                spec = importlib.util.spec_from_file_location("_aipod_owned_component_tests", root / _TEST_FILE)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                suite = unittest.defaultTestLoader.loadTestsFromName(test_name, module)
                runner = unittest.TextTestRunner(stream=captured, verbosity=2,
                    resultclass=lambda *args, **kwargs: _ComponentResult(*args, evidence=evidence, **kwargs))
                result = runner.run(suite)
            rows = evidence.tests
            report.update(tests_run=result.testsRun, tests=rows)
            valid = (result.wasSuccessful() and result.testsRun == 1 and len(rows) == 1
                     and rows[0]["outcome"] == "passed" and not rows[0]["violations"] and not evidence.setup_error)
            report["status"] = "passed" if valid else "failed"
            if result.setup_error or evidence.setup_error:
                report["marker"] = TEST_SETUP
            elif result.wasSuccessful() and not valid:
                report["marker"] = TEST_INVALID
    except BaseException as error:
        traceback.print_exc(file=captured)
        if isinstance(error, TestSetupError) or isinstance(error, AttributeError) and isinstance(getattr(error, "obj", None), Sandbox):
            report["marker"] = TEST_SETUP
        elif isinstance(error, SystemExit):
            report["marker"] = TEST_INVALID
    finally:
        _clear_context()
    report["log"] = captured.getvalue()
    return report


def _fingerprint(root: Path) -> dict:
    paths = [*root.rglob("*.py"), root / "beans_config.json"]
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.is_file()}


def _candidate_path(bean: dict) -> str:
    category = {"model": "models", "provider": "providers", "service": "services"}.get(bean.get("category"))
    file = bean.get("file")
    if not category or not isinstance(file, str) or Path(file).name != file or not file.endswith(".py") or "\\" in file:
        raise ValueError("Component candidate requires an exact category-local Python filename")
    relative = f"modules/{category}/{file}"
    if bean.get("class_path") != relative[:-3].replace("/", ".") + "." + str(bean.get("id")):
        raise ValueError("Component id, class_path, and source filename must identify the same target")
    return relative


def verify_component_test(
    project_root: str | Path, bean: dict, source: str, test_source: str,
    timeout: int = 30, required_tests=None,
) -> list[str]:
    """Execute each named test in its own project copy and process; never patch tests."""
    errors = validate_component_test_source(test_source, required_tests)
    if errors:
        return errors
    root = Path(project_root).resolve()
    try:
        relative = _candidate_path(bean)
    except ValueError as error:
        return [f"{TEST_SETUP} {error}"]
    tests = _test_names(ast.parse(test_source))
    for test_name in tests:
        with tempfile.TemporaryDirectory(prefix="aipod-component-test-") as temporary:
            candidate = Path(temporary) / "project"
            try:
                _copy_sandbox_project(root, candidate, copy_config=False)
                _isolate_sandbox_database(candidate)
                registry = json.loads((candidate / "beans_config.json").read_text(encoding="utf-8"))
                registry["beans"] = [item for item in registry.get("beans", []) if item.get("id") != bean["id"]] + [bean]
                (candidate / "beans_config.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
                target = candidate / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.encode("utf-8"))
                (candidate / "modules/__init__.py").touch(exist_ok=True)
                (candidate / _TEST_FILE).write_bytes(test_source.encode("utf-8"))
                before = _fingerprint(candidate)
            except Exception as error:
                errors.append(f"{TEST_SETUP} Could not prepare isolated component test ({type(error).__name__})")
                continue
            environment = {"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
                           "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
            try:
                completed = subprocess.run([sys.executable, "-m", "ai_pod_cli.component_tests", "--worker"],
                    input=json.dumps({"bean": bean, "test": test_name}), cwd=candidate,
                    env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
            except subprocess.TimeoutExpired:
                errors.append(f"Component test {test_name} timed out after {timeout} seconds")
                continue
            if _fingerprint(candidate) != before:
                errors.append(f"{TEST_INVALID} {test_name} changed candidate source, registry, or frozen tests")
                continue
            try:
                report = json.loads(completed.stdout)
            except ValueError:
                errors.append(f"{TEST_INVALID} {test_name} did not return a component execution proof\n{completed.stderr[-8000:]}")
                continue
            rows = report.get("tests", []) if isinstance(report, dict) else []
            proven = (isinstance(report, dict) and report.get("schema") == _SCHEMA and report.get("status") == "passed"
                      and report.get("tests_run") == 1 and len(rows) == 1 and rows[0].get("outcome") == "passed"
                      and rows[0].get("target_calls", 0) > 0 and rows[0].get("assertions", 0) > 0
                      and rows[0].get("id", "").endswith("." + test_name))
            if completed.returncode or not proven:
                marker = report.get("marker", "") if isinstance(report, dict) else TEST_INVALID
                violations = [value for row in rows for value in row.get("violations", [])]
                errors.append(f"{marker} Component test {test_name} failed\n" + "\n".join(violations) + "\n" + str(report.get("log", ""))[-16000:])
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run frozen component tests with the framework Sandbox SDK")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--component", default="")
    arguments = parser.parse_args(argv)
    if arguments.worker:
        payload = json.load(sys.stdin)
        report = _worker(payload["bean"], payload["test"])
    else:
        from ai_pod_cli.test_generation import verify_frozen_component
        root = Path(arguments.project_root).resolve()
        report = {"schema": _SCHEMA, "status": "failed", "components": [], "errors": []}
        try:
            beans = json.loads((root / "beans_config.json").read_text(encoding="utf-8")).get("beans", [])
            selected = [bean for bean in beans if bean.get("id") == arguments.component] if arguments.component else [bean for bean in beans if isinstance(bean.get("component_test"), dict)]
            if not selected:
                report["errors"].append(f"{TEST_SETUP} No matching component with frozen tests was found")
            for bean in selected:
                source_path = root / _candidate_path(bean)
                errors = verify_frozen_component(root, bean, source_path.read_text(encoding="utf-8"))
                report["components"].append({"id": bean["id"], "status": "failed" if errors else "passed", "errors": errors})
                report["errors"].extend(errors)
            report["status"] = "failed" if report["errors"] else "passed"
        except (OSError, ValueError, TypeError, KeyError) as error:
            report["errors"].append(f"{TEST_SETUP} {type(error).__name__}: {error}")
    print(json.dumps(report, ensure_ascii=False, default=str))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SDK_PROMPT", "validate_component_test_source", "verify_component_test"]
