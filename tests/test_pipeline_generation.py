"""Integration checks for frozen public Pipeline inputs and real entry scenarios."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_pod_cli.commands.compose import handle_compose
from ai_pod_cli.config import register_route
from ai_pod_cli.pipeline_validation import (
    load_pipeline_inputs, save_pipeline_inputs, validate_pipeline_inputs,
)
from ai_pod_cli.pod.tools.pipelines import generate_pipelines
from ai_pod_cli.pod.routes import load_routes_map
from ai_pod_cli.project_model import build_project_model
from ai_pod_cli.runner import PipelineRunner
from ai_pod_cli.sandbox import verify_pipeline_candidate
from ai_pod_cli.source_codec import encode_source_artifact


class PipelineGenerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aipod_pipeline_generation_")
        self.addCleanup(temporary.cleanup)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        self.root = Path(temporary.name)
        os.chdir(self.root)
        Path("modules/services").mkdir(parents=True)
        Path("pipelines").mkdir()
        Path("modules/__init__.py").touch()
        Path("modules/services/__init__.py").touch()
        Path("modules/services/consumer.py").write_text(textwrap.dedent("""
            class Consumer:
                def execute(self, ctx):
                    return {"observed": ctx.get("internal_count")}
        """), encoding="utf-8")
        Path("beans_config.json").write_text(json.dumps({"beans": [{
            "id": "Consumer", "category": "service",
            "class_path": "modules.services.consumer.Consumer",
            "inputs": {"internal_count": "int"}, "outputs": {"observed": "int"},
        }]}), encoding="utf-8")
        Path("routes.toml").write_text("# Routes\n", encoding="utf-8")
        Path("config.toml").write_text("# No external dependencies\n", encoding="utf-8")
        self.inputs = {"request": {"type": "int"}}
        self.cases = [{"name": "real request", "params": {"request": 7}}]
        self.metadata = {
            "pipeline_ids": ["Consumer"], "inputs": self.inputs,
            "verification_cases": self.cases,
        }
        self.good_source = textwrap.dedent("""
            from ai_pod_cli.config import load_beans
            from ai_pod_cli.container import build_container, Pod
            from modules.services.consumer import Consumer

            def run(ctx):
                assert set(ctx.params) == {"request"}
                assert "internal_count" not in ctx.params
                ctx.set("internal_count", ctx.get("request") + 1)
                S = Pod(build_container(load_beans()))
                S(Consumer).execute_all(ctx)
                return ctx.summary()
        """)
        self.bad_source = self.good_source.replace(
            '    ctx.set("internal_count", ctx.get("request") + 1)\n', "",
        )

    def args(self, **overrides):
        return SimpleNamespace(**{
            "cmd": "Convert the caller request into internal_count and run Consumer",
            "name": "demo", "list": False, "json": True,
            "auto_repair": True, **overrides,
        })

    def compose(self, llm, **args):
        with redirect_stdout(io.StringIO()), patch(
            "ai_pod_cli.commands.compose.call_llm", side_effect=llm,
        ):
            return handle_compose(self.args(**args))

    def test_missing_cases_are_rejected_before_source_or_registration(self):
        modes = []

        def llm(*_args, **options):
            modes.append(options["json_mode"])
            self.assertTrue(options["json_mode"], "Invalid metadata must not reach source generation")
            return {"pipeline_ids": ["Consumer"], "inputs": self.inputs}

        self.assertFalse(self.compose(llm))
        self.assertEqual(modes, [True, True, True])
        self.assertEqual(PipelineRunner().route_names(), [])
        self.assertFalse(Path("pipelines/demo.py").exists())
        self.assertFalse(Path("pipelines/demo.contract.json").exists())

    def test_source_repair_reuses_one_frozen_metadata_request_and_unchanged_cases(self):
        modes = []
        metadata = copy.deepcopy(self.metadata)
        initial = copy.deepcopy(metadata)
        frozen_prompts = []

        def llm(_system, user, **options):
            modes.append(options["json_mode"])
            if options["json_mode"]:
                return metadata
            frozen = json.loads(user.rsplit("Frozen metadata:\n", 1)[1])
            frozen_prompts.append(frozen)
            if len(frozen_prompts) == 1:
                return encode_source_artifact("pipelines/demo.py", self.bad_source)
            self.assertIn("internal_count: required field is missing", user)
            return encode_source_artifact("pipelines/demo.py", self.good_source)

        with patch("ai_pod_cli.commands.compose.verify_pipeline_candidate", wraps=verify_pipeline_candidate) as sandbox:
            self.assertTrue(self.compose(llm))
        self.assertEqual(modes, [True, False, False])
        self.assertEqual(frozen_prompts, [initial, initial])
        self.assertEqual(metadata, initial)
        self.assertEqual(sandbox.call_count, 2)
        self.assertTrue(all(call.kwargs["cases"] == self.cases for call in sandbox.call_args_list))
        boundary = load_pipeline_inputs("pipelines/demo.contract.json")
        self.assertEqual(boundary, {"inputs": self.inputs, "verification_cases": self.cases})
        self.assertEqual(PipelineRunner().route_names(), ["demo"])
        self.assertIn('ctx.set("internal_count"', Path("pipelines/demo.py").read_text())

    def test_inferred_internal_field_cannot_be_filled_to_make_broken_source_pass(self):
        modes = []

        def llm(*_args, **options):
            modes.append(options["json_mode"])
            if options["json_mode"]:
                return copy.deepcopy(self.metadata)
            return encode_source_artifact("pipelines/demo.py", self.bad_source)

        self.assertFalse(self.compose(llm))
        self.assertEqual(modes, [True, False, False, False])
        self.assertEqual(PipelineRunner().route_names(), [])
        self.assertFalse(Path("pipelines/demo.py").exists())

    def test_declared_sidecar_replaces_inferred_public_inputs_in_project_model(self):
        def llm(*_args, **options):
            return copy.deepcopy(self.metadata) if options["json_mode"] else encode_source_artifact("pipelines/demo.py", self.good_source)

        self.assertTrue(self.compose(llm))
        route = PipelineRunner().get_route("demo")
        self.assertEqual(route["input_contract"], "pipelines/demo.contract.json")
        model = build_project_model()
        pipeline = next(item for item in model["pipelines"] if item["name"] == "demo")
        self.assertEqual(pipeline["services"], ["Consumer"])
        self.assertEqual(pipeline["contract"]["inputs"], self.inputs)
        self.assertEqual(pipeline["contract"]["input_source"], "declared")
        self.assertIn("internal_count", pipeline["contract"]["inferred_inputs"])
        self.assertNotIn("internal_count", pipeline["contract"]["inputs"])
        self.assertTrue(model["validation"]["valid"], model["validation"])

    def test_planner_cases_override_model_suggestions_and_reach_real_sandbox_unchanged(self):
        planned_cases = [
            {"name": "first input", "params": {"request": 7}},
            {"name": "second input", "params": {"request": 11}},
        ]
        before = copy.deepcopy(planned_cases)
        weak_metadata = {
            "pipeline_ids": ["Consumer"],
            "inputs": {"internal_count": "int"},
            "verification_cases": [{"name": "synthetic", "params": {"internal_count": 1}}],
        }
        modes = []

        def llm(_system, user, **options):
            modes.append(options["json_mode"])
            if options["json_mode"]:
                return weak_metadata
            frozen = json.loads(user.rsplit("Frozen metadata:\n", 1)[1])
            self.assertEqual(frozen["inputs"], self.inputs)
            self.assertEqual(frozen["verification_cases"], before)
            return encode_source_artifact("pipelines/demo.py", self.good_source)

        with patch("ai_pod_cli.commands.compose.verify_pipeline_candidate", wraps=verify_pipeline_candidate) as sandbox:
            self.assertTrue(self.compose(llm, pipeline_inputs=self.inputs, verification_cases=planned_cases))
        self.assertEqual(modes, [True, False])
        self.assertEqual(sandbox.call_args.kwargs["cases"], before)
        self.assertEqual(planned_cases, before)
        self.assertEqual(weak_metadata["inputs"], {"internal_count": "int"})
        self.assertEqual(load_pipeline_inputs("pipelines/demo.contract.json")["verification_cases"], before)

    def test_reused_pipeline_is_checked_again_with_explicit_planned_inputs(self):
        Path("pipelines/demo.py").write_text(self.good_source, encoding="utf-8")
        register_route("demo", "pipelines/demo.py", "existing route")
        plan = [{"name": "demo", "instruction": "Run existing Consumer",
                 "inputs": self.inputs, "verification_cases": self.cases}]
        with redirect_stdout(io.StringIO()), patch(
            "ai_pod_cli.pod.tools.pipelines.handle_compose",
        ) as compose, patch(
            "ai_pod_cli.pod.tools.pipelines.verify_pipeline_candidate", wraps=verify_pipeline_candidate,
        ) as sandbox:
            result = generate_pipelines(
                pipelines=plan, generated=[], reused=["Consumer"], args=SimpleNamespace(yes=True),
                load_routes=lambda: PipelineRunner().routes(),
            )
        self.assertEqual(result, ([], [], ["demo"]))
        compose.assert_not_called()
        self.assertEqual(sandbox.call_args.kwargs["cases"], self.cases)
        self.assertEqual(load_pipeline_inputs("pipelines/demo.contract.json")["inputs"], self.inputs)
        self.assertEqual(Path("pipelines/demo.py").read_text(), self.good_source)

    def test_reused_broken_pipeline_is_not_marked_complete_or_given_a_sidecar(self):
        Path("pipelines/demo.py").write_text(self.bad_source, encoding="utf-8")
        register_route("demo", "pipelines/demo.py", "existing route")
        plan = [{"name": "demo", "instruction": "Run existing Consumer",
                 "inputs": self.inputs, "verification_cases": self.cases}]
        with redirect_stdout(io.StringIO()), patch("ai_pod_cli.pod.tools.pipelines.handle_compose") as compose:
            result = generate_pipelines(
                pipelines=plan, generated=[], reused=["Consumer"], args=SimpleNamespace(yes=True),
                load_routes=lambda: PipelineRunner().routes(),
            )
        self.assertEqual(result, ([], ["demo"], []))
        compose.assert_not_called()
        self.assertFalse(Path("pipelines/demo.contract.json").exists())
        self.assertEqual(Path("pipelines/demo.py").read_text(), self.bad_source)

    def test_real_description_map_reuses_source_from_registered_path(self):
        source_path = Path("pipelines/existing_consumer.py")
        source_path.write_text(self.good_source, encoding="utf-8")
        description = "读取调用者请求，并返回已计算的内部计数"
        register_route("demo", source_path.as_posix(), description)
        self.assertEqual(load_routes_map(), {"demo": description})
        plan = [{"name": "demo", "instruction": description,
                 "inputs": self.inputs, "verification_cases": self.cases}]
        with redirect_stdout(io.StringIO()), patch("ai_pod_cli.pod.tools.pipelines.handle_compose") as compose:
            result = generate_pipelines(
                pipelines=plan, generated=[], reused=["Consumer"], args=SimpleNamespace(yes=True),
                load_routes=load_routes_map,
            )
        self.assertEqual(result, ([], [], ["demo"]))
        compose.assert_not_called()
        self.assertEqual(source_path.read_text(), self.good_source)
        route = PipelineRunner().get_route("demo")
        self.assertEqual(route["pipeline"], source_path.as_posix())
        self.assertEqual(route["input_contract"], "pipelines/existing_consumer.contract.json")
        self.assertEqual(load_pipeline_inputs(route["input_contract"]), {
            "inputs": self.inputs, "verification_cases": self.cases,
        })

    def test_json_sidecar_retains_nullable_inputs_and_explicit_null_scenarios(self):
        inputs = {"filter": {"type": "Optional[str]", "required": False, "default": None}}
        cases = [{"name": "omitted", "params": {}}, {"name": "null", "params": {"filter": None}}]
        self.assertEqual(validate_pipeline_inputs(inputs, cases), [])
        path = save_pipeline_inputs("pipelines/demo.py", inputs, cases)
        self.assertEqual(load_pipeline_inputs(path), {"inputs": inputs, "verification_cases": cases})

    def test_boundary_rejects_case_names_that_sandbox_considers_duplicates(self):
        errors = validate_pipeline_inputs({}, [
            {"name": "duplicate", "params": {}},
            {"name": " duplicate ", "params": {}},
        ])
        self.assertTrue(errors, "Do not freeze cases that the sandbox cannot execute")


if __name__ == "__main__":
    unittest.main()
