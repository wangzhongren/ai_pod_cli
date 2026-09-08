"""Application repair-budget boundaries using real behavior verification."""

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_pod_cli.config import init_config_if_not_exists
from ai_pod_cli.pod.agent import handle_pod
from ai_pod_cli.pod.state import load_decision_plan, save_decision_plan


class RepairBudgetTests(unittest.TestCase):
    def run_repairs(self, target, *, rejected=0, prior_repairs=0, build=False):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "OPENAI_API_KEY": "test", "PYTHONDONTWRITEBYTECODE": "1",
        }), redirect_stdout(io.StringIO()):
            try:
                project = Path(tmp)
                os.chdir(project)
                init_config_if_not_exists()
                (project / "pipelines").mkdir(exist_ok=True)
                source = project / "pipelines/work.py"
                source.write_text(
                    "LEVEL = 0\n"
                    "def run(ctx):\n"
                    f"    if LEVEL < {target}:\n"
                    "        raise RuntimeError('repair required')\n"
                    "    return {'value': LEVEL}\n",
                )
                (project / "routes.toml").write_text('[work]\npipeline = "pipelines/work.py"\n')
                acceptance = project / "acceptance.py"
                acceptance.write_text(
                    "import unittest\nfrom ai_pod_cli.runner import PipelineRunner\n"
                    "class Behavior(unittest.TestCase):\n"
                    "    def test_value(self):\n"
                    "        result = PipelineRunner().run('work', {})\n"
                    f"        self.assertEqual(result['value'], {target})\n",
                )
                frozen = acceptance.read_bytes()
                state = load_decision_plan("repair budget")
                for record in state["stages"].values():
                    record.update(status="pending" if build else "complete", plan={})
                state["stages"]["interfaces"]["plan"] = {"interfaces": [{
                    "name": "test", "artifacts": [{"path": "acceptance.py", "role": "behavior_test"}],
                    "verify": [{"name": "behavior", "kind": "behavior", "required": True,
                                "command": ["{python}", "-m", "ai_pod_cli.behavior_tests", "acceptance.py"],
                                "cases": [{"test": "Behavior.test_value", "requirement": "The route returns the required value"}]}],
                }]}
                state["agent"]["verification"]["repairs"] = prior_repairs
                save_decision_plan(state)
                calls = 0
                def propose(*_args, **_kwargs):
                    nonlocal calls
                    calls += 1
                    if calls <= rejected:
                        return {"patches": [{"old": "nonexistent source", "new": "invalid patch"}]}
                    old = source.read_text().splitlines()[0]
                    level = int(old.split("=", 1)[1].strip())
                    return {"patches": [{"old": old, "new": f"LEVEL = {level + 1}"}]}
                def finish_stage(args):
                    current = load_decision_plan("repair budget")
                    list(current["stages"].values())[args._pod_stage]["status"] = "complete"
                    save_decision_plan(current)
                exit_code = 0
                with patch("ai_pod_cli.pod.verification.call_llm", side_effect=propose), patch(
                    "ai_pod_cli.pod.agent._execute_pod_build_tool", side_effect=finish_stage,
                ):
                    try:
                        handle_pod(SimpleNamespace(desc="repair budget", file="", yes=True, json=True))
                    except SystemExit as error:
                        exit_code = error.code
                final = json.loads((project / "aipod_plan.json").read_text())
                self.assertEqual(acceptance.read_bytes(), frozen)
                return final, calls, exit_code
            finally:
                os.chdir(previous)

    def test_tenth_repair_gets_final_verification_after_all_build_stages(self):
        state, calls, code = self.run_repairs(10, build=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls, 10)
        self.assertEqual(state["agent"]["status"], "complete")
        self.assertEqual(state["agent"]["verification"]["repair_attempts"], 10)
        self.assertEqual(state["agent"]["verification"]["attempts"], 11)
        self.assertEqual(len(state["agent"]["history"]), 26)

    def test_failed_tenth_verification_never_starts_eleventh_repair(self):
        state, calls, code = self.run_repairs(11)
        self.assertEqual(code, 1)
        self.assertEqual(calls, 10)
        self.assertEqual(state["agent"]["status"], "blocked")
        self.assertEqual(state["agent"]["verification"]["repairs"], 10)
        self.assertEqual(state["agent"]["verification"]["attempts"], 11)

    def test_rejected_patches_count_and_do_not_stop_after_two(self):
        state, calls, code = self.run_repairs(1, rejected=3)
        self.assertEqual(code, 0)
        self.assertEqual(calls, 4)
        self.assertEqual(state["agent"]["verification"]["repair_attempts"], 4)
        self.assertEqual(state["agent"]["verification"]["repairs"], 1)

    def test_legacy_resume_retains_used_repair_budget(self):
        state, calls, code = self.run_repairs(1, prior_repairs=9)
        self.assertEqual(code, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(state["agent"]["verification"]["repair_attempts"], 10)
        self.assertEqual(state["agent"]["verification"]["repairs"], 10)
