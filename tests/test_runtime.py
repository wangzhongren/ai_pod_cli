"""End-to-end tests for the deterministic AIPod runtime."""

import json
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_pod_cli.config import init_config_if_not_exists, save_config
from ai_pod_cli.contracts import analyze_pipeline_contracts, normalize_type, types_compatible
from ai_pod_cli.commands.visualize import _extract_pipeline_services, _graph_html
from ai_pod_cli.commands.pod import handle_pod
from ai_pod_cli.agent_output import execute_json_command
from ai_pod_cli.project_model import inspect_project
from ai_pod_cli.runner import PipelineRunner
from ai_pod_cli.result import Effect, Failure, Success
from ai_pod_cli.run_store import get_run_trace, list_run_traces, write_run_trace
from ai_pod_cli.studio import StudioApi, StudioError, studio_asset_path
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
                self.assertIn("duration_ms", result["steps"][0])
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

    def test_pipeline_rejects_invalid_component_ref_calls_and_process_exit(self):
        invalid = """
def run(ctx):
    service = S(Worker)
    service.execute(value=1)
    sys.exit(1)
"""
        violations = validate_pipeline_contract(invalid)
        self.assertTrue(any("PipelineContext" in item for item in violations))
        self.assertTrue(any("sys.exit" in item for item in violations))

    def test_component_dict_outputs_are_merged_into_pipeline_state(self):
        from ai_pod_cli.container import _ComponentRef
        from ai_pod_cli.context import PipelineContext

        component = type("Component", (), {"execute": lambda self, ctx: {"answer": 42}})()
        ctx = PipelineContext()
        _ComponentRef("Component", component).execute_all(ctx)

        self.assertEqual(ctx.get("answer"), 42)
        self.assertEqual(ctx.steps[0]["result"], {"answer": 42})

    def test_structured_success_merges_output_and_records_effects(self):
        from ai_pod_cli.container import _ComponentRef
        from ai_pod_cli.context import PipelineContext

        component = type("Writer", (), {"execute": lambda self, ctx: Success(
            {"saved": True}, effects=(Effect("database", "expenses", "insert"),)
        )})()
        ctx = PipelineContext()

        result = _ComponentRef("Writer", component).execute_all(ctx)

        self.assertTrue(result.ok)
        self.assertTrue(ctx.get("saved"))
        self.assertEqual(ctx.steps[0]["result"]["effects"][0]["kind"], "database")
        self.assertEqual(ctx.steps[0]["status"], "success")

    def test_failure_stops_the_remaining_pipeline(self):
        from ai_pod_cli.container import _ComponentRef, _PipeChain
        from ai_pod_cli.context import PipelineContext

        calls = []
        failing = type("Failing", (), {"execute": lambda self, ctx: Failure("not available")})()
        later = type("Later", (), {"execute": lambda self, ctx: calls.append("later") or {"ok": True}})()
        ctx = PipelineContext()

        result = _PipeChain([
            _ComponentRef("Failing", failing),
            _ComponentRef("Later", later),
        ]).execute_all(ctx)

        self.assertIsInstance(result, Failure)
        self.assertEqual(calls, [])
        self.assertEqual(ctx.steps[0]["status"], "failure")

    def test_retry_recovers_from_transient_exception(self):
        from ai_pod_cli.container import _ComponentRef
        from ai_pod_cli.context import PipelineContext

        class Flaky:
            def __init__(self):
                self.calls = 0

            def execute(self, ctx):
                self.calls += 1
                if self.calls < 3:
                    raise ConnectionError("temporary")
                return {"connected": True}

        ctx = PipelineContext()
        component = Flaky()
        result = _ComponentRef("Flaky", component).retry(2).execute_all(ctx)

        self.assertEqual(result, {"connected": True})
        self.assertEqual(component.calls, 3)
        self.assertEqual(ctx.steps[0]["attempts"], 3)

    def test_fallback_handles_explicit_failure(self):
        from ai_pod_cli.container import _ComponentRef
        from ai_pod_cli.context import PipelineContext

        primary = type("Primary", (), {"execute": lambda self, ctx: Failure("offline")})()
        cache = type("Cache", (), {"execute": lambda self, ctx: {"source": "cache"}})()
        ctx = PipelineContext()

        result = _ComponentRef("Primary", primary).fallback(
            _ComponentRef("Cache", cache)
        ).execute_all(ctx)

        self.assertEqual(result, {"source": "cache"})
        self.assertEqual(ctx.get("source"), "cache")
        self.assertEqual(ctx.steps[0]["fallback"], "Cache")

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


