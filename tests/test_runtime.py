"""End-to-end tests for the deterministic AIPod runtime."""

import json
import io
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_pod_cli.config import extract_model_fields, extract_sql_resources, init_config_if_not_exists, save_config
from ai_pod_cli.client import _parse_json_content, call_llm
from ai_pod_cli.contracts import (
    analyze_pipeline_contracts, normalize_type, semantic_field_similarity, types_compatible,
    validate_contract_data,
)
from ai_pod_cli.commands.visualize import _extract_pipeline_services, _graph_html
from ai_pod_cli.commands.pod import (
    _load_decision_plan, _resume_stage, _save_decision_plan, _set_stage_status, handle_pod,
)
from ai_pod_cli.commands.verify import _bounded_output, _project_traceback_locations, verify_project
from ai_pod_cli.cli import _apply_global_env
from ai_pod_cli.commands.env import print_missing_model_config, record_global_config_load_error
from ai_pod_cli.sandbox import (
    materialize_path_fixtures, sample_value, verify_component_candidate,
)
from ai_pod_cli.decision import reduce_decision_fragments, reduce_evidence
from ai_pod_cli.agent_output import execute_json_command
from ai_pod_cli.project_model import inspect_project
from ai_pod_cli.runner import PipelineRunner
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.model import Model
from ai_pod_cli.repository import ModelRepository
from ai_pod_cli.config_store import ConfigStore
from sqlmodel import Field
from ai_pod_cli.result import Effect, Failure, Success
from ai_pod_cli.repair import apply_code_patches, classify_failures
from ai_pod_cli.run_store import get_run_trace, list_run_traces, write_run_trace
from ai_pod_cli.studio import StudioApi, StudioError, _ProgressCapture, studio_asset_path
from ai_pod_cli.validation import (
    extract_component_fields,
    repair_feedback,
    request_repair,
    validate_component_contract,
    validate_entry_contract,
    validate_pipeline_contract,
)


class TestShipment(Model):
    id: str
    distance_km: float


class TestShipmentSnapshot(Model):
    shipments: list[TestShipment]


class OtherShipmentSnapshot(Model):
    shipments: list[TestShipment]


class TimedSample(Model):
    timestamp: datetime


class DatedSample(Model):
    day: date


class VectorValue(Model):
    x: float
    y: float


class OptionalValue(Model):
    label: str | None


