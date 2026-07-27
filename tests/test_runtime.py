"""End-to-end tests for the deterministic AIPod runtime."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_pod_cli.config import init_config_if_not_exists
from ai_pod_cli.commands.visualize import _extract_pipeline_services, _graph_html
from ai_pod_cli.runner import PipelineRunner
from ai_pod_cli.validation import (
    repair_feedback,
    request_repair,
    validate_component_contract,
    validate_pipeline_contract,
)


class RuntimeIntegrationTests(unittest.TestCase):
    def test_registered_services_execute_through_pipeline_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            previous_path = list(sys.path)
            try:
                os.chdir(project)
                sys.path.insert(0, str(project))
                init_config_if_not_exists()
                (project / "modules" / "services").mkdir(parents=True)
                (project / "modules" / "__init__.py").write_text("")
                (project / "modules" / "services" / "__init__.py").write_text("")
                (project / "modules" / "services" / "increment.py").write_text(
                    "from ai_pod_cli.context import PipelineContext\n"
                    "class Increment:\n"
                    "    def execute(self, ctx: PipelineContext):\n"
                    "        ctx.set('value', ctx.params['value'] + 1)\n"
                    "        return {'status': 'incremented'}\n"
                )
                (project / "pipelines").mkdir()
                (project / "pipelines" / "increment.py").write_text(
                    "from ai_pod_cli.config import load_beans\n"
                    "from ai_pod_cli.container import Pod, build_container\n"
                    "from modules.services.increment import Increment\n"
                    "def run(ctx):\n"
                    "    (Pod(build_container(load_beans()))(Increment)).execute_all(ctx)\n"
                    "    return ctx.summary()\n"
                )
                registry = json.loads((project / "beans_config.json").read_text())
                registry["beans"].append({
                    "id": "Increment",
                    "category": "service",
                    "class_path": "modules.services.increment.Increment",
                })
                (project / "beans_config.json").write_text(json.dumps(registry))
                (project / "routes.toml").write_text('[increment]\npipeline = "pipelines/increment.py"\n')

                result = PipelineRunner().run("increment", {"value": 41})
                self.assertEqual(result["data"]["value"], 42)
                self.assertEqual(result["steps"][0]["component"], "Increment")
            finally:
                os.chdir(previous_cwd)
                sys.path[:] = previous_path
                for module_name in list(sys.modules):
                    if module_name == "modules" or module_name.startswith("modules."):
                        sys.modules.pop(module_name)


class GeneratedArtifactValidationTests(unittest.TestCase):
    def test_service_requires_execute(self):
        self.assertTrue(validate_component_contract("class MissingExecute: pass", "MissingExecute", "service"))

    def test_pipeline_requires_run(self):
        self.assertTrue(validate_pipeline_contract("def other(): pass"))

    def test_repair_feedback_is_explicit_and_requires_confirmation(self):
        violations = ["Pipeline 必须定义 run(ctx) 函数"]
        self.assertIn(violations[0], repair_feedback(violations))
        with patch("builtins.input", return_value="n"):
            self.assertFalse(request_repair(violations, 1, 3))
        with patch("builtins.input", return_value=""):
            self.assertTrue(request_repair(violations, 1, 3))


class VisualizationTests(unittest.TestCase):
    def test_extracts_services_without_importing_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Path(tmp) / "flow.py"
            pipeline.write_text(
                "def run(ctx):\n"
                "    (S(First) | S(Second)).execute_all(ctx)\n"
            )
            self.assertEqual(
                _extract_pipeline_services(str(pipeline), {"First": "First", "Second": "Second"}),
                ["First", "Second"],
            )

    def test_graph_html_contains_project_data(self):
        page = _graph_html(
            [{"id": "Worker", "category": "service", "class_path": "modules.worker.Worker"}],
            [{"name": "run_work", "pipeline": "pipelines/work.py", "services": ["Worker"]}],
        )
        self.assertIn("AIPod 项目图谱", page)
        self.assertIn("run_work", page)
        self.assertIn("Worker", page)


if __name__ == "__main__":
    unittest.main()
