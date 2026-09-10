"""Nested public entries must load real components and preserve layer boundaries."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from ai_pod_cli.component_layout import SourceGraph, validate_layout
from ai_pod_cli.pod.coordinator import PodCoordinator
from ai_pod_cli.pod.state import load_and_upgrade_plan
from ai_pod_cli.workspace import WorkspaceTools, LAYERS
from ai_pod_cli.validation import validate_component_contract


class ComponentLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.beans = [
            {"id": "Store", "category": "provider", "class_path": "modules.providers.public.storage.Store", "dependencies": [], "inputs": {}, "outputs": {}},
            {"id": "Calculate", "category": "service", "class_path": "modules.services.public.Calculate", "dependencies": ["Store"], "inputs": {}, "outputs": {"total": "int"}},
        ]
        self.write("modules/__init__.py", "")
        self.write("modules/providers/contracts/storage.py", "from typing import Protocol\nclass Storage(Protocol):\n    def value(self) -> int: ...\n")
        self.write("modules/providers/impl/storage/memory.py", "class MemoryStore:\n    def value(self): return 6\n")
        self.write("modules/providers/public/storage.py", "from ..impl.storage.memory import MemoryStore as Store\n__all__ = ['Store']\n")
        self.write("modules/services/contracts/calculation.py", "from typing import Protocol\nclass Calculation(Protocol):\n    def execute(self, ctx): ...\n")
        self.write("modules/services/impl/math/rules/arithmetic.py", "def double(value): return value * 2\n")
        self.write("modules/services/impl/math/calculate.py", "from injector import inject\nfrom modules.providers.public.storage import Store\nfrom .rules.arithmetic import double\nclass Calculator:\n    @inject\n    def __init__(self, store: Store): self.store = store\n    def execute(self, ctx):\n        ctx.set('total', double(self.store.value()))\n")
        self.write("modules/services/public/__init__.py", "from ..impl.math.calculate import Calculator as Calculate\n__all__ = ['Calculate']\n")

    def write(self, path, code):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")

    def test_public_aliases_packages_and_nested_helpers_run_with_real_injection(self):
        self.assertEqual(validate_layout(self.root, self.beans), [])
        self.write("beans_config.json", json.dumps({"beans": self.beans}))
        script = """import json
from ai_pod_cli.container import build_container, Pod
from ai_pod_cli import PipelineContext
from modules.services.public import Calculate
container = build_container(json.load(open('beans_config.json')))
ctx = PipelineContext()
container.get(Calculate).execute(ctx)
assert ctx.get('total') == 12
config = json.load(open('beans_config.json'))
config['beans'][1]['outputs'] = {'total': 'str'}
try:
    Pod(build_container(config))(Calculate).execute_all(PipelineContext())
except ValueError as error:
    assert 'total' in str(error)
