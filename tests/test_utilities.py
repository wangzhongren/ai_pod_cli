"""Real method-case execution, ordinary imports, drift, and publication safety."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_pod_cli.utilities import list_utilities, read_utility, write_utility


SOURCE = textwrap.dedent('''
    class NumberUtils:
        """Small numeric helpers."""
        @staticmethod
        def clamp(value: float, lower: float, upper: float) -> float:
            """Keep a value inside an inclusive interval."""
            if lower > upper:
                raise ValueError("Invalid interval")
            return max(lower, min(value, upper))
''')
CASES = [
    {"method": "clamp", "args": [8, 0, 5], "expected": 5},
    {"method": "clamp", "kwargs": {"value": -2, "lower": 0, "upper": 5}, "expected": 0},
    {"method": "clamp", "args": [1, 3, 2], "raises": "ValueError"},
]


class UtilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aipod-utils-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def create(self, **kwargs):
        return write_utility("NumberUtils", SOURCE, "Clamp a numeric value", CASES, project_root=self.root, **kwargs)

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file() and path.name != ".utility_registry.lock"}

    def test_registration_extracts_real_methods_and_supports_ordinary_imports(self):
        record = self.create()
        self.assertEqual(record["operation"], "created")
        self.assertEqual((record["language"], record["path"], record["symbol"]),
                         ("python", "modules/utils/numberutils.py", "NumberUtils"))
        method = record["methods"][0]
        self.assertEqual(method["name"], "clamp")
        self.assertEqual(method["signature"], "NumberUtils.clamp(value: float, lower: float, upper: float) -> float")
        self.assertIn("inclusive interval", method["doc"])
        self.assertEqual([item["name"] for item in method["parameters"]], ["value", "lower", "upper"])
        self.assertEqual(read_utility("NumberUtils", self.root)["source"], SOURCE)
        self.assertEqual(len(list_utilities(self.root, "inclusive")), 1)
        self.assertEqual(list_utilities(self.root, "does-not-exist"), [])
        self.assertFalse((self.root / "beans_config.json").exists())
        result = subprocess.run([sys.executable, "-c", "from modules.utils.numberutils import NumberUtils; assert NumberUtils.clamp(9,0,2)==2"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_read_reports_importing_callers_without_changing_them(self):
        self.create()
        caller = self.root / "modules/services/example.py"
        caller.parent.mkdir(parents=True)
        source = "from modules.utils.numberutils import NumberUtils\nvalue = NumberUtils.clamp(2, 0, 1)\n"
        caller.write_text(source)
        self.assertEqual(read_utility("NumberUtils", self.root)["callers"], [
            {"path": "modules/services/example.py", "lines": [1]},
        ])
        self.assertEqual(caller.read_text(), source)

    def test_read_only_queries_do_not_create_project_or_lock_files(self):
        missing = self.root / "missing-project"
        self.assertEqual(list_utilities(missing), [])
        self.assertFalse(missing.exists())
        with self.assertRaisesRegex(ValueError, "not registered"):
            read_utility("NumberUtils", missing)
        self.assertFalse(missing.exists())
        self.assertEqual(list_utilities(self.root), [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.create()
        (self.root / ".utility_registry.lock").unlink()
        before = self.snapshot()
        self.assertEqual(read_utility("NumberUtils", self.root)["source"], SOURCE)
        self.assertEqual(len(list_utilities(self.root)), 1)
        self.assertFalse((self.root / ".utility_registry.lock").exists())
        self.assertEqual(self.snapshot(), before)

    @unittest.skipIf(os.name == "nt", "POSIX read-only lock check")
    def test_existing_lock_can_be_opened_read_only_for_shared_queries(self):
        self.create()
        lock = self.root / ".utility_registry.lock"
        lock.chmod(0o444)
        self.root.chmod(0o555)
        try:
            self.assertEqual(len(list_utilities(self.root)), 1)
            self.assertEqual(read_utility("NumberUtils", self.root)["id"], "NumberUtils")
        finally:
            self.root.chmod(0o755)
            lock.chmod(0o644)

    def test_failed_cases_do_not_publish_source_or_registry(self):
        bad = [{"method": "clamp", "args": [8, 0, 5], "expected": 900}]
        with self.assertRaisesRegex(ValueError, "verification failed"):
            write_utility("NumberUtils", SOURCE, "Clamp", bad, project_root=self.root)
        self.assertFalse((self.root / "modules/utils/numberutils.py").exists())
        self.assertFalse((self.root / "utility_registry.json").exists())

    def test_nested_boolean_does_not_match_expected_integer(self):
        source = "class ExampleUtils:\n    @staticmethod\n    def value(): return {'value': True}\n"
        with self.assertRaisesRegex(ValueError, "expected value"):
            write_utility("ExampleUtils", source, "Example", [{"method": "value", "expected": {"value": 1}}], project_root=self.root)

    def test_case_errors_do_not_echo_argument_or_exception_values(self):
        source = "class ExampleUtils:\n    @staticmethod\n    def value(text): raise ValueError(text)\n"
        secret = "never-echo-this-test-value"
        with self.assertRaises(ValueError) as raised:
            write_utility("ExampleUtils", source, "Example", [{"method": "value", "args": [secret], "expected": 1}], project_root=self.root)
        self.assertNotIn(secret, str(raised.exception))

    def test_all_public_methods_require_real_cases_and_instance_methods_are_rejected(self):
        extra = SOURCE + "\n    @staticmethod\n    def absolute(value: float) -> float:\n        return abs(value)\n"
        with self.assertRaisesRegex(ValueError, "every public method"):
            write_utility("NumberUtils", extra, "Numbers", CASES, project_root=self.root)
        with self.assertRaisesRegex(ValueError, "staticmethod"):
            write_utility("NumberUtils", SOURCE.replace("    @staticmethod\n", ""), "Numbers", CASES, project_root=self.root)
        with self.assertRaisesRegex(ValueError, "nonempty"):
            write_utility("NumberUtils", SOURCE, "Numbers", [], project_root=self.root)

    def test_forbidden_runtime_io_dynamic_import_and_async_patterns_are_rejected(self):
        for prefix, expression in (
            ("import os\n", "os.getcwd()"),
            ("from ai_pod_cli.context import PipelineContext\n", "1"),
            ("from modules.services.private import Service\n", "1"),
            ("", "open('not-allowed')"),
            ("", "__import__('os')"),
            ("", "getattr(value, 'execute')()"),
        ):
            with self.subTest(prefix=prefix, expression=expression):
                source = prefix + f"class ExampleUtils:\n    @staticmethod\n    def value(value=1): return {expression}\n"
                with self.assertRaises(ValueError):
                    write_utility("ExampleUtils", source, "Example", [{"method": "value", "expected": 1}], project_root=self.root)
        with self.assertRaises(ValueError):
            write_utility("NumberUtils", SOURCE.replace("def clamp", "async def clamp"), "Numbers", CASES, project_root=self.root)

    def test_method_cases_reject_mutating_the_callers_inputs(self):
        source = "class ExampleUtils:\n    @staticmethod\n    def append(values):\n        values.append(2)\n        return values\n"
        with self.assertRaisesRegex(ValueError, "mutated its caller input"):
            write_utility("ExampleUtils", source, "Append", [{"method": "append", "args": [[1]], "expected": [1, 2]}], project_root=self.root)

    def test_identical_source_is_idempotent_and_default_update_is_rejected(self):
        first = self.create()
        before = self.snapshot()
        second = self.create()
        self.assertEqual(second["operation"], "reused")
        self.assertEqual(second["sha256"], first["sha256"])
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(ValueError, "already exists"):
            write_utility("NumberUtils", SOURCE + "\n", "Numbers", CASES, project_root=self.root)
        self.assertEqual(self.snapshot(), before)

    def test_raw_source_hash_including_line_endings_is_stable(self):
        source = SOURCE.replace("\n", "\r\n")
        record = write_utility("NumberUtils", source, "Numbers", CASES, project_root=self.root)
        self.assertEqual(read_utility("NumberUtils", self.root)["source"], source)
        self.assertEqual(read_utility("NumberUtils", self.root)["sha256"], record["sha256"])

    def test_hash_drift_cannot_be_silently_registered_or_read(self):
        self.create()
        path = self.root / "modules/utils/numberutils.py"
        path.write_text(SOURCE + "\n# externally edited\n")
        changed = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "hash drift"):
            read_utility("NumberUtils", self.root)
        with self.assertRaisesRegex(ValueError, "hash drift"):
            self.create()
        self.assertEqual(path.read_bytes(), changed)

    def test_unregistered_path_and_duplicate_symbol_or_casefolded_path_are_rejected(self):
        path = self.root / "modules/utils/numberutils.py"
        path.parent.mkdir(parents=True)
        path.write_text("# User-owned unregistered source\n")
        with self.assertRaisesRegex(ValueError, "occupied"):
            self.create()
        self.assertEqual(path.read_text(), "# User-owned unregistered source\n")
        path.unlink()
        self.create()
        with self.assertRaises(ValueError):
            write_utility("OtherUtils", SOURCE, "Wrong class", CASES, project_root=self.root)
        with self.assertRaisesRegex(ValueError, "occupied"):
            write_utility("NUMBERUTILS", SOURCE.replace("NumberUtils", "NUMBERUTILS"), "Duplicate path", CASES, project_root=self.root)

    def test_symlinked_utility_directory_and_registry_are_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            (self.root / "modules").mkdir()
            (self.root / "modules/utils").symlink_to(other, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                self.create()
            (self.root / "modules/utils").unlink()
            outside = Path(other) / "registry.json"
            outside.write_text('{"schema_version":1,"utilities":[]}')
            (self.root / "utility_registry.json").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                list_utilities(self.root)

    def test_registered_dependencies_are_checked_and_cycles_rejected(self):
        self.create()
        derived = textwrap.dedent('''
            from modules.utils.numberutils import NumberUtils
            class PercentUtils:
                @staticmethod
                def cap(value: float) -> float:
                    return NumberUtils.clamp(value, 0, 100)
        ''')
        second = write_utility("PercentUtils", derived, "Percent range", [{"method": "cap", "args": [150], "expected": 100}], project_root=self.root)
        self.assertEqual(second["dependencies"], ["NumberUtils"])
        first = read_utility("NumberUtils", self.root)
        cycle = "from modules.utils.percentutils import PercentUtils\n" + SOURCE
        with self.assertRaisesRegex(ValueError, "cycle"):
            write_utility("NumberUtils", cycle, "Cycle", CASES, first["sha256"], self.root, allow_update=True, verify_command=["{python}", "-c", "pass"])

    def test_updates_require_current_hash_command_and_keep_old_cases(self):
        first = self.create()
        before = self.snapshot()
        changed = SOURCE.replace("max(lower, min(value, upper))", "min(upper, max(lower, value))")
        for expected, command in ((None, ["{python}", "-c", "pass"]), ("wrong", ["{python}", "-c", "pass"]), (first["sha256"], None)):
            with self.subTest(expected=expected, command=command), self.assertRaises(ValueError):
                write_utility("NumberUtils", changed, "Numbers", CASES, expected, self.root, allow_update=True, verify_command=command)
        bad = SOURCE.replace("max(lower, min(value, upper))", "-100")
        with self.assertRaisesRegex(ValueError, "verification failed"):
            write_utility("NumberUtils", bad, "Numbers", [{"method": "clamp", "args": [8, 0, 5], "expected": -100}], first["sha256"], self.root, allow_update=True, verify_command=["{python}", "-c", "pass"])
        self.assertEqual(self.snapshot(), before)

    def test_update_verifies_candidate_source_registry_and_callers_in_isolated_project(self):
        first = self.create()
        caller = self.root / "caller.py"
        caller.write_text("from modules.utils.numberutils import NumberUtils\nassert NumberUtils.clamp(20,0,10)==10\n")
        check = self.root / "check_project.py"
        check.write_text(textwrap.dedent('''
            import hashlib, json
            from pathlib import Path
            import caller
            registry = json.loads(Path('utility_registry.json').read_text())
            entry = registry['utilities'][0]
            source = Path(entry['path']).read_bytes()
            assert hashlib.sha256(source).hexdigest() == entry['sha256']
            assert b'min(upper, max(lower, value))' in source
            Path('verification-ran').touch()
        '''))
        original_caller = caller.read_bytes()
        updated = write_utility("NumberUtils", SOURCE.replace("max(lower, min(value, upper))", "min(upper, max(lower, value))"),
                                "Numbers", CASES, first["sha256"], self.root, allow_update=True, verify_command=["{python}", "check_project.py"])
        self.assertEqual(updated["operation"], "updated")
        self.assertEqual(updated["cases"][:len(first["cases"])], first["cases"])
        self.assertFalse((self.root / "verification-ran").exists())
        self.assertEqual(caller.read_bytes(), original_caller)

    def test_failed_project_verification_rolls_back_source_and_registry(self):
        first = self.create()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "project verification failed"):
            write_utility("NumberUtils", SOURCE + "\n", "Numbers", CASES, first["sha256"], self.root,
                          allow_update=True, verify_command=["{python}", "-c", "raise SystemExit(1)"])
        self.assertEqual(self.snapshot(), before)

    def test_project_verification_cannot_change_frozen_candidate_callers(self):
        first = self.create()
        (self.root / "caller.py").write_text("from modules.utils.numberutils import NumberUtils\n")
        before = self.snapshot()
        command = ["{python}", "-c", "from pathlib import Path; Path('caller.py').write_text('pass')"]
        with self.assertRaisesRegex(ValueError, "changed candidate source"):
            write_utility("NumberUtils", SOURCE + "\n", "Numbers", CASES, first["sha256"], self.root,
                          allow_update=True, verify_command=command)
        self.assertEqual(self.snapshot(), before)

    def test_publication_failure_restores_original_source_and_registry(self):
        first = self.create()
        before = self.snapshot()
        original = os.replace
        failed = False
        def replace_once(source, target):
            nonlocal failed
            if Path(target) == self.root / "utility_registry.json" and not failed:
                failed = True
                raise OSError("injected publication error")
            return original(source, target)
        with patch("ai_pod_cli.utilities.os.replace", side_effect=replace_once), self.assertRaises(OSError):
            write_utility("NumberUtils", SOURCE + "\n", "Numbers", CASES, first["sha256"], self.root,
                          allow_update=True, verify_command=["{python}", "-c", "pass"])
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_creates_do_not_lose_registry_entries(self):
        command = """
import json, sys, time
from pathlib import Path
from ai_pod_cli.utilities import write_utility
p=json.load(sys.stdin)
root=Path(sys.argv[1])
(root / ('ready-' + p['id'])).touch()
while len(list(root.glob('ready-*'))) < 2:
    time.sleep(0.01)
print(write_utility(p['id'],p['source'],'Identity helper',[{'method':'identity','args':[1],'expected':1}],project_root=sys.argv[1])['operation'])
"""
        processes = []
        for identifier in ("FirstUtils", "SecondUtils"):
            source = f"class {identifier}:\n    @staticmethod\n    def identity(value: int) -> int: return value\n"
            process = subprocess.Popen([sys.executable, "-c", command, str(self.root)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            process.stdin.write(json.dumps({"id": identifier, "source": source}))
            process.stdin.close()
            process.stdin = None
            processes.append(process)
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout.strip(), "created")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assertEqual({item["id"] for item in list_utilities(self.root)}, {"FirstUtils", "SecondUtils"})


if __name__ == "__main__":
    unittest.main()