class AgentProjectModelTests(unittest.TestCase):
    def test_inspect_returns_compact_machine_readable_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                init_config_if_not_exists()
                summary = inspect_project(summary_only=True)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(summary["schema_version"], "1.0")
        self.assertEqual(summary["summary"]["component_count"], 2)
        self.assertTrue(summary["validation"]["valid"])

    def test_json_command_envelope_reports_real_project_changes(self):
        class Args:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                init_config_if_not_exists()
                args = Args()
                output = io.StringIO()
                with redirect_stdout(output):
                    execute_json_command("test", lambda _args: None, args)
            finally:
                os.chdir(previous_cwd)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "no_change")
        self.assertEqual(payload["changes"]["components"]["added"], [])


class RunTraceTests(unittest.TestCase):
    def test_trace_is_persisted_and_redacts_sensitive_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                trace = write_run_trace(
                    "demo",
                    {"api_key": "secret-value", "count": 2},
                    {"data": {"token": "result-secret", "ok": True}},
                    None,
                    12.3456,
                    "2026-07-27T00:00:00+00:00",
                )
                loaded = get_run_trace(trace["run_id"])
                listed = list_run_traces()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(trace["params"]["api_key"], "***")
        self.assertEqual(loaded["result"]["data"]["token"], "***")
        self.assertEqual(listed[0]["run_id"], trace["run_id"])

    def test_structured_failure_creates_a_failed_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                trace = write_run_trace(
                    "demo", {}, Failure("queue unavailable", code="queue_down").to_dict(),
                    None, 1.0,
                )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(trace["status"], "failed")
        self.assertEqual(trace["error"]["code"], "queue_down")
        self.assertEqual(trace["error"]["type"], "Failure")


