"""Test-first ordering and immutable acceptance during retries and resume."""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_pod_cli.commands.create import handle_create
from ai_pod_cli.config import init_config_if_not_exists
from ai_pod_cli.repair import classify_failures
from ai_pod_cli.source_codec import encode_source_artifact
from ai_pod_cli.test_generation import (
    prepare_component_tests, read_frozen_test, validate_test_plan,
)


TESTS = [{"name": "test_default", "requirement": "Sample.value defaults to 1"}]
COMPONENT = {"name": "Sample", "category": "model", "description": "Sample.value defaults to 1"}
METADATA = {"dependencies": [], "inputs": {}, "outputs": {}, "extra_deps": [], "tests": TESTS}
TEST_SOURCE = """import unittest
from ai_pod_cli.testing import Sandbox

class ComponentTests(unittest.TestCase):
    def test_default(self):
        with Sandbox() as s:
            result = s.model("Sample", {})
            self.assertEqual(result.value, 1)
"""
SOURCE = "from ai_pod_cli import Model\nclass Sample(Model):\n    value: int = 1\n"


@contextmanager
def project():
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as temporary:
        try:
            os.chdir(temporary)
            with redirect_stdout(io.StringIO()):
                init_config_if_not_exists()
            yield Path(temporary)
        finally:
            os.chdir(previous)


def test_llm(system, _user, **options):
    if options["json_mode"]:
        return METADATA.copy()
    path = re.search(r"tests/components/Sample_[a-f0-9]+\.py", system).group()
    return encode_source_artifact(path, TEST_SOURCE)


