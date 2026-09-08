"""Run project-authored unittest acceptance tests with real execution evidence.

Usage: ``python -m ai_pod_cli.behavior_tests tests/test_behavior.py`` from the
project root. Each successful test must complete a registered PipelineRunner
route and use at least one ``self.assert*`` unittest assertion. Plain Python
``assert`` statements and calls whose arguments are all literal constants do not
count. Skips and expected failures are reported,
but cannot alone establish acceptance. No fixtures or parameters are synthesized.

This is a minimum evidence gate, not a test-coverage proof or a security boundary:
authors must still choose meaningful inputs and assert the required outcomes.
The literal check uses source ASTs, not data-flow analysis: assertions involving
variables or attributes are accepted without proving their dependence on a route.
Exception-only negative tests need a successful route call too; expected-error
assertions alone do not establish that a runnable application was delivered.
The calling verifier owns the subprocess timeout for hung imports or tests.
All temporary instrumentation is restored before this module returns.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import io
import json
from pathlib import Path
import sys
import traceback
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout

import tomlkit

from ai_pod_cli.result import Failure
from ai_pod_cli.runner import PipelineRunner


class _Evidence:
    def __init__(self, root: Path):
        self.root = root
        self.current_test = None
        self.current = None
        self.tests: list[dict] = []
        self.assertion_depth = 0
        self._assertion_sources: dict[str, list[ast.Call]] = {}

    def is_literal_assertion(self, filename, lineno, assertion_name):
        """Recognize obvious constant-only calls; never evaluate project code."""
        if filename not in self._assertion_sources:
            try:
                tree = ast.parse(Path(filename).read_text(encoding="utf-8"), filename)
                calls = [
                    node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr.startswith("assert")
                ]
            except (OSError, SyntaxError, UnicodeError):
                calls = []
            self._assertion_sources[filename] = calls
        candidates = [
            call for call in self._assertion_sources[filename]
            if call.func.attr == assertion_name
            and call.lineno <= lineno <= (call.end_lineno or call.lineno)
        ]
        if not candidates:
            return False
        for call in candidates:
            arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
            if not arguments:
                return False
            try:
                for argument in arguments:
                    ast.literal_eval(argument)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                return False
        return True

    def start(self, test):
        self.current_test = test
        self.current = {
            "id": test.id(), "outcome": "running", "route_calls": 0,
            "assertions": 0, "routes": [], "violations": [],
        }
        self.tests.append(self.current)

    def violation(self, message):
        if self.current is not None and message not in self.current["violations"]:
            self.current["violations"].append(message)

    def registered(self, runner, route_name):
        """Only accept routes from this project's real registry and files."""
        try:
            registry = self.root / "routes.toml"
            if Path(runner._routes_path).resolve() != registry.resolve():
                raise ValueError("route runner must use the project's routes.toml")
            with registry.open(encoding="utf-8") as stream:
                declared = tomlkit.load(stream).get(route_name)
            if not isinstance(declared, dict) or declared != runner._routes.get(route_name):
                raise ValueError("route must match the project registry")
            pipeline = Path(declared.get("pipeline", "")).resolve()
            pipeline.relative_to(self.root)
            if not pipeline.is_file():
                raise ValueError("registered pipeline file does not exist")
        except (OSError, ValueError, TypeError) as error:
            self.violation(f"Route {route_name!r} has no project execution evidence: {error}")
            return False
        return True

    def completed(self, route_name, returned, registered):
        if self.current is None:
            return
        if not isinstance(returned, tuple) or len(returned) != 2:
            self.violation(f"Route {route_name!r} did not return runner context evidence")
            return
        result, ctx = returned
        if registered:
            self.current["route_calls"] += 1
            self.current["routes"].append(str(route_name))
        # The runner serializes Failure. Do not mistake a business dict with a
        # plain 'status' field for that structured framework result.
        normalized_failure = (
            isinstance(result, dict) and result.get("status") == "failure"
            and isinstance(result.get("error"), dict)
            and {"code", "message"}.issubset(result["error"])
            and "context" in result and "effects" in result
        )
        context_failure = isinstance(getattr(ctx, "failure", None), Failure)
        if hasattr(ctx, "get"):
            context_failure = context_failure or isinstance(ctx.get("failure"), Failure)
        failed_steps = [
            step for step in getattr(ctx, "steps", [])
            if isinstance(step, dict) and step.get("status") == "failure"
        ]
        if isinstance(result, Failure) or normalized_failure or context_failure or failed_steps:
            self.violation(f"Route {route_name!r} produced a framework failure")


