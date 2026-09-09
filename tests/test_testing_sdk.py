"""Real isolated component tests for fixtures, calls, proof, and invalid test gates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_pod_cli.component_tests import validate_component_test_source, verify_component_test
from ai_pod_cli.sandbox import verify_component_candidate
from ai_pod_cli.testing import Sandbox, TestSetupError


GUARD = textwrap.dedent('''
    from injector import inject
    from ai_pod_cli.config_store import ConfigStore
    from ai_pod_cli.repository import ModelRepository
    from modules.models.account import Account
    from modules.models.record import Record
    from modules.providers.clock import Clock
    class Guard:
        @inject
        def __init__(self, repository: ModelRepository, config: ConfigStore, clock: Clock):
            self.repo, self.config, self.clock = repository, config, clock
        def execute(self, ctx):
            actor = ctx.get("actor")
            if not isinstance(actor, str) or not actor.strip():
                raise PermissionError("identity required")
            users = self.repo.find(Account, username=actor)
            account = next(iter(users), None)
            if account is None or not account.is_active or account.role != "writer":
                raise PermissionError("writer required")
            title = ctx.get("title")
            if len(title) > self.config.get("app.max_length", 20):
                raise ValueError("title too long")
            item = self.repo.save(Record(title=title, tick=self.clock.now()))
            ctx.set("record", item)
            return {"record": item}
''')


def test_source(methods, extra=""):
    return ("import unittest\nfrom ai_pod_cli.testing import Sandbox, TestSetupError\n" + extra
            + "\nclass ComponentTests(unittest.TestCase):\n" + textwrap.indent(textwrap.dedent(methods), "    "))


class TestingSDKTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aipod-sdk-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for directory in ("modules/models", "modules/providers", "modules/services"):
            (self.root / directory).mkdir(parents=True)
            (self.root / directory / "__init__.py").touch()
        (self.root / "modules/__init__.py").touch()
        (self.root / "modules/models/account.py").write_text(textwrap.dedent('''
            from typing import Optional
            from ai_pod_cli import Model
            from sqlmodel import Field
            class Account(Model, table=True):
                id: Optional[int] = Field(default=None, primary_key=True)
                username: str
                role: str = "reader"
                is_active: bool = True
        '''))
        (self.root / "modules/models/record.py").write_text(textwrap.dedent('''
            from typing import Optional
            from ai_pod_cli import Model
            from sqlmodel import Field
            class Record(Model, table=True):
                id: Optional[int] = Field(default=None, primary_key=True)
                title: str
                tick: int
        '''))
        (self.root / "modules/providers/clock.py").write_text("class Clock:\n    def now(self): return 9\n")
        (self.root / "modules/services/guard.py").write_text(GUARD)
        self.bean = {"id": "Guard", "category": "service", "file": "guard.py",
                     "class_path": "modules.services.guard.Guard",
                     "dependencies": ["ModelRepository", "ConfigStore", "Clock"],
                     "inputs": {"actor": {"type": "str", "required": False}, "title": "str"},
                     "outputs": {"record": {"model": "modules.models.record.Record"}}}
        self.beans = [
            {"id": "ConfigStore", "category": "provider", "class_path": "ai_pod_cli.config_store.ConfigStore", "file": "config_store.py"},
            {"id": "ModelRepository", "category": "provider", "class_path": "ai_pod_cli.repository.ModelRepository", "file": "repository.py", "dependencies": ["ConfigStore"]},
            {"id": "Account", "category": "model", "class_path": "modules.models.account.Account", "file": "account.py"},
            {"id": "Record", "category": "model", "class_path": "modules.models.record.Record", "file": "record.py"},
            {"id": "Clock", "category": "provider", "class_path": "modules.providers.clock.Clock", "file": "clock.py", "methods": {"now": {"inputs": {}, "outputs": "int"}}},
            self.bean,
        ]
        (self.root / "beans_config.json").write_text(json.dumps({"beans": self.beans}))
        (self.root / "config.toml").write_text('[database]\nurl="sqlite:///original.data"\n[app]\nmax_length=0\n')
        db = sqlite3.connect(self.root / "original.data")
        try:
            with db:
                db.execute("CREATE TABLE protected(value TEXT)")
                db.execute("INSERT INTO protected VALUES ('not part of test data')")
        finally:
            db.close()
        self.original_hash = hashlib.sha256((self.root / "original.data").read_bytes()).hexdigest()

    def verify(self, tests, source=GUARD, bean=None, required=None):
        return verify_component_test(self.root, bean or self.bean, source, tests,
                                     timeout=8, required_tests=required)

    def model_bean(self):
        return {"id": "Number", "category": "model", "file": "number.py",
                "class_path": "modules.models.number.Number", "dependencies": []}

    def test_sdk_is_unavailable_outside_owned_worker(self):
        with self.assertRaises(TestSetupError):
            Sandbox()

    def test_explicit_positive_and_permission_denial_preserve_database(self):
        tests = test_source('''
            def denied(self, username=None):
                with Sandbox() as s:
                    if username:
                        s.seed("Account", {"username": username, "role": "reader"})
                    before = s.snapshot()
                    params = {"title": "case"}
                    if username: params["actor"] = username
                    with self.assertRaises(PermissionError):
                        s.run("Guard", params)
                    self.assertEqual(s.snapshot(), before)
            def test_denied(self):
                self.denied("reader")
            def test_missing(self):
                self.denied()
            def test_success(self):
                with Sandbox() as s:
                    self.assertEqual(s.count("Account"), 0)
                    s.seed("Account", {"username": "author", "role": "writer"})
                    result = s.run("Guard", {"actor": "author", "title": "created"})
                    self.assertEqual(result["record"].title, "created")
                    self.assertEqual(s.count("Record"), 1)
                    self.assertEqual(s.rows("Record")[0]["tick"], 9)
        ''')
        self.assertEqual(self.verify(tests, required=["ComponentTests.test_denied", "ComponentTests.test_success"]), [])
        self.assertEqual(hashlib.sha256((self.root / "original.data").read_bytes()).hexdigest(), self.original_hash)

    def test_explicit_configuration_and_dependency_fake_are_used(self):
        tests = test_source('''
            def test_dependency(self):
                with Sandbox(config={"app": {"max_length": 3}}, provider_overrides={"Clock": FakeClock()}) as s:
                    s.seed("Account", {"username": "author", "role": "writer"})
                    result = s.run("Guard", {"actor": "author", "title": "ok"})
                    self.assertEqual(result["record"].tick, 42)
                    with self.assertRaises(ValueError):
                        s.run("Guard", {"actor": "author", "title": "too long"})
        ''', "class FakeClock:\n    def now(self): return 42\n")
        self.assertEqual(self.verify(tests), [])

    def test_expected_model_validation_error_counts_real_target_execution(self):
        source = "from ai_pod_cli import Model\nclass Number(Model):\n    value: int\n"
        tests = test_source('''
            def test_valid(self):
                with Sandbox() as s:
                    value = s.model("Number", {"value": 3})
                    self.assertEqual(value.value, 3)
            def test_invalid(self):
                with Sandbox() as s:
                    with self.assertRaises(ValidationError):
                        s.model("Number", {"value": "not an integer"})
        ''', "from pydantic import ValidationError\n")
        self.assertEqual(self.verify(tests, source, self.model_bean()), [])

    def test_provider_calls_and_per_test_module_state_are_real_and_isolated(self):
        bean = {"id": "Counter", "category": "provider", "file": "counter.py",
                "class_path": "modules.providers.counter.Counter", "methods": {"next": {"inputs": {}, "outputs": "int"}}}
        source = "calls = 0\nclass Counter:\n    def next(self):\n        global calls\n        calls += 1\n        return calls\n"
        tests = test_source('''
            def test_first(self):
                with Sandbox() as s:
                    value = s.call_provider("Counter", "next")
                    self.assertEqual(value, 1)
            def test_second(self):
                with Sandbox() as s:
                    value = s.call_provider("Counter", "next")
                    self.assertEqual(value, 1)
        ''')
        self.assertEqual(self.verify(tests, source, bean), [])

    def test_service_framework_failure_is_not_silently_accepted(self):
        source = "from ai_pod_cli.result import Failure\nclass Guard:\n    def execute(self, ctx): return Failure('unavailable')\n"
        tests = test_source('''
            def test_success(self):
                with Sandbox() as s:
                    result = s.run("Guard", {"title": "case"})
                    self.assertIsNotNone(result)
        ''')
        errors = self.verify(tests, source)
        self.assertTrue(errors)
        self.assertIn("Service returned Failure", "\n".join(errors))

    def test_provider_typo_is_setup_but_missing_declared_implementation_is_runtime_failure(self):
        bean = next(item for item in self.beans if item["id"] == "Clock")
        tests = test_source('''
            def test_call(self):
                with Sandbox() as s:
                    value = s.call_provider("Clock", "misspelled")
                    self.assertEqual(value, 9)
        ''')
        errors = self.verify(tests, "class Clock:\n    def now(self): return 9\n", bean)
        self.assertTrue(any("[AIPOD_TEST_SETUP]" in error for error in errors), errors)
        errors = self.verify(tests.replace('"misspelled"', '"now"'), "class Clock:\n    pass\n", bean)
        self.assertTrue(errors)
        self.assertTrue(any("AttributeError" in error for error in errors), errors)
        self.assertFalse(any("[AIPOD_TEST_SETUP]" in error for error in errors), errors)

    def test_unknown_sdk_method_or_argument_is_setup_error(self):
        for expression in ('s.lookup("Guard")', 's.run("Guard", {}, bogus=True)', 's.seed("Account")'):
            with self.subTest(expression=expression):
                tests = test_source(f'''\n                    def test_case(self):\n                        with Sandbox() as s:\n                            result = {expression}\n                            self.assertIsNotNone(result)\n                ''')
                errors = validate_component_test_source(tests)
                self.assertTrue(any("[AIPOD_TEST_SETUP]" in error for error in errors), errors)

    def test_invalid_fixture_is_setup_error_not_permission_policy_repair(self):
        tests = test_source('''
            def test_case(self):
                with Sandbox() as s:
                    s.seed("Account", {})
                    result = s.run("Guard", {"title": "case"})
                    self.assertIsNotNone(result)
        ''')
        errors = self.verify(tests)
        self.assertTrue(any("[AIPOD_TEST_SETUP]" in error for error in errors), errors)

    def test_target_service_model_and_database_providers_cannot_be_overridden(self):
        for identifier in ("Guard", "Account", "ModelRepository", "ConfigStore"):
            with self.subTest(identifier=identifier):
                tests = test_source(f'''\n                    def test_case(self):\n                        with Sandbox(provider_overrides={{{identifier!r}: object()}}) as s:\n                            result = s.run("Guard", {{"title": "case"}})\n                            self.assertIsNotNone(result)\n                ''')
                errors = self.verify(tests)
                self.assertTrue(any("[AIPOD_TEST_SETUP]" in error for error in errors), errors)

    def test_patch_string_target_class_or_injector_fake_cannot_provide_execution_evidence(self):
        fake = 'from unittest.mock import patch\nclass Fake:\n    def execute(self, ctx): return {"record": {"title": "fake"}}\n'
        for substitution in ('patch("modules.services.guard.Guard", Fake)', 'patch("injector.Injector.get", return_value=Fake())'):
            with self.subTest(substitution=substitution):
                tests = test_source(f'''\n                    def test_replaced(self):\n                        with {substitution}:\n                            with Sandbox() as s:\n                                result = s.run("Guard", {{"title": "fake"}})\n                                self.assertIsNotNone(result)\n                ''', fake)
                errors = self.verify(tests)
                self.assertTrue(any("[AIPOD_TEST_SETUP]" in error for error in errors), errors)
                self.assertTrue(any("cannot be replaced" in error for error in errors), errors)

    def test_empty_constant_and_direct_import_tests_fail_before_freezing(self):
        for tests in (
            test_source('def test_empty(self):\n    pass\n'),
            test_source('def test_constant(self):\n    with Sandbox() as s:\n        s.run("Guard", {"title":"case"})\n        self.assertTrue(True)\n'),
            test_source('def test_direct(self):\n    self.assertEqual(Guard.__name__, "Guard")\n', 'from modules.services.guard import Guard\n'),
        ):
            self.assertTrue(validate_component_test_source(tests))

    def test_missing_required_test_cannot_be_replaced_by_another(self):
        tests = test_source('''
            def test_other(self):
                with Sandbox() as s:
                    value = s.model("Account", {"username": "example"})
                    self.assertEqual(value.username, "example")
        ''')
        self.assertTrue(self.verify(tests, required=["ComponentTests.test_required"]))

    def test_calling_only_a_dependency_is_not_target_evidence(self):
        tests = test_source('''
            def test_other(self):
                with Sandbox() as s:
                    value = s.call_provider("Clock", "now")
                    self.assertEqual(value, 9)
        ''')
        errors = self.verify(tests)
        self.assertTrue(any("[AIPOD_TEST_INVALID]" in error for error in errors), errors)

    def test_caught_assertion_failure_cannot_be_erased_by_a_later_success(self):
        tests = test_source('''
            def test_swallowed(self):
                with Sandbox() as s:
                    value = s.model("Number", {"value": 1})
                    try:
                        self.assertEqual(value.value, 2)
                    except AssertionError:
                        pass
                    self.assertEqual(value.value, 1)
        ''')
        errors = self.verify(tests, "from ai_pod_cli import Model\nclass Number(Model):\n    value: int\n", self.model_bean())
        self.assertTrue(any("[AIPOD_TEST_INVALID]" in error for error in errors), errors)

    def test_caught_failed_assert_raises_context_is_not_a_pass(self):
        tests = test_source('''
            def test_swallowed(self):
                with Sandbox() as s:
                    try:
                        with self.assertRaises(PermissionError):
                            value = s.model("Number", {"value": 1})
                    except AssertionError:
                        pass
                    self.assertEqual(value.value, 1)
        ''')
        errors = self.verify(tests, "from ai_pod_cli import Model\nclass Number(Model):\n    value: int\n", self.model_bean())
        self.assertTrue(any("[AIPOD_TEST_INVALID]" in error for error in errors), errors)

    def test_skipped_test_cannot_pass_a_declared_requirement(self):
        tests = test_source('''
            @unittest.skip("not implemented")
            def test_skipped(self):
                with Sandbox() as s:
                    value = s.model("Number", {"value": 1})
                    self.assertEqual(value.value, 1)
        ''')
        errors = self.verify(tests, "from ai_pod_cli import Model\nclass Number(Model):\n    value: int\n", self.model_bean())
        self.assertTrue(any("[AIPOD_TEST_INVALID]" in error for error in errors), errors)

    def test_candidate_registry_and_frozen_test_mutation_are_rejected(self):
        for relative in ("beans_config.json", "modules/models/number.py", "_aipod_component_tests.py"):
            with self.subTest(relative=relative):
                tests = test_source(f'''\n                    def test_mutation(self):\n                        with Sandbox() as s:\n                            value = s.model("Number", {{"value": 1}})\n                            self.assertEqual(value.value, 1)\n                            Path({relative!r}).write_text("changed")\n                ''', "from pathlib import Path\n")
                errors = self.verify(tests, "from ai_pod_cli import Model\nclass Number(Model):\n    value: int\n", self.model_bean())
                self.assertTrue(any("[AIPOD_TEST_INVALID]" in error for error in errors), errors)

    def test_temporary_resource_path_is_available_and_original_environment_is_not(self):
        tests = test_source('''
            def test_resource(self):
                with Sandbox() as s:
                    value = s.model("Number", {"value": 1})
                    resource = s.path("input.txt")
                    resource.write_text("explicit fixture")
                    self.assertEqual(resource.read_text(), "explicit fixture")
                    self.assertEqual(value.value, 1)
                    self.assertIsNone(os.environ.get("AIPOD_TEST_REAL_ENV_SECRET"))
                    self.assertFalse(Path("original.data").exists())
        ''', "import os\nfrom pathlib import Path\n")
        with patch.dict("os.environ", {"AIPOD_TEST_REAL_ENV_SECRET": "not forwarded"}):
            self.assertEqual(self.verify(tests, "from ai_pod_cli import Model\nclass Number(Model):\n    value: int\n", self.model_bean()), [])
        self.assertFalse((self.root / "input.txt").exists())

    def test_external_socket_and_subprocess_attempts_are_blocked_even_when_caught(self):
        for action in ('socket.create_connection(("example.invalid", 80))', 'socket.socket().bind(("127.0.0.1", 0))', 'subprocess.run(["unexpected-program"])'):
            with self.subTest(action=action):
                tests = test_source(f'''\n                    def test_external(self):\n                        with Sandbox() as s:\n                            value = s.model("Number", {{"value": 1}})\n                            with self.assertRaises(TestSetupError):\n                                {action}\n                            self.assertEqual(value.value, 1)\n                ''', "import socket, subprocess\n")
                errors = self.verify(tests, "from ai_pod_cli import Model\nclass Number(Model):\n    value: int\n", self.model_bean())
                self.assertTrue(any("[AIPOD_TEST_SETUP]" in error for error in errors), errors)

    def test_external_database_connection_is_blocked_before_open(self):
        tests = test_source(f'''\n            def test_external_database(self):\n                with Sandbox() as s:\n                    value = s.model("Number", {{"value": 1}})\n                    with self.assertRaises(TestSetupError):\n                        sqlite3.connect({str(self.root / 'original.data')!r})\n                    self.assertEqual(value.value, 1)\n        ''', "import sqlite3\n")
        errors = self.verify(tests, "from ai_pod_cli import Model\nclass Number(Model):\n    value: int\n", self.model_bean())
        self.assertTrue(any("[AIPOD_TEST_SETUP]" in error for error in errors), errors)
        self.assertEqual(hashlib.sha256((self.root / "original.data").read_bytes()).hexdigest(), self.original_hash)

    def test_parent_cwd_and_private_model_modules_remain_unchanged(self):
        current = Path.cwd()
        paths = list(sys.path)
        sentinel = types.ModuleType("modules.models.account")
        tests = test_source('''
            def test_denied(self):
                with Sandbox() as s:
                    with self.assertRaises(PermissionError):
                        s.run("Guard", {"title": "case"})
        ''')
        with patch.dict(sys.modules, {"modules.models.account": sentinel}):
            self.assertEqual(self.verify(tests), [])
            self.assertIs(sys.modules["modules.models.account"], sentinel)
        self.assertEqual(Path.cwd(), current)
        self.assertEqual(sys.path, paths)

    def test_synthetic_smoke_permission_denial_is_explicitly_inconclusive(self):
        errors = verify_component_candidate(self.root, self.bean, GUARD, [])
        self.assertTrue(any("[AIPOD_SAMPLE_REJECTED]" in error for error in errors), errors)
        self.assertEqual(hashlib.sha256((self.root / "original.data").read_bytes()).hexdigest(), self.original_hash)


if __name__ == "__main__":
    unittest.main()
