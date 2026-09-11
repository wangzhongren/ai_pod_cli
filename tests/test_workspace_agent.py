"""Shared-workspace tools and Pod-to-owner handoffs, with real protected shell calls."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_pod_cli.config import init_config_if_not_exists
from ai_pod_cli.pod.coordinator import PodCoordinator
from ai_pod_cli.pod.state import load_and_upgrade_plan
from ai_pod_cli.source_codec import encode_source_artifact
from ai_pod_cli.workspace import WorkspaceTools, parse_action, path_owner, LAYERS
from ai_pod_cli.workspace_agent import WorkspaceAgent


def run_python(source):
    return {"tool": "shell", "command": shlex.quote(sys.executable) + " -c " + shlex.quote(source)}


def response(action):
    return action if isinstance(action, str) else json.dumps(action)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.previous = Path.cwd()
        os.chdir(self.root)
        init_config_if_not_exists()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def protected(self, stage="services", paths=None):
        tools = WorkspaceTools(self.root, stage, paths)
        try:
            check = tools.shell("printf backend-ready")
        except RuntimeError as error:
            self.skipTest(str(error))
        if "sandbox_apply: Operation not permitted" in check["output"]:
            self.skipTest("Outer process sandbox prevents nested sandbox-exec; rerun this module with local shell permission")
        self.assertEqual(check["exit_code"], 0, check)
        tools.checks.clear(); tools.revision = 0
        return tools

    def state(self):
        state = load_and_upgrade_plan(None, "Create a Number model whose default value is 2")
        for stage in LAYERS:
            state["stages"][stage]["status"] = "complete"
        state["stages"]["services"]["status"] = "pending"
        return state

    def test_all_agents_have_shared_crud_and_read_other_layers(self):
        for stage in (*LAYERS, "pod"):
            tools = WorkspaceTools(self.root, stage)
            path = ("requirements.txt" if stage == "pod" else tools.paths[0] + "/example.txt")
            tools.execute(parse_action(encode_source_artifact(path, "first\r\n")))
            tools.execute(parse_action(encode_source_artifact(path, "second\r\n")))
            self.assertEqual((self.root / path).read_bytes(), b"second\r\n")
            self.assertIn("second", tools.execute({"tool": "read", "path": path})["content"])
            page = tools.execute({"tool": "read", "path": path, "limit": 3})
            tail = tools.execute({"tool": "read", "path": path, "offset": page["next_offset"]})
            self.assertEqual(page["content"] + tail["content"], "second\r\n")
            self.assertIsNone(tail["next_offset"])
            self.assertTrue(tools.execute({"tool": "search", "path": ".", "text": "second"})["matches"])
            tools.execute({"tool": "delete", "path": path})
            self.assertFalse((self.root / path).exists())
        self.assertEqual(path_owner("modules/models/item.py"), "models")

    def test_file_tools_do_not_allow_upstream_registry_or_path_escapes(self):
        tools = WorkspaceTools(self.root, "services")
        for path in ("modules/models/item.py", "beans_config.json", "aipod_plan.json", ".aipod/plan.json", ".git/config", "../outside.py", ".env"):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                tools.execute({"tool": "write", "path": path, "content": "bad"})
        with self.assertRaises(ValueError):
            parse_action('{"tool":"write","path":"modules/services/x.py","content":"code"}')

    def test_real_shell_cannot_write_rename_link_or_delete_upstream(self):
        tools = self.protected()
        parent = self.root / "modules/models"
        parent.mkdir(exist_ok=True)
        locked = parent / "locked.txt"; locked.write_text("unchanged")
        (self.root / "modules/services/escape").symlink_to(parent, target_is_directory=True)
        commands = [
            "printf bad > modules/models/locked.txt", "rm modules/models/locked.txt",
            "mv modules/models/locked.txt modules/services/stolen.txt",
            "printf bad > modules/services/escape/locked.txt",
            "ln modules/models/locked.txt modules/services/linked.txt",
            run_python("from pathlib import Path; Path('modules/models/locked.txt').write_text('bad')")["command"],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertNotEqual(tools.shell(command)["exit_code"], 0)
                self.assertEqual(locked.read_text(), "unchanged")
        self.assertEqual(tools.shell("printf allowed > modules/services/own.txt")["exit_code"], 0)
        self.assertEqual((self.root / "modules/services/own.txt").read_text(), "allowed")

    def test_preexisting_hardlinks_are_not_a_shell_write_escape(self):
        tools = self.protected()
        locked = self.root / "modules/models/locked.txt"; locked.parent.mkdir(exist_ok=True); locked.write_text("same")
        os.link(locked, self.root / "modules/services/alias.txt")
        with self.assertRaises(PermissionError):
            tools.shell("printf bad > modules/services/alias.txt")
        self.assertEqual(locked.read_text(), "same")

    def test_shell_timeout_and_credentials(self):
        tools = self.protected()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-parent-key"}):
            check = tools.execute(run_python("import os; assert 'OPENAI_API_KEY' not in os.environ"))
        self.assertEqual(check["exit_code"], 0, check)
        (self.root / ".env").write_text("PRIVATE=value")
        self.assertNotEqual(tools.shell("cat .env")["exit_code"], 0)
        command = run_python("import time; time.sleep(10)")
        command["timeout"] = 1
        self.assertTrue(tools.execute(command)["timed_out"])

    def test_no_unprotected_shell_fallback(self):
        tools = WorkspaceTools(self.root, "services")
        with patch("ai_pod_cli.workspace.sys.platform", "win32"), patch("ai_pod_cli.workspace.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "unrestricted"):
                tools.shell("echo should-not-run")
            spawn.assert_not_called()

    def test_shell_uses_the_current_python_environment(self):
        tools = self.protected()
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            check = tools.shell("python -c 'import sys; print(sys.prefix)'")
        self.assertEqual(check["exit_code"], 0, check)
        self.assertEqual(Path(check["output"].strip()).resolve(), Path(sys.prefix).resolve())

    def test_workspace_actions_keep_dsml_source_protocol_compatibility(self):
        raw = "<｜DSML｜create><｜DSML｜path>modules/models/note.txt</｜DSML｜path><｜DSML｜content><![CDATA[hello\r\n]]></｜DSML｜content></｜DSML｜create>"
        tools = WorkspaceTools(self.root, "models")
        tools.execute(parse_action(raw))
        self.assertEqual((self.root / "modules/models/note.txt").read_bytes(), b"hello\r\n")

    def test_named_create_cannot_register_a_different_component(self):
        state = load_and_upgrade_plan(None, "Create Requested")
        llm = Mock(return_value=response({"tool": "finish", "components": [{"id": "Other"}]}))
        co = PodCoordinator(self.root, state, llm, save=lambda s: None, max_steps=1, instruction_mode="direct")
        with self.assertRaisesRegex(RuntimeError, "1 steps"):
            co.run_layer("models", expected_component="Requested")
        self.assertFalse(any(bean["id"] == "Other" for bean in json.loads((self.root / "beans_config.json").read_text())["beans"]))

    def test_one_loop_writes_source_checks_and_registers_without_test_generation(self):
        self.protected("models")
        state = load_and_upgrade_plan(None, "Number defaults to 2")
        source = "from ai_pod_cli import Model\nclass Number(Model):\n    value: int = 2\n"
        actions = iter([
            encode_source_artifact("modules/models/number.py", source),
            run_python("from modules.models.number import Number; assert Number().value == 2"),
            {"tool": "finish", "summary": "default validated", "components": [{"id": "Number", "class_path": "modules.models.number.Number"}]},
        ])
        llm = Mock(side_effect=lambda *_a, **kw: response(next(actions)))
        co = PodCoordinator(self.root, state, llm, save=lambda s: None, instruction_mode="direct")
        with patch("ai_pod_cli.test_generation.prepare_component_tests", side_effect=AssertionError("must not run")):
            co.run_layer("models")
        self.assertEqual(llm.call_count, 3)
        self.assertTrue(all(call.kwargs["json_mode"] is False for call in llm.call_args_list))
        self.assertFalse((self.root / ".aipod/component-tests.json").exists())
        self.assertEqual(state["stages"]["models"]["status"], "complete")
        self.assertEqual(json.loads((self.root / "beans_config.json").read_text())["beans"][-1]["id"], "Number")

    def test_pod_approval_dispatches_owner_without_expanding_requester_permissions(self):
        self.protected("models")
        source_path = "modules/models/number.py"
        (self.root / source_path).write_text("from ai_pod_cli import Model\nclass Number(Model):\n    value: int = 1\n")
        state = self.state()
        request = {"tool": "request_change", "target": "models", "paths": [source_path], "reason": "Observed default 1 but objective requires 2", "change": "Set Number.value default to 2"}
        actions = {
            "models": iter([encode_source_artifact(source_path, "from ai_pod_cli import Model\nclass Number(Model):\n    value: int = 2\n"),
                run_python("from modules.models.number import Number; assert Number().value == 2"),
                {"tool": "finish", "summary": "corrected default", "components": [{"id": "Number", "class_path": "modules.models.number.Number"}]}]),
            "providers": iter([{"tool": "finish", "summary": "No project Providers need adaptation"}]),
            "services": iter([request, run_python("from modules.models.number import Number; assert Number().value == 2"), {"tool": "finish", "summary": "upstream now correct"}]),
        }
        calls = []
        def llm(system, user, **options):
            if options["json_mode"]:
                calls.append("pod_approval")
                return {"approved": True, "summary": "Required by original objective"}
            stage = system.split("\nLayer: ", 1)[1].splitlines()[0]
            calls.append(stage)
            return response(next(actions[stage]))
        co = PodCoordinator(self.root, state, llm, save=lambda s: None, instruction_mode="direct")
        co.run_layer("services")
        self.assertLess(calls.index("pod_approval"), calls.index("models"))
        self.assertIn("providers", calls)
        self.assertEqual(state["agent"]["change_requests"][0]["status"], "applied")
        with self.assertRaises(PermissionError):
            WorkspaceTools(self.root, "services").execute({"tool": "write", "path": source_path, "content": "bad"})
        self.assertEqual(state["stages"]["pipelines"]["status"], "pending")

    def test_denied_request_preserves_source_and_invalid_scopes_never_reach_pod(self):
        state = self.state(); llm = Mock(return_value={"approved": False, "summary": "Keep the original rule"})
        co = PodCoordinator(self.root, state, llm, save=lambda s: None, instruction_mode="direct")
        request = {"target": "models", "paths": ["modules/models/number.py"], "reason": "test failed", "change": "change default"}
        self.assertFalse(co.request_change("services", request)["approved"])
        self.assertFalse((self.root / "modules/models/number.py").exists())
        with self.assertRaises(PermissionError):
            co.request_change("services", {**request, "paths": ["modules/services/other.py"]})
        self.assertEqual(llm.call_count, 1)

    def test_owner_failure_does_not_approve_downstream_finish(self):
        state = self.state()
        def llm(*_args, **options):
            return {"approved": True} if options["json_mode"] else response({"tool": "read", "path": "missing.py"})
        co = PodCoordinator(self.root, state, llm, save=lambda s: None, max_steps=1, instruction_mode="direct")
        with self.assertRaises(RuntimeError):
            co.request_change("services", {"target": "models", "paths": ["modules/models/item.py"], "reason": "broken model", "change": "fix model"})
        self.assertEqual(state["agent"]["change_requests"][0]["status"], "failed")
        with self.assertRaisesRegex(RuntimeError, "upstream"):
            co.accept("services", {"tool": "finish"}, WorkspaceTools(self.root, "services"))

    def test_finish_needs_a_check_after_latest_edit(self):
        tools = self.protected("models")
        state = load_and_upgrade_plan(None, "test")
        co = PodCoordinator(self.root, state, Mock(), save=lambda s: None, instruction_mode="direct")
        tools.execute(run_python("assert 1 + 1 == 2"))
        tools.execute({"tool": "write", "path": "modules/models/note.txt", "content": "changed after check"})
        with self.assertRaisesRegex(ValueError, "successful shell check"):
            co.accept("models", {"tool": "finish"}, tools)

    def test_final_pod_is_also_a_tool_agent_and_runs_delivery_check(self):
        self.protected("pod")
        state = self.state()
        for stage in LAYERS:
            state["stages"][stage]["status"] = "complete"
        actions = iter([run_python("from pathlib import Path; assert Path('beans_config.json').is_file()"), {"tool": "finish", "summary": "project inspected"}])
        llm = Mock(side_effect=lambda *_a, **kw: response(next(actions)))
        co = PodCoordinator(self.root, state, llm, save=lambda s: None, instruction_mode="direct")
        co.build()
        self.assertEqual(state["agent"]["verification"]["status"], "passed")
        self.assertEqual(len(state["agent"]["verification"]["checks"]), 1)
        self.assertTrue(all("Layer: pod" in call.args[0] for call in llm.call_args_list))

    def test_final_pod_requests_owner_repair_and_repeats_ordinary_test(self):
        self.protected("models"); self.protected("pod")
        state = load_and_upgrade_plan(None, "Number.value must default to 2")
        path = "modules/models/number.py"
        model = {"tool": "finish", "summary": "Number model", "components": [{"id": "Number", "class_path": "modules.models.number.Number"}]}
        test_path = "tests/pod/test_number.py"
        test_source = "import unittest\nfrom modules.models.number import Number\nclass NumberTests(unittest.TestCase):\n    def test_default(self):\n        self.assertEqual(Number().value, 2)\n"
        check = {"tool": "shell", "command": shlex.quote(sys.executable) + " -m unittest discover -s tests/pod"}
        actions = {
            "models": [encode_source_artifact(path, "from ai_pod_cli import Model\nclass Number(Model):\n    value: int = 1\n"),
                       run_python("from modules.models.number import Number; assert Number().value == 1"), model,
                       encode_source_artifact(path, "from ai_pod_cli import Model\nclass Number(Model):\n    value: int = 2\n"),
                       run_python("from modules.models.number import Number; assert Number().value == 2"), model],
            "pod": [encode_source_artifact(test_path, test_source), check,
                    {"tool": "request_change", "target": "models", "paths": [path], "reason": "The ordinary acceptance test observed 1 instead of required 2", "change": "Set the default to 2"},
                    check, {"tool": "finish", "summary": "Original requirement now passes"}],
        }
        def llm(system, user, **options):
            if options["json_mode"]:
                return {"approved": True, "summary": "Correct the original requirement"}
            stage = system.split("\nLayer: ", 1)[1].splitlines()[0]
            queue = actions.get(stage, [])
            return response(queue.pop(0) if queue else {"tool": "finish", "summary": "No extra code required"})
        co = PodCoordinator(self.root, state, llm, save=lambda s: None, instruction_mode="direct")
        co.build()
        self.assertEqual(state["agent"]["status"], "complete")
        self.assertEqual([item["exit_code"] for item in state["agent"]["verification"]["checks"]], [1, 0])
        self.assertEqual(state["agent"]["change_requests"][0]["requester"], "pod")
        self.assertEqual((self.root / test_path).read_text(), test_source)

    def test_resume_approved_request_does_not_ask_for_approval_again(self):
        self.protected("models"); self.protected("pod")
        state = self.state()
        state["agent"]["change_requests"] = [{"id": "saved", "requester": "services", "target": "models", "paths": ["modules/models/note.txt"],
                                               "reason": "original need", "change": "Write the approved note", "approved": True, "status": "working"}]
        actions = {"models": [encode_source_artifact("modules/models/note.txt", "approved"), run_python("assert open('modules/models/note.txt').read() == 'approved'"), {"tool": "finish", "summary": "resumed correction"}],
                   "pod": [run_python("assert open('modules/models/note.txt').read() == 'approved'"), {"tool": "finish", "summary": "checked"}]}
        def llm(system, user, **options):
            self.assertFalse(options["json_mode"], "Approval must be reused")
            stage = system.split("\nLayer: ", 1)[1].splitlines()[0]
            queue = actions.get(stage, [])
            return response(queue.pop(0) if queue else {"tool": "finish", "summary": "compatible"})
        PodCoordinator(self.root, state, llm, save=lambda s: None, instruction_mode="direct").build()
        self.assertEqual(state["agent"]["change_requests"][0]["status"], "applied")


if __name__ == "__main__":
    unittest.main()