class TestGenerationTests(unittest.TestCase):
    def prepare(self, llm, root, **kwargs):
        return prepare_component_tests(
            llm, "Metadata requirements", "Sample", "modules/models/sample.py",
            component=COMPONENT, project_root=root, **kwargs,
        )

    def test_freezes_real_test_source_before_implementation_and_reuses_without_llm(self):
        with project() as root:
            llm = Mock(side_effect=test_llm)
            metadata, descriptor, source = self.prepare(llm, root)
            self.assertEqual(source, TEST_SOURCE)
            self.assertEqual(metadata["tests"], TESTS)
            self.assertEqual(read_frozen_test(root, descriptor), TEST_SOURCE)
            self.assertFalse((root / "modules/models/sample.py").exists())
            self.assertEqual([call.kwargs["json_mode"] for call in llm.call_args_list], [True, False])
            resume_llm = Mock(side_effect=AssertionError("resume must not regenerate tests/metadata"))
            self.assertEqual(self.prepare(resume_llm, root), (metadata, descriptor, source))
            resume_llm.assert_not_called()

    def test_hash_drift_or_deleted_tests_cannot_silently_regenerate(self):
        for tamper in ("overwrite", "delete"):
            with self.subTest(tamper=tamper), project() as root:
                _, descriptor, _ = self.prepare(test_llm, root)
                path = root / descriptor["path"]
                path.write_text("pass\n") if tamper == "overwrite" else path.unlink()
                llm = Mock()
                with self.assertRaisesRegex(ValueError, "AIPOD_TEST_INVALID"):
                    self.prepare(llm, root, allow_revision=True)
                llm.assert_not_called()

    def test_frozen_crlf_test_source_keeps_its_hash_when_read_back(self):
        raw = TEST_SOURCE.replace("\n", "\r\n")
        def llm(system, user, **options):
            if options["json_mode"]:
                return METADATA
            path = re.search(r"tests/components/Sample_[a-f0-9]+\.py", system).group()
            return encode_source_artifact(path, raw)
        with project() as root:
            _, descriptor, source = self.prepare(llm, root)
            self.assertEqual(source, raw)
            self.assertEqual(read_frozen_test(root, descriptor), raw)
            self.assertEqual((root / descriptor["path"]).read_bytes(), raw.encode())

    def test_changed_requirement_needs_explicit_revision_and_keeps_old_test_version(self):
        with project() as root:
            _, old_descriptor, _ = self.prepare(test_llm, root)
            updated = {**COMPONENT, "description": "Sample.value still defaults to 1; value is an integer"}
            with self.assertRaisesRegex(ValueError, "AIPOD_TEST_SETUP"):
                prepare_component_tests(Mock(), "rules", "Sample", "modules/models/sample.py",
                                        component=updated, project_root=root)
            _, descriptor, _ = prepare_component_tests(
                test_llm, "rules", "Sample", "modules/models/sample.py", component=updated,
                project_root=root, allow_revision=True,
            )
            self.assertNotEqual(old_descriptor["path"], descriptor["path"])
            self.assertEqual(read_frozen_test(root, old_descriptor), TEST_SOURCE)

    def test_changed_frozen_metadata_cannot_redefine_the_contract_on_resume(self):
        with project() as root:
            self.prepare(test_llm, root)
            path = root / ".aipod/component-tests.json"
            journal = json.loads(path.read_text())
            journal["components"]["Sample"]["metadata"]["tests"][0]["requirement"] = "Always return anything"
            path.write_text(json.dumps(journal))
            llm = Mock()
            with self.assertRaisesRegex(ValueError, "AIPOD_TEST_INVALID"):
                self.prepare(llm, root)
            llm.assert_not_called()

    def test_missing_or_invalid_scenario_plan_prevents_source_generation(self):
        for cases in (None, [], [{"name": "test_x"}], TESTS * 2):
            self.assertTrue(validate_test_plan(cases))
        with project() as root:
            llm = Mock(return_value={"dependencies": []})
            with self.assertRaisesRegex(ValueError, "AIPOD_TEST_SETUP"):
                self.prepare(llm, root)
            self.assertEqual(llm.call_count, 1)

    def test_static_test_repair_happens_before_freeze_without_generating_implementation(self):
        with project() as root:
            modes = []
            def llm(system, user, **options):
                modes.append(options["json_mode"])
                if options["json_mode"]:
                    return METADATA
                path = re.search(r"tests/components/Sample_[a-f0-9]+\.py", system).group()
                source = "import unittest\nclass ComponentTests(unittest.TestCase):\n    pass\n" if len(modes) == 2 else TEST_SOURCE
                return encode_source_artifact(path, source)
            _, descriptor, source = self.prepare(llm, root)
            self.assertEqual(source, TEST_SOURCE)
            self.assertEqual(read_frozen_test(root, descriptor), TEST_SOURCE)
            self.assertEqual(modes, [True, False, False])



    def test_permission_rejection_is_classified_by_test_evidence(self):
        self.assertEqual(classify_failures(["[AIPOD_SAMPLE_REJECTED] PermissionError"]), "authorization_fixture")
        self.assertEqual(classify_failures(["Traceback: expected valid admin to succeed; PermissionError"]), "runtime")
        self.assertEqual(classify_failures(["[AIPOD_TEST_SETUP] unknown SDK method"]), "test_setup")

    def test_public_test_command_uses_the_saved_oracle_and_returns_failure_for_regression(self):
        with project() as root:
            _, descriptor, _ = self.prepare(test_llm, root)
            bean = {"id": "Sample", "category": "model", "class_path": "modules.models.sample.Sample",
                    "file": "sample.py", "dependencies": [], "component_test": descriptor}
            registry = json.loads((root / "beans_config.json").read_text())
            registry["beans"].append(bean)
            (root / "beans_config.json").write_text(json.dumps(registry))
            target = root / "modules/models/sample.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            environment = {"PYTHONPATH": str(Path(__file__).resolve().parents[1]), "PYTHONDONTWRITEBYTECODE": "1"}
            for source, expected in ((SOURCE, 0), (SOURCE.replace("= 1", "= 2"), 1)):
                target.write_text(source)
                run = subprocess.run([sys.executable, "-m", "ai_pod_cli.component_tests", "--project-root", str(root), "--component", "Sample"],
                                     capture_output=True, text=True, env=environment, timeout=30)
                self.assertEqual(run.returncode, expected, run.stdout + run.stderr)
                report = json.loads(run.stdout)
                self.assertEqual(report["status"], "passed" if expected == 0 else "failed")
                self.assertEqual(read_frozen_test(root, descriptor), TEST_SOURCE)


if __name__ == "__main__":
    unittest.main()