class _EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, evidence, **kwargs):
        super().__init__(*args, **kwargs)
        self.evidence = evidence

    def startTest(self, test):
        self.evidence.start(test)
        super().startTest(test)

    def _outcome(self, name):
        if self.evidence.current is not None:
            self.evidence.current["outcome"] = name

    def addSuccess(self, test):
        self._outcome("passed")
        super().addSuccess(test)

    def addError(self, test, err):
        self._outcome("error")
        super().addError(test, err)

    def addFailure(self, test, err):
        self._outcome("failed")
        super().addFailure(test, err)

    def addSkip(self, test, reason):
        self._outcome("skipped")
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self._outcome("expected_failure")
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self._outcome("unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._outcome("failed")
        super().addSubTest(test, subtest, err)

    def stopTest(self, test):
        record = self.evidence.current
        if record is not None and record["outcome"] == "passed":
            if record["route_calls"] == 0:
                self.evidence.violation("Test must complete a real registered PipelineRunner route")
            if record["assertions"] == 0:
                self.evidence.violation(
                    "Test must use a non-literal unittest self.assert* assertion; "
                    "Python assert and constant-only assertion calls do not count"
                )
        super().stopTest(test)
        self.evidence.current = None
        self.evidence.current_test = None


@contextmanager
def _instrument(evidence):
    """Observe public execution and assertions without changing return values."""
    original_sync = PipelineRunner.run_with_context
    original_async = PipelineRunner.run_with_context_async
    original_loader = PipelineRunner._load_route_module
    original_finalizer = PipelineRunner._finalize_result
    assertion_methods = {
        name: value for name, value in vars(unittest.TestCase).items()
        if name.startswith("assert") and callable(value)
    }

    def real_runner(runner, route_name):
        registered = evidence.registered(runner, route_name)
        if (
            getattr(runner._load_route_module, "__func__", None) is not original_loader
            or runner._finalize_result is not original_finalizer
        ):
            evidence.violation(f"Route {route_name!r} replaced the real PipelineRunner loader or finalizer")
            return False
        return registered

    @functools.wraps(original_sync)
    def run_with_context(runner, route_name, params=None):
        registered = real_runner(runner, route_name)
        returned = original_sync(runner, route_name, params)
        evidence.completed(route_name, returned, registered)
        return returned

    @functools.wraps(original_async)
    async def run_with_context_async(runner, route_name, params=None):
        registered = real_runner(runner, route_name)
        returned = await original_async(runner, route_name, params)
        evidence.completed(route_name, returned, registered)
        return returned

    def observed_assertion(original):
        @functools.wraps(original)
        def assertion(test, *args, **kwargs):
            outermost = evidence.assertion_depth == 0
            literal = False
            if outermost and test is evidence.current_test:
                caller = sys._getframe(1)
                literal = evidence.is_literal_assertion(
                    caller.f_code.co_filename, caller.f_lineno, original.__name__,
                )
                del caller
            evidence.assertion_depth += 1
            try:
                returned = original(test, *args, **kwargs)
            finally:
                evidence.assertion_depth -= 1
            if outermost and not literal and test is evidence.current_test and evidence.current is not None:
                evidence.current["assertions"] += 1
            return returned
        return assertion

    try:
        PipelineRunner.run_with_context = run_with_context
        PipelineRunner.run_with_context_async = run_with_context_async
        for name, original in assertion_methods.items():
            setattr(unittest.TestCase, name, observed_assertion(original))
        yield
    finally:
        PipelineRunner.run_with_context = original_sync
        PipelineRunner.run_with_context_async = original_async
        for name, original in assertion_methods.items():
            setattr(unittest.TestCase, name, original)


def run_behavior_tests(test_file: str) -> dict:
    """Run one project-local test module and return JSON-compatible evidence."""
    root = Path.cwd().resolve()
    evidence = _Evidence(root)
    report = {
        "schema": "aipod.behavior-tests.v1", "status": "error",
        "test_file": test_file, "tests_run": 0, "successful_tests": 0,
        "skipped": 0, "expected_failures": 0, "failures": 0, "errors": 0,
        "route_calls": 0, "assertions": 0, "violations": [], "tests": [],
    }
    captured = io.StringIO()
    original_path = sys.path[:]
    module_name = "_aipod_project_behavior_tests"
    previous_module = sys.modules.get(module_name)
    try:
        requested = Path(test_file)
        if requested.is_absolute():
            raise ValueError("test_file must be a project-relative .py path")
        path = requested.resolve()
        path.relative_to(root)
        if path.suffix != ".py" or not path.is_file():
            raise ValueError("test_file must be an existing project-local .py file")
        sys.path[:0] = [str(root), str(path.parent)]
        with redirect_stdout(captured), redirect_stderr(captured), _instrument(evidence):
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
            runner = unittest.TextTestRunner(
                stream=captured, verbosity=2,
                resultclass=lambda *args, **kwargs: _EvidenceResult(
                    *args, evidence=evidence, **kwargs,
                ),
            )
            result = runner.run(suite)
        report.update({
            "tests_run": result.testsRun,
            "successful_tests": sum(row["outcome"] == "passed" for row in evidence.tests),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "failures": len(result.failures) + len(result.unexpectedSuccesses),
            "errors": len(result.errors),
        })
        report["violations"] = [
            {"test": row["id"], "reason": violation}
            for row in evidence.tests for violation in row["violations"]
        ]
        if not report["successful_tests"]:
            report["violations"].append({
                "test": "", "reason": "At least one passing, non-skipped, non-expected-failure test is required",
            })
        report["status"] = (
            "passed" if result.wasSuccessful() and not report["violations"] else "failed"
        )
    except BaseException as error:
        report["errors"] += 1
        report["violations"].append({"test": "", "reason": f"{type(error).__name__}: {error}"})
        traceback.print_exc(file=captured)
    finally:
        sys.path[:] = original_path
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
    report["tests"] = evidence.tests
    report["route_calls"] = sum(row["route_calls"] for row in evidence.tests)
    report["assertions"] = sum(row["assertions"] for row in evidence.tests)
    report["aipod_behavior_proof"] = {
        "version": 1, "tests_run": report["tests_run"],
        "route_calls": report["route_calls"], "assertions": report["assertions"],
        "success": report["status"] == "passed",
    }
    if captured.getvalue():
        sys.stderr.write(captured.getvalue())
    return report


def main(argv=None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("Usage: python -m ai_pod_cli.behavior_tests <project-relative test_file.py>", file=sys.stderr)
        return 2
    report = run_behavior_tests(arguments[0])
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
