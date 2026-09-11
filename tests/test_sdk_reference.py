"""Run the exact prompt examples against a real project and the bundled SDK."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import Mock

from ai_pod_cli.sdk_reference import ROLE_SECTIONS, SDK_EXAMPLES, sdk_reference
from ai_pod_cli.workspace import WorkspaceTools
from ai_pod_cli.workspace_agent import WorkspaceAgent


class SDKReferenceTests(unittest.TestCase):
    def test_role_reference_survives_ai_history_compaction(self):
        with tempfile.TemporaryDirectory() as directory:
            for role in ROLE_SECTIONS:
                with self.subTest(role=role):
                    tools = WorkspaceTools(Path(directory), role)
                    # Cross the compaction threshold without executing SDK discovery.
                    tools.execute = Mock(side_effect=[
                        {"content": f"observation-{i}:" + "x" * 41000} for i in range(4)
                    ])
                    replies = iter([
                        '<read><path>README.md</path></read>' for _ in range(4)
                    ] + ['<finish><summary>Checked</summary></finish>'])
                    def respond(system, _user, **_kwargs):
                        if system.startswith("CONTEXT_COMPACTION"):
                            return {"summary": "Earlier file observations were read; no implementation or check occurred."}
                        return next(replies)
                    llm = Mock(side_effect=respond)
                    WorkspaceAgent(llm, tools, max_steps=5, instruction_mode="direct").run(
                        "Check example", {}, request_change=Mock(),
                        finish=lambda action, _tools: action,
                    )
                    for call in llm.call_args_list:
                        system = call.args[0]
                        if system.startswith("CONTEXT_COMPACTION"):
                            self.assertTrue(call.kwargs["json_mode"])
                            continue
                        self.assertIn(sdk_reference(role), system)
                        self.assertIn("AIPod Instruction Set", system)
                        self.assertFalse(call.kwargs["json_mode"])
                    self.assertNotIn("observation-0:", llm.call_args_list[-1].args[1])
                    self.assertIn("observation-3:", llm.call_args_list[-1].args[1])
                    self.assertIn("Earlier file observations", llm.call_args_list[-1].args[1])
        self.assertIn(SDK_EXAMPLES["model"], sdk_reference("models"))
        self.assertNotIn("INTERFACE SDK", sdk_reference("models"))
        self.assertNotIn("CONFIGURATION, DEPENDENCIES AND STORAGE", sdk_reference("interfaces"))
        self.assertIn(SDK_EXAMPLES["repository"], sdk_reference("providers"))
        self.assertIn(SDK_EXAMPLES["service"], sdk_reference("services"))
        self.assertIn(SDK_EXAMPLES["pipeline"], sdk_reference("pipelines"))
        for role in ("interfaces", "pod"):
            self.assertIn(SDK_EXAMPLES["interface_check"], sdk_reference(role))
            self.assertIn("may block", sdk_reference(role))
        with self.assertRaises(ValueError):
            sdk_reference("unknown")

    def test_prompt_examples_execute_in_a_real_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def write(path, content):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                for parent in target.parents:
                    if parent == root:
                        break
                    (parent / "__init__.py").touch()

            files = {
                "model": "modules/models/value.py",
                "provider": "modules/providers/impl/example.py",
                "service": "modules/services/impl/example.py",
                "pipeline": "pipelines/example.py",
                "adapter": "interfaces/example/adapter.py",
                "contracts": "checks/contracts.py",
                "repository": "checks/repository.py",
                "runner": "checks/runner.py",
                "interface_check": "checks/interface_check.py",
            }
            self.assertEqual(set(files), set(SDK_EXAMPLES))
            for name, path in files.items():
                write(path, SDK_EXAMPLES[name])
            write("modules/providers/public/example.py",
                  "from modules.providers.impl.example import ConfiguredProvider\n")
            write("modules/services/public/example.py",
                  "from modules.services.impl.example import ExampleService\n")
            write("modules/models/record.py", textwrap.dedent('''\
                from ai_pod_cli import Model
                from sqlmodel import Field
                class Record(Model, table=True):
                    id: int | None = Field(default=None, primary_key=True)
                    value: int
            '''))
            contracts = {}
            exec(SDK_EXAMPLES["contracts"], contracts)
            beans = [
                {"id": "ConfigStore", "category": "provider", "dependencies": [],
                 "class_path": "ai_pod_cli.config_store.ConfigStore"},
                {"id": "ConfiguredProvider", "category": "provider", "dependencies": ["ConfigStore"],
                 "class_path": "modules.providers.public.example.ConfiguredProvider"},
                {"id": "ExampleService", "category": "service", "dependencies": ["ConfiguredProvider"],
                 "class_path": "modules.services.public.example.ExampleService",
                 "inputs": contracts["INPUTS"], "outputs": contracts["OUTPUTS"]},
            ]
            write("beans_config.json", json.dumps({"beans": beans}))
            write("config.toml", "[example]\nincrement = 2\n")
            write("routes.toml", '[example]\npipeline = "pipelines/example.py"\n')
            write("interfaces/example/interface.json", json.dumps({
                "name": "example", "kind": "cli", "artifacts": [],
                "adapter": {"path": "interfaces/example/adapter.py", "class_name": "ExampleAdapter"},
            }))
            write("verify.py", textwrap.dedent('''\
                import os
                from pathlib import Path
                from ai_pod_cli import PipelineContext
                from ai_pod_cli.config import load_beans
                from ai_pod_cli.config_store import ConfigStore
                from ai_pod_cli.container import build_container
                from ai_pod_cli.contracts import validate_contract_data
                from ai_pod_cli.repository import ModelRepository
                from ai_pod_cli.runner import PipelineRunner
                from modules.models.value import ExampleValue
                from modules.models.record import Record
                from modules.providers.public.example import ConfiguredProvider
                from modules.services.public.example import ExampleService
                from checks.contracts import INPUTS, OUTPUTS
                from checks.repository import repository_example
                from checks.runner import run_example
                from checks.interface_check import check_interface
                from pipelines.example import run

                assert ExampleValue.validate({"value": 3}) == []
                assert ExampleValue.validate({"value": "bad"})
                assert ExampleValue.model_validate({"value": 3}).to_dict() == {"value": 3}
                config = ConfigStore()
                assert config.get("example.increment") == 2
                repo = ModelRepository(config)
                try:
                    found, matches = repository_example(repo, Record)
                    assert found.value == 3 and matches[0].id == found.id
                    assert repo.get(Record, found.id) is None
                    assert repo.list(Record) == []
                finally:
                    repo.close()

                assert validate_contract_data({}, INPUTS) == []
                assert validate_contract_data({"value": None}, INPUTS)
                assert validate_contract_data({}, OUTPUTS)
                container = build_container(load_beans())
                assert container.get(ConfiguredProvider) is container.get(ConfiguredProvider)
                ctx = PipelineContext({"value": 3}, data={"value": 4})
                assert container.get(ExampleService).execute(ctx) == {"answer": 6}
                assert ctx.get("answer") == 6
                assert run(PipelineContext()) == {"answer": 3}
                assert run_example() == {"answer": 5}
                runner = PipelineRunner()
                result, ctx = runner.run_with_context("example", {"value": 3})
                assert result == {"answer": 5} and ctx.get("answer") == 5
                assert len(ctx.steps) == 1
                try:
                    runner.run("example", {"value": "bad"})
                except ValueError as error:
                    assert "inputs schema validation failed" in str(error)
                else:
                    raise AssertionError("Invalid input was accepted")

                # InterfaceContext must also work when the caller is outside the project.
                root = Path.cwd()
                os.chdir(root.parent)
                manifest_path, adapter, context = check_interface(root)
                assert manifest_path == root / "interfaces/example/interface.json"
                assert adapter.start(context, {"value": 4}) == {"answer": 6}
                assert context.run_route("example") == {"answer": 3}
                assert Path.cwd() == root.parent
                print("All Python SDK prompt examples passed")
            '''))
            env = dict(os.environ)
            source_root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
            env["AIPOD_DATABASE_URL"] = "sqlite:///" + str(root / "test.db")
            completed = subprocess.run(
                [sys.executable, str(root / "verify.py")], cwd=root, env=env,
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("All Python SDK prompt examples passed", completed.stdout)
