"""Regressions for the real-route unittest acceptance evidence gate."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from ai_pod_cli.behavior_tests import run_behavior_tests
from ai_pod_cli.runner import PipelineRunner


class BehaviorTestsDriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pipelines").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "routes.toml").write_text(
            '[advance]\npipeline = "pipelines/advance.py"\n', encoding="utf-8",
        )
        self.pipeline("return {'position': ctx.params['position'] + ctx.params['speed']}")

    def pipeline(self, source, *, asynchronous=False):
        prefix = "async def" if asynchronous else "def"
        (self.root / "pipelines/advance.py").write_text(
            f"{prefix} run(ctx):\n" + textwrap.indent(source, "    ") + "\n", encoding="utf-8",
        )

    def write_test_module(self, body, *, prefix="", base="unittest.TestCase"):
        source = (
            "import unittest\nfrom ai_pod_cli.runner import PipelineRunner\n"
            + prefix + f"\nclass Acceptance({base}):\n" + textwrap.indent(body, "    ")
        )
        (self.root / "tests/acceptance.py").write_text(source, encoding="utf-8")

    def invoke(self, target="tests/acceptance.py"):
        result = subprocess.run(
            [sys.executable, "-m", "ai_pod_cli.behavior_tests", target],
            cwd=self.root, text=True, capture_output=True, timeout=20,
        )
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"Driver did not emit one JSON report: {result.stdout}\n{result.stderr}")
        return result, report

    def test_sync_route_and_behavior_assertions_pass_with_json_evidence(self):
        self.write_test_module("""def test_motion(self):
    print('application output is kept out of the JSON protocol')
    result = PipelineRunner().run('advance', {'position': 4, 'speed': 3})
    self.assertEqual(result['position'], 7)
""")
        result, report = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["route_calls"], 1)
        self.assertEqual(report["tests_run"], 1)
        self.assertGreater(report["assertions"], 0)
        self.assertTrue(report["aipod_behavior_proof"]["success"])
        self.assertIn("application output", result.stderr)

    def test_async_route_and_context_assertions_pass(self):
        self.pipeline("ctx.set('position', ctx.params['position'] + 2)\nreturn ctx.summary()", asynchronous=True)
        self.write_test_module("""async def test_motion(self):
    result, ctx = await PipelineRunner().run_with_context_async('advance', {'position': 5})
    self.assertEqual(ctx.get('position'), 7)
    result = await PipelineRunner().run_async('advance', {'position': 9})
    self.assertEqual(result['data']['position'], 11)
""", base="unittest.IsolatedAsyncioTestCase")
        result, report = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["route_calls"], 2)

    def test_missing_real_inputs_fail_instead_of_being_synthesized(self):
        self.write_test_module("""def test_missing_speed(self):
    result = PipelineRunner().run('advance', {'position': 4})
    self.assertIsNotNone(result)
""")
        result, report = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["errors"], 1)
        self.assertIn("KeyError: 'speed'", result.stderr)

    def test_incorrect_behavior_assertion_fails_even_when_route_returns(self):
        self.write_test_module("""def test_motion(self):
    result = PipelineRunner().run('advance', {'position': 4, 'speed': 3})
    self.assertEqual(result['position'], 8)
""")
        result, report = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["failures"], 1)
        self.assertEqual(report["route_calls"], 1)

    def test_noop_and_constant_assertion_do_not_prove_route_execution(self):
        for statement in ("pass", "self.assertTrue(True)"):
            with self.subTest(statement=statement):
                self.write_test_module(f"def test_empty(self):\n    {statement}\n")
                result, report = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report["route_calls"], 0)
                self.assertTrue(report["violations"])

    def test_real_route_with_only_literal_assertions_does_not_prove_behavior(self):
        for assertion in (
            "self.assertTrue(True)",
            "self.assertEqual(1, 1)",
            "self.assertEqual([1, 2], [1, 2])",
            "self.assertEqual(\n        {'x': [1]},\n        {'x': [1]},\n        msg='constant-only',\n    )",
        ):
            with self.subTest(assertion=assertion):
                self.write_test_module("""def test_fake_behavior(self):
    result = PipelineRunner().run('advance', {'position': 4, 'speed': 3})
    """ + assertion + "\n")
                result, report = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report["route_calls"], 1)
                self.assertEqual(report["assertions"], 0)
                self.assertTrue(any("constant-only" in item["reason"] for item in report["violations"]))

    def test_return_value_assertion_counts_even_beside_constant_assertions(self):
        self.write_test_module("""def test_real_behavior(self):
    result = PipelineRunner().run('advance', {'position': 4, 'speed': 3})
    self.assertTrue(True)
    self.assertEqual([1, 2], [1, 2])
    self.assertEqual(result['position'], 7)
""")
        result, report = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["route_calls"], 1)
        self.assertEqual(report["assertions"], 1)

    def test_every_passing_test_needs_its_own_route_and_assertion(self):
        self.write_test_module("""def test_actual(self):
    result = PipelineRunner().run('advance', {'position': 4, 'speed': 3})
    self.assertEqual(result['position'], 7)

