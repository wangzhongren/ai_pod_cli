"""Pipeline scenario execution must use explicit inputs and preserve failures."""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from ai_pod_cli.sandbox import verify_pipeline_candidate


class PipelineSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aipod_sandbox_test_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "modules/services").mkdir(parents=True)
        (self.root / "modules/__init__.py").touch()
        (self.root / "modules/services/__init__.py").touch()
        (self.root / "modules/services/consumer.py").write_text(textwrap.dedent("""
            class Consumer:
                def execute(self, ctx):
                    from ai_pod_cli.result import Failure
                    if ctx.get("fail", False):
                        return Failure("consumer unavailable")
                    return {"observed": ctx.get("total_bricks")}
        """), encoding="utf-8")
        vector = {
            "type": "object", "required": ["x", "y"],
            "properties": {"x": {"type": "float"}, "y": {"type": "float"}},
        }
        self.inputs = {
            "total_bricks": "int", "remaining_bricks": "int",
            "ball_position": vector, "paddle_position": vector,
        }
        (self.root / "beans_config.json").write_text(json.dumps({"beans": [{
            "id": "Consumer", "category": "service",
            "class_path": "modules.services.consumer.Consumer",
            "inputs": self.inputs, "outputs": {"observed": "int"},
        }]}), encoding="utf-8")
        self.pipeline = textwrap.dedent("""
            from ai_pod_cli.config import load_beans
            from ai_pod_cli.container import build_container, Pod
            from modules.services.consumer import Consumer

            def run(ctx):
                S = Pod(build_container(load_beans()))
                S(Consumer).execute_all(ctx)
                return ctx.summary()
        """)
        self.real_params = {
            "total_bricks": 10, "remaining_bricks": 9,
            "ball_position": {"x": 100.0, "y": 200.0},
            "paddle_position": {"x": 300.0, "y": 500.0},
        }

    def check(self, code, params=None, *, timeout=5, contract=None):
        return verify_pipeline_candidate(
            self.root, textwrap.dedent(code), self.inputs if contract is None else contract,
            timeout=timeout, cases=[{"name": "real-entry", "params": params or {}}],
        )

    def test_missing_consumer_fields_are_not_synthesized_from_contract(self):
        errors = self.check(self.pipeline)
        self.assertEqual(len(errors), 1)
        self.assertIn("real-entry", errors[0])
        for name in self.inputs:
            self.assertIn(f"Consumer.{name}: required field is missing", errors[0])

    def test_explicit_valid_entry_inputs_satisfy_real_component_contract(self):
        self.assertEqual(self.check(self.pipeline, self.real_params), [])

    def test_context_contains_only_explicit_params_not_inferred_defaults(self):
        self.assertEqual(self.check("""
            def run(ctx):
                assert ctx.params == {"request": 5}
                assert ctx.data == {}
                return {"observed": ctx.params["request"]}
        """, {"request": 5}, contract={
            "inferred_only": {"model": "unavailable.models.Missing", "default": 1},
        }), [])

    def test_input_path_is_not_materialized_as_a_synthetic_file(self):
        errors = self.check("""
            from pathlib import Path
            def run(ctx):
                return {"text": Path(ctx.params["input_path"]).read_text()}
        """, {"input_path": "missing.log"})
        self.assertEqual(len(errors), 1)
        self.assertIn("FileNotFoundError", errors[0])
        self.assertFalse((self.root / "missing.log").exists())

    def test_existing_project_file_remains_an_available_explicit_fixture(self):
        (self.root / "source.log").write_text("real project input", encoding="utf-8")
        self.assertEqual(self.check("""
            from pathlib import Path
            def run(ctx):
                assert Path(ctx.params["input_path"]).read_text() == "real project input"
                return {"read": True}
        """, {"input_path": "source.log"}), [])

    def test_failure_return_is_rejected_without_an_exception(self):
        errors = self.check("""
            from ai_pod_cli.result import Failure
            def run(ctx):
                return Failure("route declined")
        """)
        self.assertEqual(len(errors), 1)
        self.assertIn("route declined", errors[0])

    def test_component_failure_cannot_be_hidden_by_returning_context_summary(self):
        errors = self.check(self.pipeline, {**self.real_params, "fail": True})
        self.assertEqual(len(errors), 1)
        self.assertIn("consumer unavailable", errors[0])
        self.assertIn("Consumer", errors[0])

    def test_explicit_context_failure_is_also_rejected(self):
        errors = self.check("""
            from ai_pod_cli.result import Failure
            def run(ctx):
                ctx.failure = Failure("context declined")
                return ctx.summary()
        """)
        self.assertEqual(len(errors), 1)
        self.assertIn("context declined", errors[0])

    def test_business_status_named_failure_is_not_a_framework_failure(self):
        self.assertEqual(self.check("""
            from ai_pod_cli.result import Success
            def run(ctx):
                ctx.set("status", "failure")
                ctx.set("failure", "a domain label")
                return Success({"status": "failure", "failed_count": 7})
        """), [])
        self.assertEqual(self.check("""
            def run(ctx):
                return {"status": "failure", "reason": "a business record"}
        """), [])

    def test_explicit_parallel_ignore_policy_is_respected(self):
        self.assertEqual(self.check("""
            from ai_pod_cli.container import _ComponentRef, parallel
            from ai_pod_cli.result import Failure
            class FailedBranch:
                def execute(self, ctx):
                    return Failure("allowed branch failure")
            class GoodBranch:
                def execute(self, ctx):
                    return {"answer": 1}
            def run(ctx):
                return parallel(
                    _ComponentRef("failed", FailedBranch()),
                    _ComponentRef("good", GoodBranch()),
                    failure_policy="ignore",
                ).execute_all(ctx)
        """), [])

    def test_async_entry_is_awaited_and_its_failure_is_not_dropped(self):
        errors = self.check("""
            import asyncio
            from ai_pod_cli.result import Failure
            async def run(ctx):
                await asyncio.sleep(0)
                return Failure("async declined")
        """)
        self.assertEqual(len(errors), 1)
        self.assertIn("async declined", errors[0])
        self.assertEqual(self.check("""
            import asyncio
            def run(ctx):
                raise AssertionError("wrong entry selected")
            async def run_async(ctx):
                await asyncio.sleep(0)
                assert ctx.params == {"name": "provided"}
                return {"ready": True}
        """, {"name": "provided"}), [])

    def test_scenarios_isolate_params_module_state_and_project_files(self):
        code = textwrap.dedent("""
            from pathlib import Path
            runs = 0
            def run(ctx):
                global runs
                runs += 1
                assert runs == 1
                assert ctx.params == {"nested": {"value": 1}}
                assert ctx.data == {}
                assert not Path("scenario-created.txt").exists()
                ctx.params["nested"]["value"] = 2
                ctx.set("leaked", True)
                Path("scenario-created.txt").write_text("local effect")
                return ctx.summary()
        """)
        shared = {"nested": {"value": 1}}
        cases = [{"name": name, "params": shared} for name in ("first", "second")]
        self.assertEqual(verify_pipeline_candidate(self.root, code, {}, cases=cases), [])
        self.assertEqual(shared, {"nested": {"value": 1}})
        self.assertFalse((self.root / "scenario-created.txt").exists())

    def test_all_failed_scenarios_are_reported_with_their_names(self):
        errors = verify_pipeline_candidate(self.root, "def run(ctx): raise ValueError('bad entry')", {}, cases=[
            {"name": "first", "params": {}}, {"name": "second", "params": {}},
        ])
        self.assertEqual(len(errors), 2)
        self.assertIn("first", errors[0])
        self.assertIn("second", errors[1])
        self.assertTrue(all("bad entry" in error for error in errors))

    def test_timeout_names_the_case(self):
        errors = self.check("""
            import time
            def run(ctx):
                time.sleep(5)
        """, timeout=0.2)
        self.assertEqual(len(errors), 1)
        self.assertIn("real-entry", errors[0])
        self.assertIn("0.2", errors[0])

    def test_exit_zero_before_completion_cannot_fake_a_pass(self):
        errors = self.check("""
            def run(ctx):
                raise SystemExit(0)
        """)
        self.assertEqual(len(errors), 1)
        self.assertIn("提前退出", errors[0])

    def test_missing_empty_or_malformed_cases_never_become_generated_inputs(self):
        for cases in (
            None, [], {}, "sample", [None], [{}],
            [{"name": "", "params": {}}], [{"name": "  ", "params": {}}],
            [{"name": 1, "params": {}}], [{"name": "case"}],
            [{"name": "case", "params": None}], [{"name": "case", "params": []}],
            [{"name": "case", "params": {1: "non-string key"}}],
            [{"name": "case", "params": {"invalid": object()}}],
            [{"name": "case", "params": {"invalid": float("nan")}}],
            [{"name": "same", "params": {}}, {"name": " same ", "params": {}}],
        ):
            with self.subTest(cases=cases):
                errors = verify_pipeline_candidate(self.root, "def run(ctx): return {}", self.inputs, cases=cases)
                self.assertEqual(len(errors), 1)
                self.assertIn("实际输入场景", errors[0])


if __name__ == "__main__":
    unittest.main()