class StudioApiTests(unittest.TestCase):
    def test_studio_inspects_project_without_leaking_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            project = Path(tmp)
            os.chdir(project)
            try:
                init_config_if_not_exists()
            finally:
                os.chdir(previous_cwd)

            result = StudioApi(project).inspect_project()
            source = StudioApi(project).read_component_source("ConfigStore")
            settings = StudioApi(project).get_settings()
            invalid_run = StudioApi(project).run_pipeline("missing", "[]")

        self.assertTrue(result["ok"])
        self.assertTrue(source["ok"])
        self.assertIn("class ConfigStore", source["source"])
        self.assertTrue(settings["ok"])
        self.assertNotIn("api_key", settings["settings"])
        self.assertFalse(invalid_run["ok"])
        self.assertIn("JSON", invalid_run["error"]["message"])
        self.assertEqual(result["project"]["summary"]["component_count"], 2)
        self.assertEqual(Path.cwd(), previous_cwd)

    def test_studio_rejects_non_aipod_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(StudioError):
                StudioApi(tmp)

    def test_studio_switches_between_initialized_projects(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            previous_cwd = Path.cwd()
            for root in (first, second):
                os.chdir(root)
                init_config_if_not_exists()
            os.chdir(previous_cwd)
            api = StudioApi(first)

            result = api.open_project(second)

            self.assertTrue(result["ok"])
            self.assertEqual(Path(result["project"]["project_root"]), Path(second).resolve())
            self.assertEqual(api.project_root, Path(second).resolve())

    def test_studio_runs_project_entry_and_captures_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
            finally:
                os.chdir(previous_cwd)
            (project / "main.py").write_text("print('hello from studio')\n", encoding="utf-8")
            api = StudioApi(project)

            started = api.start_program_request({"entry": "main.py", "arguments": ""})
            for _ in range(30):
                status = api.program_status()
                if not status["running"]:
                    break
                time.sleep(0.02)

            self.assertTrue(started["ok"])
            self.assertFalse(status["running"])
            self.assertIn("hello from studio", "\n".join(status["output"]))
            self.assertIn("main.py", api.inspect_project()["project"]["entrypoints"])

    def test_studio_frontend_is_packaged(self):
        page = studio_asset_path().read_text(encoding="utf-8")
        self.assertIn("AIPod Studio", page)
        self.assertIn("pywebview.api", page)
        self.assertIn("Build a Pod with AI", page)
        self.assertIn("Run pipeline", page)
        self.assertIn("pod_build_status", page)
        self.assertIn("podProgressBar", page)

    def test_studio_pod_build_runs_in_background_with_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
            finally:
                os.chdir(previous_cwd)
            api = StudioApi(project)

            def fake_build(_description, progress, _cancelled):
                progress("📋 [拆解方案] test")
                progress("🤖 [1/2] 生成 Repository (provider)...")
                progress("🤖 [2/2] 生成 Service (service)...")
                return {"ok": True, "project": {"summary": {}}, "changes": {}}

            with patch.object(api, "build_pod", side_effect=fake_build):
                started = api.start_pod_build("build a test")
                for _ in range(100):
                    task = api.pod_build_status(started["build_id"])["task"]
                    if task["status"] != "running":
                        break
                    time.sleep(0.005)

            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["percent"], 100)
            self.assertTrue(any("Repository" in line for line in task["logs"]))
            self.assertTrue(task["result"]["ok"])

    def test_studio_pod_build_supports_cooperative_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
            finally:
                os.chdir(previous_cwd)
            api = StudioApi(project)

            def wait_for_cancel(_description, _progress, cancelled):
                for _ in range(200):
                    if cancelled():
                        return {"ok": True}
                    time.sleep(0.002)
                return {"ok": False, "error": {"message": "not cancelled"}}

            with patch.object(api, "build_pod", side_effect=wait_for_cancel):
                started = api.start_pod_build("cancel me")
                cancelled = api.cancel_pod_build(started["build_id"])
                for _ in range(100):
                    task = api.pod_build_status(started["build_id"])["task"]
                    if task["status"] == "cancelled":
                        break
                    time.sleep(0.005)

            self.assertTrue(cancelled["ok"])
            self.assertEqual(task["status"], "cancelled")

    def test_studio_imports_complete_manual_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
            finally:
                os.chdir(previous_cwd)

            source = project / "modules" / "services" / "ordervalidator.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                "class OrderValidator:\n"
                "    def execute(self, ctx):\n"
                "        return {'status': 'success'}\n",
                encoding="utf-8",
            )

            result = StudioApi(project).create_component({
                "mode": "manual",
                "name": "OrderValidator",
                "category": "service",
                "class_path": "modules.services.ordervalidator.OrderValidator",
                "description": "Validate an order before processing",
                "dependencies": "ConfigStore",
                "inputs": '{"order_id": "str"}',
                "outputs": '{"valid": "bool"}',
                "methods": "{}",
            })

            self.assertTrue(result["ok"])
            self.assertTrue(source.exists())
            self.assertIn(
                "OrderValidator",
                {item["id"] for item in result["project"]["components"]},
            )

    def test_contract_types_support_legacy_descriptions(self):
        self.assertEqual(normalize_type("str — order identifier"), "str")
        self.assertEqual(normalize_type({"type": "boolean"}), "bool")
        self.assertTrue(types_compatible("int", "float"))
        self.assertFalse(types_compatible("str", "bool"))

    def test_pipeline_contract_infers_inputs_outputs_and_type_errors(self):
        components = [
            {"id": "Load", "inputs": {"query": "str"}, "outputs": {"count": "int"}},
            {"id": "Format", "inputs": {"count": "str"}, "outputs": {"text": "str"}},
        ]
        contract = analyze_pipeline_contracts(["Load", "Format"], components)

        self.assertEqual(contract["inputs"]["query"]["type"], "str")
        self.assertEqual(contract["outputs"]["text"]["type"], "str")
        self.assertFalse(contract["valid"])
        self.assertEqual(contract["links"][0]["mismatches"][0]["field"], "count")

    def test_studio_composes_and_registers_visual_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
                save_config({"beans": [
                    {"id": "Load", "category": "service", "class_path": "modules.load.Load", "inputs": {"query": "str"}, "outputs": {"rows": "list"}},
                    {"id": "Format", "category": "service", "class_path": "modules.format.Format", "inputs": {"rows": "list"}, "outputs": {"text": "str"}},
                ]})
            finally:
                os.chdir(previous_cwd)

            result = StudioApi(project).compose_pipeline({
                "name": "search_report",
                "description": "Load and format results",
                "services": ["Load", "Format"],
            })

            self.assertTrue(result["ok"])
            pipeline = (project / "pipelines" / "search_report.py").read_text(encoding="utf-8")
            self.assertIn("S(Load) | S(Format)", pipeline)
            self.assertIn("[search_report]", (project / "routes.toml").read_text(encoding="utf-8"))
            self.assertTrue(result["contract"]["valid"])

    def test_studio_initializes_and_opens_an_ordinary_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current"
            ordinary = root / "ordinary"
            current.mkdir()
            ordinary.mkdir()
            previous_cwd = Path.cwd()
            os.chdir(current)
            try:
                init_config_if_not_exists()
            finally:
                os.chdir(previous_cwd)

            result = StudioApi(current).initialize_project(str(ordinary))

            self.assertTrue(result["ok"])
            self.assertTrue(result["initialized"])
            self.assertEqual(Path(result["project"]["project_root"]), ordinary)
            self.assertTrue((ordinary / "beans_config.json").exists())
            self.assertTrue((ordinary / "modules" / "services" / "__init__.py").exists())
            self.assertTrue((ordinary / "pipelines" / "__init__.py").exists())

    def test_pod_reuses_existing_component_and_pipeline_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
                config = json.loads((project / "beans_config.json").read_text(encoding="utf-8"))
                config["beans"].append({
                    "id": "ExistingService", "category": "service",
                    "class_path": "modules.services.existing.ExistingService",
                    "inputs": {}, "outputs": {"ok": "bool"},
                })
                save_config(config)
                (project / "routes.toml").write_text(
                    '[existing_route]\npipeline = "pipelines/existing_route.py"\n', encoding="utf-8"
                )
                plan = {
                    "pod_name": "reuse_test",
                    "reuse_components": ["ExistingService"],
                    "components": [{"name": "ExistingService", "category": "service", "description": "same"}],
                    "pipelines": [{"name": "existing_route", "instruction": "reuse it"}],
                    "config_additions": {},
                }
                args = type("Args", (), {"desc": "reuse", "file": "", "yes": True, "json": True})()
                output = io.StringIO()
                with (
                    patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
                    patch("ai_pod_cli.commands.pod.call_llm", return_value=plan) as llm,
                    redirect_stdout(output),
                ):
                    handle_pod(args)
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(llm.call_count, 1)
            self.assertIn("ExistingService (reuse)", output.getvalue())
            self.assertIn("[Pipeline 复用] existing_route", output.getvalue())

    def test_studio_discovers_cli_interface_and_connected_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
            finally:
                os.chdir(previous_cwd)
            (project / "pipelines").mkdir()
            (project / "pipelines" / "work.py").write_text("def run(ctx): return ctx.summary()\n", encoding="utf-8")
            (project / "routes.toml").write_text('[work]\npipeline = "pipelines/work.py"\n', encoding="utf-8")
            (project / "main.py").write_text(
                "import argparse\nfrom ai_pod_cli.runner import PipelineRunner\n"
                "route = 'work'\nPipelineRunner().run(route, {})\n",
                encoding="utf-8",
            )
            (project / "server.py").write_text(
                "from http.server import HTTPServer\nPAGE='<!doctype html>'\nCONTENT='text/html'\n",
                encoding="utf-8",
            )
            (project / "desktop.py").write_text("import webview\n", encoding="utf-8")

            result = StudioApi(project).inspect_project()

            self.assertTrue(result["ok"])
            interfaces = {item["name"]: item for item in result["project"]["interfaces"]}
            self.assertEqual(interfaces["main.py"]["kind"], "cli")
            self.assertEqual(interfaces["main.py"]["routes"], ["work"])
            self.assertEqual(interfaces["server.py"]["kind"], "web")
            self.assertEqual(interfaces["desktop.py"]["kind"], "desktop")


if __name__ == "__main__":
    unittest.main()