else:
    raise AssertionError('A public alias must retain its registered output contract')
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=self.root, capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_internal_reorganization_keeps_public_registration_stable(self):
        path = "modules/services/impl/math/rules/arithmetic.py"
        self.write("modules/services/impl/algorithms/numbers/double.py", "def double(value): return value * 2\n")
        self.write(path, "from ...algorithms.numbers.double import double\n")
        self.assertEqual(validate_layout(self.root, self.beans), [])
        self.assertIn("modules/services/impl/algorithms/numbers/double.py", SourceGraph(self.root).closure("modules/services/impl/math/calculate.py"))

    def test_no_private_cross_layer_imports_in_helpers_or_pipelines(self):
        for path in ("modules/services/impl/math/rules/arithmetic.py", "pipelines/route.py"):
            with self.subTest(path=path):
                self.write(path, "from modules.providers.impl.storage.memory import MemoryStore\n")
                self.assertTrue(any("through public/" in error for error in validate_layout(self.root, self.beans)))
                (self.root / path).unlink()
                if "arithmetic" in path:
                    self.write(path, "def double(value): return value * 2\n")

    def test_contracts_cannot_depend_on_implementation(self):
        self.write("modules/providers/contracts/storage.py", "from ..public.storage import Store\n")
        self.assertTrue(any("contracts cannot depend" in error for error in validate_layout(self.root, self.beans)))

    def test_service_cannot_hide_another_service_or_runtime_in_a_helper(self):
        self.write("modules/services/impl/other.py", "class Other:\n    def execute(self, ctx): pass\n")
        self.write("modules/services/public/other.py", "from ..impl.other import Other\n")
        self.beans.append({"id": "Other", "class_path": "modules.services.public.other.Other", "category": "service"})
        self.write("modules/services/impl/math/rules/arithmetic.py", "from ...other import Other as Hidden\ndef double(value): return value * 2\n")
        self.assertTrue(any("another Service" in error for error in validate_layout(self.root, self.beans)))
        self.write("modules/services/impl/math/rules/arithmetic.py", "from ai_pod_cli.runner import PipelineRunner\ndef double(value): return value * 2\n")
        self.assertTrue(any("Runtime" in error for error in validate_layout(self.root, self.beans)))

    def test_rejects_private_registrations_cycles_and_public_logic_without_execution(self):
        self.beans[0]["class_path"] = "modules.providers.impl.storage.memory.MemoryStore"
        self.assertTrue(any("Register components through public" in error for error in validate_layout(self.root, self.beans)))
        self.beans[0]["class_path"] = "modules.providers.public.storage.Store"
        self.write("modules/providers/public/storage.py", "from .second import Store\n")
        self.write("modules/providers/public/second.py", "from .storage import Store\n")
        self.assertTrue(any("Cyclic" in error for error in validate_layout(self.root, self.beans)))
        self.write("modules/providers/public/storage.py", "open('unexpected', 'w').write('bad')\nfrom ..impl.storage.memory import MemoryStore as Store\n")
        self.assertTrue(any("public contains only" in error for error in validate_layout(self.root, self.beans)))
        self.assertFalse((self.root / "unexpected").exists())
        self.write("modules/services/impl/math/calculate.py", "class Calculator: pass\n")
        self.assertTrue(any("execute(self, ctx)" in error for error in validate_layout(self.root, self.beans)))

    def test_missing_exports_and_symlink_implementations_fail(self):
        self.write("modules/providers/public/storage.py", "from ..impl.storage.memory import Missing as Store\n")
        self.assertTrue(any("Cannot resolve class" in error for error in validate_layout(self.root, self.beans)))
        self.write("modules/providers/public/storage.py", "from ..impl.link import MemoryStore as Store\n")
        (self.root / "modules/providers/impl/link.py").symlink_to(self.root / "modules/providers/impl/storage/memory.py")
        self.assertTrue(any("symlinks" in error for error in validate_layout(self.root, self.beans)))

    def test_legacy_flat_components_still_work(self):
        self.write("modules/services/legacy.py", "class Legacy:\n    def execute(self, ctx): pass\n")
        self.assertEqual(validate_layout(self.root, [{"id": "Legacy", "category": "service", "class_path": "modules.services.legacy.Legacy"}], stage="services"), [])

    def test_colocated_services_keep_separate_fields_and_validate_local_helpers(self):
        source = '''def read_value(ctx):
    return ctx.get('value')
class First:
    def execute(self, ctx):
        ctx.set('answer', read_value(ctx))
class Second:
    def execute(self, ctx):
        ctx.set('other', ctx.get('name'))
'''
        self.assertEqual(validate_component_contract(source, 'First', 'service', {'value':'int'}, {'answer':'int'}), [])
        self.assertEqual(validate_component_contract(source, 'Second', 'service', {'name':'str'}, {'other':'str'}), [])
        self.assertTrue(any("'value'" in issue for issue in validate_component_contract(source, 'First', 'service', {}, {'answer':'int'})))

    def test_scaffold_and_scoped_owner_repair_register_public_entry(self):
        for stage in ("providers", "services"):
            WorkspaceTools(self.root, stage)
            for area in ("contracts", "impl", "public"):
                self.assertTrue((self.root / "modules" / stage / area).is_dir())
        self.write("beans_config.json", json.dumps({"beans": self.beans}))
        state = load_and_upgrade_plan(None, "Calculate twice the stored value")
        for stage in LAYERS:
            state["stages"][stage]["status"] = "complete"
        path = "modules/services/impl/math/rules/arithmetic.py"
        tools = WorkspaceTools(self.root, "services", [path])
        tools.execute({"tool": "write", "path": path, "content": "def double(value): return value + value\n"})
        try:
            check = tools.shell(shlex.quote(sys.executable) + " -c " + shlex.quote("from modules.services.public import Calculate; assert Calculate"))
        except RuntimeError as error:
            self.skipTest(str(error))
        if "sandbox_apply: Operation not permitted" in check["output"]:
            self.skipTest("Outer sandbox prevents nested shell; run with local shell permission")
        self.assertEqual(check["exit_code"], 0, check["output"])
        coordinator = PodCoordinator(self.root, state, None, save=lambda _: None)
        result = coordinator.accept("services", {"components": [self.beans[1]], "summary": "Nested helper repaired"}, tools)
        self.assertEqual(result["status"], "complete")
        registered = json.loads((self.root / "beans_config.json").read_text())["beans"][1]
        self.assertEqual(registered["class_path"], self.beans[1]["class_path"])
        self.assertEqual(registered["file"], "public/__init__.py")
        with self.assertRaises(PermissionError):
            tools.execute({"tool": "write", "path": "modules/services/public/__init__.py", "content": ""})

    def test_remove_one_component_preserves_shared_public_entry(self):
        self.beans.append({"id": "Other", "class_path": "modules.providers.public.storage.Other", "category": "provider"})
        self.write("modules/providers/impl/other.py", "class Other: pass\n")
        self.write("beans_config.json", json.dumps({"beans": self.beans}))
        path = "modules/providers/public/storage.py"
        self.write(path, "from ..impl.storage.memory import MemoryStore as Store\nfrom ..impl.other import Other\n")
        tools = WorkspaceTools(self.root, "providers", [path])
        tools.execute({"tool": "write", "path": path, "content": "from ..impl.storage.memory import MemoryStore as Store\n"})
        try:
            check = tools.shell(shlex.quote(sys.executable) + " -c " + shlex.quote("from modules.providers.public.storage import Store; assert Store().value() == 6"))
        except RuntimeError as error:
            self.skipTest(str(error))
        if "sandbox_apply: Operation not permitted" in check["output"]:
            self.skipTest("Outer sandbox prevents nested shell; run with local shell permission")
        self.assertEqual(check["exit_code"], 0, check["output"])
        state = load_and_upgrade_plan(None, "Remove unused Other provider")
        for stage in LAYERS:
            state["stages"][stage]["status"] = "complete"
        coordinator = PodCoordinator(self.root, state, None, save=lambda _: None)
        coordinator.accept("providers", {"remove": ["Other"]}, tools)
        registered = json.loads((self.root / "beans_config.json").read_text())["beans"]
        self.assertEqual([bean["id"] for bean in registered], ["Store", "Calculate"])
        self.assertEqual(validate_layout(self.root, registered), [])


if __name__ == "__main__":
    unittest.main()