class RepositoryItem(Model, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str


class RuntimeIntegrationTests(unittest.TestCase):
    def test_global_config_permission_error_is_not_reported_as_missing_key(self):
        output = io.StringIO()
        try:
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("ai_pod_cli.commands.env.get_global_env", side_effect=PermissionError("denied")),
                redirect_stdout(output),
            ):
                _apply_global_env()
                print_missing_model_config()
        finally:
            record_global_config_load_error(None)

        message = output.getvalue()
        self.assertIn("无法读取 AIPod 全局配置", message)
        self.assertIn("PermissionError", message)
        self.assertIn("无需重新设置", message)
        self.assertNotIn("sk-your-key", message)

    def test_successful_global_config_load_clears_previous_error(self):
        output = io.StringIO()
        record_global_config_load_error(PermissionError("old"))
        try:
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("ai_pod_cli.commands.env.get_global_env", return_value={"OPENAI_API_KEY": "secret"}),
                redirect_stdout(output),
            ):
                _apply_global_env()
                self.assertEqual(os.environ["OPENAI_API_KEY"], "secret")
                print_missing_model_config()
        finally:
            record_global_config_load_error(None)

        self.assertIn("OPENAI_API_KEY 未配置", output.getvalue())
        self.assertNotIn("无法读取 AIPod 全局配置", output.getvalue())

    def test_verify_reports_real_failure_and_project_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                init_config_if_not_exists()
                script = Path("broken_check.py")
                script.write_text(
                    "raise RuntimeError('integration failed')\n", encoding="utf-8",
                )
                result = verify_project([sys.executable, str(script)], timeout=10)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["checks"]["execution"]["exit_code"], 1)
                self.assertEqual(result["repair"]["suggested_files"], ["broken_check.py"])
            finally:
                os.chdir(previous_cwd)

    def test_verify_ignores_traceback_files_outside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            output = f'  File "{root / "inside.py"}", line 7\n  File "C:/outside.py", line 2'
            self.assertEqual(
                _project_traceback_locations(output, root),
                [{"file": "inside.py", "line": 7}],
            )

    def test_verify_redacts_common_credentials(self):
        output = _bounded_output(
            'Authorization: Bearer abc.def-123 token=sk-exampleSECRET123 '
            'api_key="custom-provider-secret"'
        )
        self.assertNotIn("abc.def-123", output)
        self.assertNotIn("sk-exampleSECRET123", output)
        self.assertNotIn("custom-provider-secret", output)

    def test_model_sample_prefers_none_for_optional_fields(self):
        self.assertIsNone(OptionalValue.sample_instance().label)

    def test_sandbox_materializes_only_safe_synthetic_input_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            materialize_path_fixtures(
                {"input_path": "sample.log", "output_path": "report.json"}, tmp,
            )
            content = (Path(tmp) / "sample.log").read_text(encoding="utf-8")
            self.assertIn("latency_ms=42", content)
            self.assertFalse((Path(tmp) / "report.json").exists())

    def test_plan_reducer_detects_unknown_dependency_and_cycle(self):
        plan = {"components": [
            {"name": "A", "category": "service", "depends_on": ["B"]},
            {"name": "B", "category": "service", "depends_on": ["A", "Missing"]},
        ]}
        result = reduce_decision_fragments(plan, [], "services")
        codes = {item["code"] for item in result["conflicts"]}
        self.assertIn("UNKNOWN_DEPENDENCY", codes)
        self.assertIn("DEPENDENCY_CYCLE", codes)

    def test_plan_mapper_normalizes_legacy_model_dependencies(self):
        plan = {"components": [{
            "name": "MoveService", "category": "service",
            "depends_on": ["Transform", "InputProvider"],
        }]}
        existing = [
            {"id": "Transform", "category": "model"},
            {"id": "InputProvider", "category": "provider"},
        ]
        result = reduce_decision_fragments(plan, existing, "services")
        self.assertEqual(result["status"], "accepted")
        fragment = result["fragments"][0]
        self.assertEqual(fragment["dependencies"], ["InputProvider"])
        self.assertEqual(fragment["models"], ["Transform"])

    def test_evidence_reducer_never_expands_repair_scope(self):
        self.assertEqual(reduce_evidence([])["status"], "accepted")
        reduced = reduce_evidence(["runtime failed", "runtime failed"])
        self.assertEqual(reduced["status"], "repair_current")
        self.assertEqual(reduced["repair_scope"], "current_candidate")
        self.assertEqual(reduced["evidence"], ["runtime failed"])

    def test_canonical_plan_resumes_without_losing_frozen_stage_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                state = _load_decision_plan("build game")
                state["stages"]["models"]["plan"] = {"pod_name": "models", "components": []}
                _save_decision_plan(state)
                loaded = _load_decision_plan("build game")
                self.assertEqual(loaded["stages"]["models"]["plan"]["pod_name"], "models")
                self.assertEqual(_resume_stage(loaded), 0)
                _set_stage_status(loaded, 0, "complete")
                self.assertEqual(_resume_stage(_load_decision_plan("build game")), 1)
            finally:
                os.chdir(previous_cwd)

    def test_value_model_does_not_require_database_table(self):
        value = VectorValue(x=1, y=2)
        self.assertEqual(value.model_dump(), {"x": 1.0, "y": 2.0})
        self.assertEqual(
            validate_component_contract(
                "from ai_pod_cli import Model\nclass VectorValue(Model):\n    x: float\n    y: float\n",
                "VectorValue", "model",
            ),
            [],
        )

    def test_model_sample_supports_date(self):
        self.assertEqual(DatedSample.sample_instance().day, date(2024, 1, 1))

    def test_sqlmodel_repository_initializes_saves_and_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            database_path = (Path(tmp) / "repo.db").as_posix()
            config_path.write_text(f'[database]\nurl = "sqlite:///{database_path}"\n', encoding="utf-8")
            repository = ModelRepository(ConfigStore(str(config_path)))
            repository.load_models = lambda package_name="modules.models": None
            saved = repository.save(RepositoryItem(name="alpha"))
            self.assertIsNotNone(saved.id)
            self.assertEqual(repository.get(RepositoryItem, saved.id).name, "alpha")
            self.assertEqual(repository.find(RepositoryItem, name="alpha")[0].id, saved.id)
            repository.close()

    def test_model_fields_are_extracted_for_registry_contract(self):
        fields = extract_model_fields(
            "from dataclasses import dataclass\n@dataclass\nclass Audit(Model):\n    details: str\n    tags: list[str]\n",
            "Audit",
        )
        self.assertEqual(fields, {"details": "str", "tags": "list[str]"})

    def test_provider_sql_resources_are_machine_readable(self):
        resources = extract_sql_resources(
            "sql = '''CREATE TABLE IF NOT EXISTS audit_log (log_id TEXT PRIMARY KEY, details TEXT)'''"
        )
        self.assertEqual(resources["tables"]["audit_log"]["columns"]["details"], "TEXT")

    def test_scalar_contract_accepts_parenthesized_format_qualifier(self):
        self.assertEqual(normalize_type("str (ISO8601)"), "str")
        self.assertEqual(normalize_type("string (ISO8601 datetime)"), "str")
        self.assertEqual(validate_contract_data(
            {"at": datetime(2024, 1, 1)}, {"at": "datetime"},
        ), [])
        self.assertIsInstance(sample_value("period_start", "str (ISO8601)"), str)
        self.assertIsInstance(sample_value("timestamp", "datetime"), datetime)
        self.assertEqual(sample_value("window_minutes", "any"), 1)

    def test_constrained_repair_preserves_candidate_and_class(self):
        code = "from datetime import datetime\nclass Clock:\n    def now(self):\n        return datetime.timezone.utc\n"
        repaired = apply_code_patches(
            code,
            [
                {"old": "from datetime import datetime", "new": "from datetime import datetime, timezone"},
                {"old": "datetime.timezone.utc", "new": "timezone.utc"},
            ],
            "Clock", "import",
        )
        self.assertIn("class Clock", repaired)
        self.assertIn("timezone.utc", repaired)
        self.assertNotIn("datetime.timezone.utc", repaired)

    def test_constrained_repair_rejects_whole_candidate_rewrite(self):
        code = "class Worker:\n    def run(self):\n        return 'stable'\n"
        with self.assertRaisesRegex(ValueError, "补丁范围过大"):
            apply_code_patches(
                code, [{"old": code, "new": "class Worker:\n    pass\n"}],
                "Worker", "runtime",
            )

    def test_failure_classifier_routes_contract_away_from_code_patch(self):
        self.assertEqual(classify_failures(["outputs.batch schema validation failed"]), "contract")
        self.assertEqual(classify_failures(["ModuleNotFoundError: no module x"]), "import")

    def test_model_sample_instance_preserves_nested_model_types(self):
        snapshot = TestShipmentSnapshot.sample_instance()
        self.assertIsInstance(snapshot, TestShipmentSnapshot)
        self.assertIsInstance(snapshot.shipments[0], TestShipment)
        self.assertIsInstance(TimedSample.sample_instance().timestamp, datetime)

    def test_incomplete_stream_retries_as_non_streaming_response(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if kwargs.get("stream"):
                return [SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content='{"answer":'), finish_reason=None,
                )])]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"answer": 42}'), finish_reason="stop",
            )], usage=None)

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch("ai_pod_cli.client.get_client", return_value=fake_client):
            result = call_llm(
                "system", "user", json_mode=True, max_retries=2, retry_delay=0,
                progress_callback=lambda event: None,
            )
        self.assertEqual(result, {"answer": 42})
        self.assertTrue(calls[0]["stream"])
        self.assertNotIn("stream", calls[1])

    def test_length_finish_reason_increases_output_limit(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return [SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content='{"answer":'), finish_reason="length",
                )])]
            return [SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content='{"answer": 42}'), finish_reason="stop",
            )])]

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch("ai_pod_cli.client.get_client", return_value=fake_client):
            result = call_llm(
                "system", "user", json_mode=True, max_tokens=100,
                max_retries=2, retry_delay=0, progress_callback=lambda event: None,
            )
        self.assertEqual(result, {"answer": 42})
        self.assertEqual(calls[0]["max_tokens"], 100)
        self.assertEqual(calls[1]["max_tokens"], 200)

    def test_llm_streaming_accumulates_json_and_reports_progress(self):
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='{"answer"'))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=': 42}'))]),
        ]
        completions = SimpleNamespace(create=lambda **kwargs: chunks)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        events = []
        with patch("ai_pod_cli.client.get_client", return_value=fake_client), patch(
            "ai_pod_cli.client.get_model", return_value="test-model"
        ):
            result = call_llm(
                "system", "user", json_mode=True, max_retries=1,
                progress_callback=events.append, progress_label="Planning",
            )
        self.assertEqual(result, {"answer": 42})
        self.assertEqual(events[0]["type"], "llm_started")
        self.assertEqual(events[-1], {"type": "llm_completed", "label": "Planning", "characters": 14})

    def test_json_parser_accepts_markdown_wrapped_proxy_output(self):
        self.assertEqual(
            _parse_json_content("Here is the result:\n```json\n{\"ok\": true}\n```"),
            {"ok": True},
        )

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

    def test_sandbox_sample_uses_contract_enum_value(self):
        self.assertEqual(
            sample_value("movement_type", "str — one of 'IN' | 'OUT' | 'ADJUST'"),
            "IN",
        )
        self.assertEqual(sample_value("status", {"type": "string", "enum": ["open", "closed"]}), "open")
        self.assertEqual(sample_value("background_color", "str"), "#ffffff")

    def test_model_repository_find_accepts_filter_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "find.db"
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                f'[database]\nurl = "sqlite:///{db_path.as_posix()}"\n', encoding="utf-8",
            )
            repository = ModelRepository(ConfigStore(config_path))
            repository.load_models = lambda package_name="modules.models": None
            repository.save(RepositoryItem(name="sample"))
            rows = repository.find(RepositoryItem, {"name": "sample"})
            self.assertEqual(len(rows), 1)
            repository.close()
    def test_sandbox_samples_follow_contract_types(self):
        self.assertEqual(sample_value("count", "int — items"), 1)
        self.assertEqual(sample_value("tick", "int (optional) — override"), 1)
        self.assertEqual(sample_value("ratio", {"type": "float"}), 1.0)
        self.assertEqual(sample_value("incident_type", "str"), "FIRE")
        self.assertEqual(sample_value("payload", "dict"), {})

    def test_context_get_falls_back_to_entry_params(self):
        ctx = PipelineContext({"risk_report": {"level": "high"}})
        self.assertEqual(ctx.get("risk_report"), {"level": "high"})
        ctx.set("risk_report", {"level": "low"})
        self.assertEqual(ctx.get("risk_report"), {"level": "low"})

    def test_entry_rejects_unknown_frozen_route(self):
        code = '''
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import build_container
container = build_container(load_beans())
runner.run("renamed_route", {})
'''
        violations = validate_entry_contract(code, ["stable_route"])
        self.assertTrue(any("renamed_route" in item for item in violations))

    def test_component_fields_are_extracted_only_from_context_boundary(self):
        code = '''
class LifeSupport:
    def execute(self, ctx):
        params = ctx.params
        state = self.repo.get_state()
        action = params.get("action")
        oxygen = state.get("oxygen_level")
        ctx.set("risk", "critical")
        updates = {}
        updates["emergency_mode"] = True
        self.repo.update_state(updates)
        return {"status": "ok"}
'''
        actual = extract_component_fields(code)
        self.assertEqual(actual["reads"], ["action"])
        self.assertEqual(actual["writes"], ["risk"])

    def test_component_contract_rejects_declared_actual_field_drift(self):
        code = '''
class LifeSupport:
    def execute(self, ctx):
        oxygen = ctx.get("oxygen")
        ctx.set("risk", "critical")
        return {"status": "ok"}
'''
        violations = validate_component_contract(
            code, "LifeSupport", "service",
            {"oxygen_level": "float"}, {"risk": "str"},
        )
        self.assertTrue(any("oxygen" in item and "inputs" in item for item in violations))

    def test_component_rejects_nonexistent_context_output_api(self):
        code = '''
class Worker:
    def execute(self, ctx):
        ctx.output["value"] = 1
        return {"value": 1}
'''
        violations = validate_component_contract(
            code, "Worker", "service", {}, {"value": "int"},
        )
        self.assertTrue(any("ctx.output" in item for item in violations))

    def test_service_requires_execute(self):
        self.assertTrue(validate_component_contract("class MissingExecute: pass", "MissingExecute", "service"))

    def test_component_rejects_noncanonical_config_store_import(self):
        code = (
            "from modules.providers.config_store import ConfigStore\n"
            "class Worker:\n"
            "    def execute(self, ctx): return {}\n"
        )
        violations = validate_component_contract(code, "Worker", "service")
        self.assertTrue(any("ai_pod_cli.config_store" in item for item in violations))

    def test_component_rejects_noncanonical_repository_import(self):
        code = (
            "from modules.providers.repository import ModelRepository\n"
            "class Worker:\n"
            "    def execute(self, ctx):\n"
            "        return {}\n"
        )
        violations = validate_component_contract(code, "Worker", "service", {}, {})
        self.assertTrue(any("ai_pod_cli.repository" in item for item in violations))

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
        feedback = repair_feedback(violations)
        self.assertIn(violations[0], feedback)
        self.assertIn('"type":"array"', feedback)
        self.assertIn('"model":"modules.models.<module>.<Class>"', feedback)
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
        self.assertEqual(summary["summary"]["component_count"], 3)
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
    def test_progress_capture_truncates_unbounded_output(self):
        lines = []
        capture = _ProgressCapture(lines.append)

        capture.write("x" * 20_000)

        self.assertEqual(len(lines), 1)
        self.assertIn("truncated", lines[0])
        self.assertLessEqual(len(lines[0]), 16_400)

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
        self.assertEqual(result["project"]["summary"]["component_count"], 3)
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
        self.assertIn("Application verification", page)
        self.assertIn("agentStatus", page)
        self.assertIn("Repairs applied", page)

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
                progress("🧠 [Pod Agent · Step 6] verify_application (application)")
                progress("🧠 [Pod Agent · Step 7] repair_current_artifact (application)")
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
            self.assertTrue(any("verify_application" in line for line in task["logs"]))
            self.assertTrue(any("repair_current_artifact" in line for line in task["logs"]))
            self.assertTrue(task["result"]["ok"])

    def test_studio_accepts_verification_only_pod_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
                (project / "aipod_plan.json").write_text(json.dumps({
                    "version": 3,
                    "objective": "verified app",
                    "current_stage": "interfaces",
                    "stages": {
                        name: {"status": "complete", "plan": None}
                        for name in ("models", "providers", "services", "pipelines", "interfaces")
                    },
                    "agent": {
                        "status": "complete", "step": 1, "history": [],
                        "verification": {
                            "status": "passed", "attempts": 1, "repairs": 0,
                            "command": [sys.executable, "app.py", "smoke"],
                        },
                    },
                }), encoding="utf-8")
            finally:
                os.chdir(previous_cwd)
            api = StudioApi(project)

            with patch("ai_pod_cli.commands.pod.handle_pod", return_value=None):
                result = api.build_pod("verified app")

            self.assertTrue(result["ok"])
            self.assertEqual(
                result["project"]["pod_agent"]["verification"]["status"],
                "passed",
            )

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
        self.assertEqual(normalize_type("modules.models.order.Order"), "model")
        self.assertTrue(types_compatible("int", "float"))
        self.assertFalse(types_compatible("str", "bool"))

    def test_pipeline_accepts_legacy_model_path_as_structured_model(self):
        model_path = "modules.models.scenestate.SceneState"
        contract = analyze_pipeline_contracts(
            ["CreateScene", "AdvanceFrame"],
            [
                {"id": "CreateScene", "outputs": {"scene": model_path}},
                {"id": "AdvanceFrame", "inputs": {"scene": {"model": model_path}}},
            ],
        )

        self.assertTrue(contract["valid"])
        self.assertEqual(contract["links"][0]["matched"], ["scene"])

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

    def test_pipeline_detects_semantic_field_drift(self):
        components = [
            {"id": "Telemetry", "outputs": {"oxygen": "float"}},
            {"id": "LifeSupport", "inputs": {"oxygen_level": "float"}},
        ]
        contract = analyze_pipeline_contracts(["Telemetry", "LifeSupport"], components)
        self.assertFalse(contract["valid"])
        self.assertEqual(contract["issues"][0]["code"], "semantic_field_drift")
        self.assertEqual(contract["issues"][0]["produced_field"], "oxygen")
        self.assertGreater(semantic_field_similarity("oxygen", "oxygen_level"), 0.9)

    def test_pipeline_detects_nested_schema_mismatch(self):
        produced = {
            "type": "object", "required": ["shipments"], "properties": {
                "shipments": {"type": "array", "items": {
                    "type": "object", "required": ["id", "weight"],
                    "properties": {"id": {"type": "string"}, "weight": {"type": "number"}},
                }},
            },
        }
        required = {
            "type": "object", "required": ["shipments"], "properties": {
                "shipments": {"type": "array", "items": {
                    "type": "object", "required": ["shipment_id", "weight", "distance_km"],
                    "properties": {
                        "shipment_id": {"type": "string"},
                        "weight": {"type": "number"},
                        "distance_km": {"type": "number"},
                    },
                }},
            },
        }
        contract = analyze_pipeline_contracts(
            ["Simulate", "Risk"],
            [
                {"id": "Simulate", "outputs": {"shipment_snapshot": produced}},
                {"id": "Risk", "inputs": {"shipment_snapshot": required}},
            ],
        )
        self.assertFalse(contract["valid"])
        self.assertEqual(contract["issues"][0]["code"], "contract_schema_mismatch")
        paths = {item["path"] for item in contract["issues"][0]["schema_mismatches"]}
        self.assertIn("shipment_snapshot.shipments[].shipment_id", paths)
        self.assertIn("shipment_snapshot.shipments[].distance_km", paths)

    def test_runtime_nested_schema_validation_reports_exact_path(self):
        schema = {
            "shipment_snapshot": {
                "type": "object", "required": ["shipments"], "properties": {
                    "shipments": {"type": "array", "items": {
                        "type": "object", "required": ["id", "distance_km"],
                        "properties": {
                            "id": {"type": "string"}, "distance_km": {"type": "number"},
                        },
                    }},
                },
            },
        }
        errors = validate_contract_data(
            {"shipment_snapshot": {"shipments": [{"id": "SHIP001"}]}}, schema,
        )
        self.assertEqual(errors, ["$.shipment_snapshot.shipments[0].distance_km: required field is missing"])

    def test_model_contract_validates_nested_dataclass_fields(self):
        model_path = f"{__name__}.TestShipmentSnapshot"
        errors = validate_contract_data(
            {"snapshot": {"shipments": [{"id": "SHIP001"}]}},
            {"snapshot": {"model": model_path}},
        )
        self.assertEqual(
            errors,
            ["$.snapshot.shipments[0].distance_km: required field is missing"],
        )
        sample = sample_value("snapshot", {"model": model_path})
        self.assertIsInstance(sample, TestShipmentSnapshot)
        self.assertIsInstance(sample.shipments[0], TestShipment)

    def test_pipeline_requires_the_same_shared_model(self):
        contract = analyze_pipeline_contracts(
            ["Producer", "Consumer"],
            [
                {"id": "Producer", "outputs": {
                    "snapshot": {"model": f"{__name__}.TestShipmentSnapshot"},
                }},
                {"id": "Consumer", "inputs": {
                    "snapshot": {"model": f"{__name__}.OtherShipmentSnapshot"},
                }},
            ],
        )
        self.assertFalse(contract["valid"])
        self.assertEqual(contract["issues"][0]["code"], "contract_schema_mismatch")

    def test_component_runtime_rejects_invalid_declared_model_output(self):
        from ai_pod_cli.container import _ComponentRef

        class Producer:
            def execute(self, ctx):
                return {"snapshot": {"shipments": [{"id": "SHIP001"}]}}

        ref = _ComponentRef(
            "Producer", Producer(),
            outputs={"snapshot": {"model": f"{__name__}.TestShipmentSnapshot"}},
        )
        with self.assertRaisesRegex(ValueError, "distance_km"):
            ref.execute_all(PipelineContext())

    def test_service_allows_legacy_boundary_metadata_with_sqlmodel_runtime(self):
        code = (
            "from ai_pod_cli.context import PipelineContext\n"
            "class Producer:\n"
            "    def execute(self, ctx: PipelineContext):\n"
            "        ctx.set('items', [])\n"
            "        return {'items': []}\n"
        )
        violations = validate_component_contract(
            code, "Producer", "service", {}, {"items": {"type": "array"}},
        )
        self.assertEqual(violations, [])
        named_type_violations = validate_component_contract(
            code, "Producer", "service", {}, {"items": "TelemetryBatch — generated batch"},
        )
        self.assertEqual(named_type_violations, [])
        scalar_array_violations = validate_component_contract(
            code, "Producer", "service", {},
            {"items": {"type": "array", "items": {"type": "str"}}},
        )
        self.assertEqual(scalar_array_violations, [])
        map_violations = validate_component_contract(
            code, "Producer", "service", {},
            {"items": {"type": "object", "additionalProperties": {"type": "float"}}},
        )
        self.assertEqual(map_violations, [])
        optional_violations = validate_component_contract(
            code, "Producer", "service", {"note": "str | None"},
            {"items": {"type": "array", "items": {"type": "str"}}},
        )
        self.assertEqual(optional_violations, [])

    def test_service_rejects_raw_sql(self):
        code = (
            "class SqlService:\n"
            "    def execute(self, ctx):\n"
            "        query = 'SELECT * FROM telemetry'\n"
            "        return {}\n"
        )
        violations = validate_component_contract(code, "SqlService", "service", {}, {})
        self.assertTrue(any("ModelRepository" in item for item in violations))

    def test_provider_method_allows_opaque_infrastructure_output(self):
        code = "class Store:\n    def load(self):\n        return None\n"
        violations = validate_component_contract(
            code, "Store", "provider", methods={
                "load": {"inputs": {}, "outputs": "TelemetryBatch — result"},
            },
        )
        self.assertEqual(violations, [])

    def test_model_candidate_imports_from_disposable_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
                (project / "modules" / "models").mkdir(parents=True)
                (project / "modules" / "models" / "__init__.py").write_text("", encoding="utf-8")
            finally:
                os.chdir(previous)
            code = (
                "from sqlmodel import Field\n"
                "from ai_pod_cli import Model\n"
                "class Order(Model, table=True):\n"
                "    id: int | None = Field(default=None, primary_key=True)\n"
            )
            bean = {
                "id": "Order", "category": "model",
                "class_path": "modules.models.order.Order", "file": "order.py",
                "dependencies": [], "inputs": {}, "outputs": {},
            }
            self.assertEqual(validate_component_contract(code, "Order", "model"), [])
            self.assertEqual(verify_component_candidate(project, bean, code, []), [])

    def test_provider_candidate_smoke_tests_declared_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
            finally:
                os.chdir(previous)
            code = (
                "from injector import inject\n"
                "class BrokenSource:\n"
                "    @inject\n"
                "    def __init__(self): pass\n"
                "    def generate(self, machine_id: str):\n"
                "        raise ValueError('invalid frozen model field')\n"
            )
            bean = {
                "id": "BrokenSource", "category": "provider",
                "class_path": "modules.providers.brokensource.BrokenSource",
                "file": "brokensource.py", "dependencies": [],
                "methods": {"generate": {
                    "inputs": {"machine_id": "str"}, "outputs": "dict",
                }},
            }
            violations = verify_component_candidate(project, bean, code, [])
            self.assertTrue(any("invalid frozen model field" in item for item in violations))

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
            self.assertTrue((ordinary / "modules" / "models" / "__init__.py").exists())
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
                (project / "pipelines").mkdir(exist_ok=True)
                (project / "pipelines" / "existing_route.py").write_text(
                    "def run(ctx):\n    return ctx.summary()\n", encoding="utf-8",
                )
                plan = {
                    "pod_name": "reuse_test",
                    "reuse_components": ["ExistingService"],
                    "components": [{"name": "ExistingService", "category": "service", "description": "same"}],
                    "pipelines": [{"name": "existing_route", "instruction": "reuse it"}],
                    "config_additions": {},
                }
                args = type("Args", (), {"desc": "reuse", "file": "", "yes": True, "json": True})()
                responses = []
                for action in (
                    "generate_models", "generate_providers", "generate_services",
                    "compose_pipelines", "generate_interfaces",
                ):
                    responses.extend([
                        {"action": action, "summary": f"run {action}", "success_criteria": ["frozen"]},
                        plan,
                    ])
                output = io.StringIO()
                with (
                    patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
                    patch("ai_pod_cli.commands.pod.call_llm", side_effect=responses) as llm,
                    redirect_stdout(output),
                ):
                    handle_pod(args)
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(llm.call_count, 10)
            self.assertIn("ExistingService (reuse)", output.getvalue())
            self.assertIn("[Pipeline 复用] existing_route", output.getvalue())
            state = json.loads((project / "aipod_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(state["agent"]["status"], "complete")
            self.assertEqual(
                [item["action"] for item in state["agent"]["history"]],
                [
                    "generate_models", "generate_providers", "generate_services",
                    "compose_pipelines", "generate_interfaces", "verify_application",
                ],
            )

    def test_pod_accepts_empty_provider_stage_and_continues_to_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
                args = type(
                    "Args", (),
                    {"desc": "local app", "file": "", "yes": True, "json": True, "_pod_stage": 1},
                )()
                plans = [
                    {
                        "pod_name": "no_external_providers",
                        "reuse_components": [], "components": [], "pipelines": [],
                        "interfaces": [], "config_additions": {},
                    },
                    {
                        "pod_name": "services",
                        "reuse_components": ["ModelRepository"], "components": [],
                        "pipelines": [], "interfaces": [], "config_additions": {},
                    },
                    {
                        "pod_name": "pipelines",
                        "reuse_components": ["PipelineRunner"], "components": [],
                        "pipelines": [], "interfaces": [], "config_additions": {},
                    },
                    {
                        "pod_name": "interfaces",
                        "reuse_components": ["PipelineRunner"], "components": [],
                        "pipelines": [], "interfaces": [], "config_additions": {},
                    },
                ]
                action_by_stage = {
                    "providers": "generate_providers",
                    "services": "generate_services",
                    "pipelines": "compose_pipelines",
                    "interfaces": "generate_interfaces",
                }
                plan_by_stage = dict(zip(action_by_stage, plans))

                def respond(_system, _user, **kwargs):
                    label = kwargs.get("progress_label", "")
                    stage_name = next((name for name in action_by_stage if name in label), None)
                    if stage_name is None:
                        raise AssertionError(f"unexpected model call: {label}")
                    if label.startswith("Pod Agent selecting"):
                        action = action_by_stage[stage_name]
                        return {
                            "action": action, "summary": f"run {action}",
                            "success_criteria": ["frozen"],
                        }
                    return plan_by_stage[stage_name]
                output = io.StringIO()
                with (
                    patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
                    patch("ai_pod_cli.commands.pod.call_llm", side_effect=respond) as llm,
                    redirect_stdout(output),
                ):
                    handle_pod(args)
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(llm.call_count, 8)
            self.assertIn("[providers 阶段已冻结]", output.getvalue())
            self.assertIn("generate_services (services)", output.getvalue())

    def test_pod_agent_observes_failure_and_retries_only_current_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
                args = type("Args", (), {
                    "desc": "agent retry", "file": "", "yes": True, "json": True,
                    "_pod_agent_max_steps": 3,
                })()
                tool_calls = []

                def fake_build_tool(_args):
                    tool_calls.append(_args._pod_stage)
                    if len(tool_calls) == 1:
                        raise SystemExit(1)
                    state = _load_decision_plan("agent retry")
                    for name in state["stages"]:
                        state["stages"][name]["status"] = "complete"
                    _save_decision_plan(state)

                decisions = [
                    {"action": "generate_models", "summary": "start models"},
                    {"action": "retry_current", "summary": "retry failed models"},
                ]
                with (
                    patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
                    patch("ai_pod_cli.commands.pod.call_llm", side_effect=decisions),
                    patch("ai_pod_cli.commands.pod._execute_pod_build_tool", side_effect=fake_build_tool),
                ):
                    handle_pod(args)
                inspected = inspect_project()
            finally:
                os.chdir(previous_cwd)

            state = json.loads((project / "aipod_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(tool_calls, [0, 0])
            self.assertEqual(
                [item["status"] for item in state["agent"]["history"]],
                ["failed", "succeeded", "succeeded"],
            )
            self.assertEqual(state["agent"]["history"][1]["requested_action"], "retry_current")
            self.assertNotIn("chain_of_thought", json.dumps(state))

            self.assertEqual(inspected["pod_agent"]["status"], "complete")
            self.assertEqual(inspected["pod_agent"]["last_action"], "verify_application")
            self.assertEqual(inspected["pod_agent"]["verification"]["status"], "passed")

    def test_pod_agent_verifies_repairs_current_artifact_and_reverifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            previous_cwd = Path.cwd()
            os.chdir(project)
            try:
                init_config_if_not_exists()
                entry = project / "engine_cli.py"
                failing_line = 'raise RuntimeError("smoke failed")'
                entry.write_text(
                    "from ai_pod_cli.config import load_beans\n"
                    "from ai_pod_cli.container import build_container\n\n"
                    "def main():\n"
                    "    build_container(load_beans())\n"
                    f"    {failing_line}\n\n"
                    "if __name__ == '__main__':\n"
                    "    main()\n",
                    encoding="utf-8",
                )
                stable = project / "modules" / "models" / "stable.py"
                stable.parent.mkdir(parents=True, exist_ok=True)
                stable.write_text("FROZEN = True\n", encoding="utf-8")

                state = _load_decision_plan("repair app")
                for record in state["stages"].values():
                    record["status"] = "complete"
                state["stages"]["interfaces"]["plan"] = {
                    "interfaces": [{
                        "name": "engine_cli", "kind": "cli",
                        "instruction": "Run the `smoke` command without interaction.",
                    }],
                }
                state["stages"]["interfaces"]["artifacts"] = ["engine_cli.py"]
                _save_decision_plan(state)
                args = type("Args", (), {
                    "desc": "repair app", "file": "", "yes": True, "json": True,
                })()

                with (
                    patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
                    patch("ai_pod_cli.commands.pod.call_llm", return_value={
                        "patches": [{"old": failing_line, "new": 'print("SMOKE OK")'}],
                    }) as llm,
                ):
                    handle_pod(args)
            finally:
                os.chdir(previous_cwd)

            final_state = json.loads((project / "aipod_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(llm.call_count, 1)
            self.assertEqual(final_state["agent"]["status"], "complete")
            self.assertEqual(final_state["agent"]["verification"]["status"], "passed")
            self.assertEqual(final_state["agent"]["verification"]["repairs"], 1)
            self.assertEqual(
                [item["action"] for item in final_state["agent"]["history"]],
                ["verify_application", "repair_current_artifact", "verify_application"],
            )
            self.assertIn('print("SMOKE OK")', entry.read_text(encoding="utf-8"))
            self.assertEqual(stable.read_text(encoding="utf-8"), "FROZEN = True\n")

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
