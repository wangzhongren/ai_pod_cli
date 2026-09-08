"""The same registered utility is shared across existing five-layer roles."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_pod_cli.config import init_config_if_not_exists, load_beans_summary
from ai_pod_cli.pod.verification import _project_verification_fingerprint, _repair_current_artifact
from ai_pod_cli.project_model import build_project_model, inspect_project
from ai_pod_cli.utilities import read_utility, write_utility
from ai_pod_cli.validation import (
    validate_component_contract, validate_entry_imports,
    validate_interface_adapter_imports, validate_pipeline_contract,
)


UTILITY = '''class NumberTools:
    """Shared numeric bounds with no application state."""

    @staticmethod
    def clamp(value: float, lower: float, upper: float) -> float:
        """Keep a number between the supplied bounds."""
        return max(lower, min(value, upper))
'''
CASES = [{"method": "clamp", "args": [20, 0, 10], "expected": 10},
         {"method": "clamp", "args": [-2, 0, 10], "expected": 0}]


class UtilityReuseTests(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        self.root = Path.cwd()
        init_config_if_not_exists()
        self.entry = write_utility("NumberTools", UTILITY, "Numeric clamping", CASES)
        self.import_line = "from modules.utils.numbertools import NumberTools\n"

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def test_provider_service_model_pipeline_and_interface_share_one_class(self):
        provider = self.import_line + '''class InputProvider:
    def normalize(self, value):
        return NumberTools.clamp(value, 0, 10)
'''
        service = self.import_line + '''class PriceService:
    def execute(self, ctx):
        return {"bounded": NumberTools.clamp(ctx.get("value"), 0, 10)}
'''
        model = self.import_line + '''from ai_pod_cli import Model
class Reading(Model):
    value: float
    def bounded(self):
        return NumberTools.clamp(self.value, 0, 10)
'''
        pipeline = self.import_line + "def run(ctx):\n    return {'bounded': NumberTools.clamp(ctx.get('value'), 0, 10)}\n"
        interface = self.import_line + "def format_value(value):\n    return str(NumberTools.clamp(value, 0, 10))\n"
        self.assertEqual(validate_component_contract(provider, "InputProvider", "provider"), [])
        self.assertEqual(validate_component_contract(service, "PriceService", "service", {"value": "float"}, {"bounded": "float"}), [])
        self.assertEqual(validate_component_contract(model, "Reading", "model"), [])
        self.assertEqual(validate_pipeline_contract(pipeline), [])
        self.assertEqual(validate_entry_imports(interface), [])
        self.assertEqual(validate_interface_adapter_imports(interface), [])
        files = {
            "modules/providers/inputprovider.py": provider,
            "modules/services/priceservice.py": service,
            "modules/models/reading.py": model,
            "pipelines/bounded.py": pipeline,
            "interfaces/demo/formatter.py": interface,
        }
        for name, code in files.items():
            path = Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(code)
        Path("routes.toml").write_text('[bounded]\npipeline="pipelines/bounded.py"\n')
        run = subprocess.run([sys.executable, "-c", '''
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.runner import PipelineRunner
from modules.providers.inputprovider import InputProvider
from modules.services.priceservice import PriceService
from modules.models.reading import Reading
from interfaces.demo.formatter import format_value
assert InputProvider().normalize(20) == 10
assert PriceService().execute(PipelineContext({'value': -2})) == {'bounded': 0}
assert Reading(value=5).bounded() == 5
assert PipelineRunner().run('bounded', {'value': 30})['bounded'] == 10
assert format_value(-20) == '0'
'''], cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(json.loads(Path("utility_registry.json").read_text())["utilities"]), 1)
        callers = str(read_utility("NumberTools")["callers"])
        for name in files:
            self.assertIn(name, callers)

    def test_interfaces_can_import_only_exact_registered_utility_symbols(self):
        for source in (
            "from modules.utils.unregistered import NumberTools\n",
            "from modules.utils.numbertools import Hidden\n",
            "from modules.utils.numbertools import *\n",
            "from modules.services.priceservice import PriceService\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(validate_entry_imports(source))
        self.assertEqual(validate_entry_imports(self.import_line), [])
        self.assertEqual(validate_entry_imports("import modules.utils.numbertools as tools\n"), [])

    def test_catalog_is_visible_without_exposing_services(self):
        summary = load_beans_summary(include_services=False)
        self.assertIn("NumberTools", summary)
        self.assertIn("clamp", summary)
        self.assertIn("modules.utils.numbertools", summary)
        result = inspect_project("utility", "NumberTools")
        self.assertEqual(result["utility"]["source"], UTILITY)
        self.assertEqual(len(inspect_project("utilities")["utilities"]), 1)
        beans = json.loads(Path("beans_config.json").read_text())["beans"]
        self.assertFalse(any(bean["id"] == "NumberTools" for bean in beans))

    def test_source_drift_invalidates_project_and_prior_proof(self):
        before = _project_verification_fingerprint()
        path = Path(self.entry["path"])
        path.write_text(UTILITY.replace("return max(lower, min(value, upper))", "return value"))
        self.assertNotEqual(before, _project_verification_fingerprint())
        result = build_project_model()
        self.assertFalse(result["validation"]["valid"])
        self.assertTrue(any(issue["code"] == "invalid_utility_registry" for issue in result["validation"]["issues"]))
        self.assertTrue(validate_entry_imports(self.import_line))

    def test_cli_list_and_read_do_not_need_a_model(self):
        env = {**os.environ, "OPENAI_API_KEY": ""}
        for args in (["list"], ["read", "NumberTools"]):
            run = subprocess.run([sys.executable, "-m", "ai_pod_cli", "utility", *args], cwd=self.root,
                                 env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("NumberTools", json.loads(run.stdout).get("id", run.stdout))

    def test_one_file_repair_cannot_bypass_global_utility_update(self):
        state = {"agent": {"verification": {"last_result": {
            "repair": {"suggested_files": [self.entry["path"]]},
        }}}, "stages": {}}
        before = Path(self.entry["path"]).read_bytes()
        with patch("ai_pod_cli.pod.verification.call_llm") as model:
            with self.assertRaisesRegex(RuntimeError, "共享工具类"):
                _repair_current_artifact("repair", state)
            model.assert_not_called()
        self.assertEqual(Path(self.entry["path"]).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