def test_unproven(self):
    self.assertTrue(True)
""")
        result, report = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["tests_run"], 2)
        self.assertTrue(any("test_unproven" in item["test"] for item in report["violations"]))

    def test_bare_assert_is_not_counted_as_unittest_evidence(self):
        self.write_test_module("""def test_motion(self):
    result = PipelineRunner().run('advance', {'position': 4, 'speed': 3})
    assert result['position'] == 7
""")
        result, report = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["assertions"], 0)

    def test_zero_skipped_and_expected_failure_only_suites_fail(self):
        bodies = (
            "pass\n",
            "@unittest.skip('unfinished')\ndef test_pending(self):\n    pass\n",
            "@unittest.expectedFailure\ndef test_pending(self):\n    self.assertEqual(1, 2)\n",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.write_test_module(body)
                result, report = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report["successful_tests"], 0)

    def test_failure_results_context_and_steps_cannot_be_ignored(self):
        sources = (
            "from ai_pod_cli.result import Failure\nreturn Failure('collision failed')",
            "from ai_pod_cli.result import Failure\nctx.set('failure', Failure('collision failed'))\nreturn {'ok': True}",
            "ctx.record_step('physics', {}, status='failure')\nreturn {'ok': True}",
        )
        for source in sources:
            with self.subTest(source=source):
                self.pipeline(source)
                self.write_test_module("""def test_ignored_failure(self):
    result = PipelineRunner().run('advance', {})
    self.assertIsInstance(result, dict)
""")
                result, report = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(any("framework failure" in item["reason"] for item in report["violations"]))

    def test_plain_business_status_is_not_a_structured_framework_failure(self):
        self.pipeline("ctx.set('failure', 'a valid business label')\nreturn {'status': 'failure', 'attempts': 3}")
        self.write_test_module("""def test_business_state(self):
    result = PipelineRunner().run('advance', {})
    self.assertEqual(result['attempts'], 3)
""")
        result, report = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mocked_runner_result_does_not_count(self):
        self.write_test_module("""def test_fake(self):
    with patch.object(PipelineRunner, 'run', return_value={'position': 7}):
        result = PipelineRunner().run('advance', {})
    self.assertEqual(result['position'], 7)
""", prefix="from unittest.mock import patch\n")
        result, report = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["route_calls"], 0)

    def test_mocked_route_loader_cannot_supply_fake_execution_evidence(self):
        self.write_test_module("""def test_fake(self):
    fake = SimpleNamespace(run=lambda ctx: {'position': 7})
    with patch.object(PipelineRunner, '_load_route_module', return_value=fake):
        result = PipelineRunner().run('advance', {})
    self.assertEqual(result['position'], 7)
""", prefix="from unittest.mock import patch\nfrom types import SimpleNamespace\n")
        result, report = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["route_calls"], 0)

    def test_in_memory_route_registration_is_not_real_project_registration(self):
        self.write_test_module("""def test_fake_registration(self):
    runner = PipelineRunner()
    runner._routes['invented'] = {'pipeline': 'pipelines/advance.py'}
    result = runner.run('invented', {'position': 4, 'speed': 3})
    self.assertEqual(result['position'], 7)
""")
        result, report = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["route_calls"], 0)

    def test_symlink_to_outside_test_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "external.py"
            target.write_text("raise RuntimeError('outside file executed')", encoding="utf-8")
            (self.root / "tests/acceptance.py").symlink_to(target)
            result, report = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["status"], "error")
        self.assertNotIn("outside file executed", result.stderr)

    def test_module_import_routes_do_not_count_for_individual_tests(self):
        self.write_test_module("""def test_unproven(self):
    self.assertEqual(cached_result['position'], 7)
""", prefix="cached_result = PipelineRunner().run('advance', {'position': 4, 'speed': 3})\n")
        result, report = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["route_calls"], 0)

    def test_missing_import_error_and_outside_paths_fail_with_json(self):
        self.write_test_module("pass\n", prefix="raise RuntimeError('broken import')\n")
        for target in ("tests/absent.py", "tests/acceptance.py", "../outside.py", str(self.root / "tests/acceptance.py")):
            with self.subTest(target=target):
                result, report = self.invoke(target)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(report["status"], "error")
                self.assertGreater(report["errors"], 0)

    def test_instrumentation_is_restored_after_import_exception(self):
        self.write_test_module("pass\n", prefix="raise RuntimeError('broken import')\n")
        sync = PipelineRunner.run_with_context
        asynchronous = PipelineRunner.run_with_context_async
        assertion = unittest.TestCase.assertEqual
        original_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stderr(io.StringIO()):
                report = run_behavior_tests("tests/acceptance.py")
        finally:
            os.chdir(original_cwd)
        self.assertEqual(report["status"], "error")
        self.assertIs(PipelineRunner.run_with_context, sync)
        self.assertIs(PipelineRunner.run_with_context_async, asynchronous)
        self.assertIs(unittest.TestCase.assertEqual, assertion)


if __name__ == "__main__":
    unittest.main()
