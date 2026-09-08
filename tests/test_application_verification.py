"""Application acceptance requires executed behavior evidence, never smoke alone."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_pod_cli.config import init_config_if_not_exists
from ai_pod_cli.pod.state import (
    APPLICATION_PROOF_VERSION, default_interface_verification,
    load_and_upgrade_plan, normalize_interface_plan, save_decision_plan,
)
from ai_pod_cli.pod.verification import (
    _application_verification_issues, _project_verification_fingerprint,
    _repair_current_artifact, _verify_application,
)


def driver_proof():
    return {
        "schema": APPLICATION_PROOF_VERSION, "status": "passed",
        "tests_run": 2, "assertions": 3, "route_calls": 2,
        "tests": [
            {"id": "acceptance.EngineTests.test_launch", "outcome": "passed", "assertions": 1, "route_calls": 1},
            {"id": "acceptance.EngineTests.test_collision", "outcome": "passed", "assertions": 2, "route_calls": 1},
        ],
        "aipod_behavior_proof": {
            "version": 1, "tests_run": 2, "assertions": 3,
            "route_calls": 2, "success": True,
        },
    }


def verification_result(command=None, *, passed=True, stdout="", stderr="", files=()):
    return {
        "status": "unverified" if command is None else "passed" if passed else "failed",
        "checks": {
            "structure": {"status": "passed", "issues": []},
            "execution": None if command is None else {
                "status": "passed" if passed else "failed", "exit_code": 0 if passed else 1,
                "command": command, "stdout": stdout, "stderr": stderr,
                "locations": [{"file": name, "line": 1} for name in files],
            },
        },
        "repair": {"required": not passed, "suggested_files": list(files)},
    }


class ApplicationVerificationTests(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        os.chdir(self.root)
        self.addCleanup(self._cleanup)
        self.write("checks/acceptance.py", "import unittest\n")

    def _cleanup(self):
        os.chdir(self.previous)
        self.directory.cleanup()

    def write(self, filename, content):
        target = self.root / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def behavior(self, *, required=True, path="checks/acceptance.py"):
        return {
            "name": "behavior", "kind": "behavior", "required": required,
            "command": ["{python}", "-m", "ai_pod_cli.behavior_tests", path],
            "cases": [
                {"test": "EngineTests.test_launch", "requirement": "Launching moves the ball"},
                {"test": "EngineTests.test_collision", "requirement": "Hitting a brick increases score"},
            ],
            "timeout": 10,
        }

    def smoke(self, name="smoke", *, required=True):
        return {
            "name": name, "kind": "smoke", "required": required,
            "command": ["{python}", "-c", "pass"], "timeout": 10,
        }

    def state(self, *interface_checks):
        state = load_and_upgrade_plan(None, "acceptance integration")
        for stage in state["stages"].values():
            stage["status"] = "complete"
        state["stages"]["interfaces"]["plan"] = {"interfaces": [
            {"name": f"interface-{index}", "verify": checks, "artifacts": []}
            for index, checks in enumerate(interface_checks)
        ]}
        save_decision_plan(state)
        return state

    def run_verification(self, state, runner=None):
        if runner is None:
            def runner(command, timeout):
                return verification_result(
                    command or None,
                    stdout=json.dumps(driver_proof()) if "ai_pod_cli.behavior_tests" in command else "",
                )
        with patch("ai_pod_cli.commands.verify.verify_project", side_effect=runner) as mocked:
            result = _verify_application("acceptance integration", state)
        return result, mocked

    def test_default_verification_and_legacy_plan_only_add_smoke(self):
        self.assertEqual(default_interface_verification({"name": "app"})["kind"], "smoke")
        self.assertEqual(default_interface_verification({"name": "app", "adapter": {}})["kind"], "smoke")
        plan = normalize_interface_plan({"interfaces": [{"name": "legacy"}]})
        self.assertEqual([check["kind"] for check in plan["interfaces"][0]["verify"]], ["smoke"])

    def test_smoke_and_plain_runtime_success_cannot_pass_application(self):
        runtime = {**self.smoke("runtime"), "kind": "runtime"}
        state = self.state([self.smoke(), runtime])
        result, _ = self.run_verification(state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["repair"]["action"], "replan_interfaces")
        self.assertEqual(result["checks"]["acceptance"]["issues"][0]["code"], "missing_behavior_verification")
        persisted = json.loads(Path("aipod_plan.json").read_text())
        self.assertEqual(persisted["agent"]["verification"]["required_action"], "replan_interfaces")
        self.assertEqual([check["status"] for check in result["checks"]["interfaces"]], ["passed", "passed"])

    def test_legacy_command_without_interface_behavior_is_rejected(self):
        state = self.state()
        state["agent"]["verification"]["command"] = [sys.executable, "-c", "pass"]
        result, _ = self.run_verification(state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["repair"]["action"], "replan_interfaces")

    def test_behavior_is_required_for_each_interface(self):
        state = self.state([self.behavior()], [self.smoke()])
        result, _ = self.run_verification(state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            [issue["interface"] for issue in result["checks"]["acceptance"]["issues"]],
            ["interface-1"],
        )

    def test_optional_behavior_does_not_satisfy_required_acceptance(self):
        result, _ = self.run_verification(self.state([self.smoke(), self.behavior(required=False)]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"]["acceptance"]["issues"][0]["code"], "missing_behavior_verification")

    def test_behavior_label_cannot_turn_arbitrary_command_into_acceptance(self):
        fake = {**self.behavior(), "command": ["{python}", "-c", "print('passed')"]}
        result, mocked = self.run_verification(self.state([fake]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(mocked.call_count, 1)  # Structure inspection only; do not run the fake driver.
        self.assertEqual(result["checks"]["acceptance"]["issues"][0]["code"], "invalid_behavior_verification")

    def test_behavior_target_must_exist_within_project(self):
        for path in ("checks/missing.py", "../outside.py", "checks/acceptance.txt", str(self.root / "checks/acceptance.py")):
            with self.subTest(path=path):
                state = self.state([self.behavior(path=path)])
                result, mocked = self.run_verification(state)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(mocked.call_count, 1)
        state = self.state([self.behavior(path="checks/future.py")])
        self.assertTrue(_application_verification_issues(state, require_files=False))
        state["stages"]["interfaces"]["plan"]["interfaces"][0]["artifacts"] = [
            {"path": "checks/future.py", "role": "behavior_test", "format": "python"},
        ]
        self.assertEqual(_application_verification_issues(state, require_files=False), [])

    def test_planning_requires_missing_behavior_target_as_owned_test_artifact(self):
        state = self.state([self.behavior(path="interfaces/proof/acceptance.py")])
        interface = state["stages"]["interfaces"]["plan"]["interfaces"][0]
        for artifact in (
            {"path": "interfaces/proof/acceptance.py", "role": "runtime"},
            {"path": "interfaces/proof/other.py", "role": "behavior_test"},
        ):
            with self.subTest(artifact=artifact):
                interface["artifacts"] = [artifact]
                issues = _application_verification_issues(state, require_files=False)
                self.assertEqual(issues[0]["code"], "invalid_behavior_verification")
                self.assertIn("behavior_test artifact", issues[0]["message"])
        interface["artifacts"] = [{"path": "interfaces/proof/acceptance.py", "role": "behavior_test"}]
        self.assertEqual(_application_verification_issues(state, require_files=False), [])
        # A declaration alone is sufficient only before generation; actual verification
        # still requires the artifact to exist and the real driver to execute it.
        self.assertTrue(_application_verification_issues(state))
        self.assertEqual(_application_verification_issues(self.state([self.behavior()]), require_files=False), [])

    def test_real_driver_executes_declared_route_assertion_before_application_passes(self):
        init_config_if_not_exists()
        self.write("routes.toml", '[advance]\npipeline = "pipelines/advance.py"\n')
        self.write("pipelines/advance.py", "def run(ctx):\n    return {'position': ctx.params['position'] + ctx.params['speed']}\n")
        self.write("checks/acceptance.py", (
            "import unittest\nfrom ai_pod_cli.runner import PipelineRunner\n\n"
            "class EngineTests(unittest.TestCase):\n"
            "    def test_launch(self):\n"
            "        result = PipelineRunner().run('advance', {'position': 2, 'speed': 3})\n"
            "        self.assertEqual(result['position'], 5)\n"
        ))
        behavior = self.behavior()
        behavior["cases"] = behavior["cases"][:1]
        state = self.state([behavior])
        result = _verify_application("acceptance integration", state)
        self.assertEqual(result["status"], "passed", result)
        proof = result["checks"]["interfaces"][0]["proof"]
        self.assertEqual(proof["route_calls"], 1)
        self.assertEqual(proof["tests_run"], 1)
        self.assertGreaterEqual(proof["assertions"], 1)

    def test_required_behavior_pass_preserves_optional_failure_semantics(self):
        optional = self.smoke("optional-installation", required=False)
        optional["command"][-1] = "raise SystemExit(1)"
        state = self.state([self.behavior(), optional])

        def runner(command, timeout):
            if not command:
                return verification_result()
            if "ai_pod_cli.behavior_tests" in command:
                return verification_result(command, stdout=json.dumps(driver_proof()))
            return verification_result(command, passed=False, stderr="optional unavailable", files=["optional.py"])

        result, _ = self.run_verification(state, runner)
        self.assertEqual(result["status"], "passed")
        self.assertEqual([check["status"] for check in result["checks"]["interfaces"]], ["passed", "failed"])
        self.assertEqual(result["checks"]["execution"]["exit_code"], 0)
        self.assertEqual(result["repair"]["suggested_files"], [])

    def test_required_failure_evidence_survives_later_success(self):
        state = self.state([self.behavior(), self.smoke()])

        def runner(command, timeout):
            if "ai_pod_cli.behavior_tests" in command:
                return verification_result(command, passed=False, stderr="actual score was wrong", files=["modules/scoring.py"])
            return verification_result(command or None)

        result, _ = self.run_verification(state, runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"]["execution"]["stderr"], "actual score was wrong")
        self.assertEqual(result["repair"]["suggested_files"], ["modules/scoring.py"])
        self.assertEqual(result["checks"]["interfaces"][0]["repair"]["suggested_files"], ["modules/scoring.py"])

    def test_exit_zero_requires_valid_versioned_positive_behavior_proof(self):
        bad_proofs = ["", "not json", json.dumps({}), json.dumps({**driver_proof(), "schema": "old"})]
        for key in ("tests_run", "assertions", "route_calls"):
            for value in (0, True, "1"):
                payload = driver_proof()
                payload["aipod_behavior_proof"][key] = value
                payload[key] = value
                bad_proofs.append(json.dumps(payload))
        for output in bad_proofs:
            with self.subTest(output=output):
                state = self.state([self.behavior()])

                def runner(command, timeout):
                    return verification_result(command or None, stdout=output)

                result, _ = self.run_verification(state, runner)
                self.assertEqual(result["status"], "failed")
                self.assertIn("proof_error", result["checks"]["interfaces"][0])

    def test_behavior_cases_must_be_named_nonempty_unique_requirements(self):
        invalid = (
            None, [], [{"test": "EngineTests.test_launch", "requirement": ""}],
            [{"test": "", "requirement": "Launching moves the ball"}],
            [{"test": "not_a_method", "requirement": "Launching moves the ball"}],
            self.behavior()["cases"] * 2,
        )
        for cases in invalid:
            with self.subTest(cases=cases):
                behavior = {**self.behavior(), "cases": cases}
                result, mocked = self.run_verification(self.state([behavior]))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["repair"]["action"], "replan_interfaces")
                self.assertEqual(mocked.call_count, 1)

    def test_missing_skipped_or_unasserted_declared_case_cannot_pass(self):
        missing = driver_proof()
        missing["tests"].pop()
        skipped = driver_proof()
        skipped["tests"][1]["outcome"] = "skipped"
        no_assertion = driver_proof()
        no_assertion["tests"][1]["assertions"] = 0
        no_route = driver_proof()
        no_route["tests"][1]["route_calls"] = 0
        duplicate = driver_proof()
        duplicate["tests"].append(deepcopy(duplicate["tests"][1]))
        for payload in (missing, skipped, no_assertion, no_route, duplicate):
            with self.subTest(payload=payload):
                def runner(command, timeout):
                    return verification_result(command or None, stdout=json.dumps(payload))

                result, _ = self.run_verification(self.state([self.behavior()]), runner)
                self.assertEqual(result["status"], "failed")
                self.assertIn("EngineTests.test_collision", result["checks"]["interfaces"][0]["proof_error"])

    def test_proof_version_invalidates_old_passes_and_fingerprints(self):
        state = self.state([self.behavior()])
        state["agent"]["verification"].update(status="passed", fingerprint="old")
        upgraded = load_and_upgrade_plan(state, "acceptance integration")
        self.assertEqual(upgraded["agent"]["verification"]["status"], "pending")
        original = _project_verification_fingerprint()
        with patch("ai_pod_cli.pod.verification.APPLICATION_PROOF_VERSION", "future-proof-v2"):
            self.assertNotEqual(original, _project_verification_fingerprint())
        self.write("checks/acceptance.py", "import unittest\n# changed acceptance\n")
        self.assertNotEqual(original, _project_verification_fingerprint())
        original = _project_verification_fingerprint()
        state["stages"]["interfaces"]["plan"]["interfaces"][0]["verify"][0]["cases"].pop()
        save_decision_plan(state)
        self.assertNotEqual(original, _project_verification_fingerprint())

    def test_repair_protects_acceptance_and_unittest_files_but_allows_production_assert(self):
        source = "def calculate(value):\n    assert value >= 0\n    return value + 1\n"
        production = self.write("modules/calculate.py", source)
        self.write("checks/assertions.py", "assert False\n")
        self.write("tests/probe.py", "raise AssertionError('real expectation')\n")
        self.write("validation.py", "import unittest\nclass Proof(unittest.TestCase):\n    pass\n")
        state = self.state([self.behavior()])
        state["stages"]["interfaces"]["plan"]["interfaces"][0]["artifacts"] = [
            {"path": "checks/assertions.py", "role": "assertion"},
        ]
        suggested = ["modules/calculate.py", "checks/acceptance.py", "checks/assertions.py", "tests/probe.py", "validation.py"]
        state["agent"]["verification"]["last_result"] = verification_result(
            ["behavior"], passed=False, stderr="first failure", files=suggested,
        )
        state["agent"]["verification"]["last_result"]["checks"]["interfaces"] = [
            {"name": "first", "required": True, "status": "failed", "execution": {"stderr": "first failure"}},
            {"name": "second", "required": True, "status": "failed", "execution": {"stderr": "second failure"}},
        ]
        protected_bytes = {name: Path(name).read_bytes() for name in suggested[1:]}
        save_decision_plan(state)
        with (
            patch("ai_pod_cli.pod.verification.call_llm", return_value={"patches": [{"old": "value + 1", "new": "value + 2"}]}) as model,
            patch("ai_pod_cli.pod.verification._validate_repaired_artifact", return_value=[]),
        ):
            repaired = _repair_current_artifact("acceptance integration", state)
        self.assertEqual(repaired["file"], "modules/calculate.py")
        self.assertIn("value + 2", production.read_text())
        self.assertIn("first failure", model.call_args.args[1])
        self.assertIn("second failure", model.call_args.args[1])
        for name, original in protected_bytes.items():
            self.assertEqual(Path(name).read_bytes(), original)

    def test_acceptance_only_traceback_never_invokes_model_repair(self):
        state = self.state([self.behavior()])
        state["agent"]["verification"]["last_result"] = verification_result(
            ["behavior"], passed=False, files=["checks/acceptance.py"],
        )
        with patch("ai_pod_cli.pod.verification.call_llm") as model:
            with self.assertRaisesRegex(RuntimeError, "不可修改"):
                _repair_current_artifact("acceptance integration", state)
            model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
