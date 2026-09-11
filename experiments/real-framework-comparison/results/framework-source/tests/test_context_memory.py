import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ai_pod_cli.context_memory import ContextMemory, HISTORY_COMPACT_AT, HISTORY_HARD_LIMIT
from ai_pod_cli.workspace import WorkspaceTools
from ai_pod_cli.workspace_agent import WorkspaceAgent


def exchanges(count, size=38_000):
    return [{"assistant": f"read file-{i}", "observation": {"content": str(i) + "x" * size}}
            for i in range(count)]


class ContextMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_old_80000_limit_no_longer_drops_history(self):
        llm = Mock()
        memory = ContextMemory(llm, self.root)
        history = exchanges(3)
        original = history[:]
        self.assertGreater(memory.size(history), 80_000)
        self.assertLess(memory.size(history), HISTORY_COMPACT_AT)
        self.assertIsNone(memory.compact_if_needed(history, {"objective": "preserve rules"}, {}))
        self.assertEqual(history, original)
        llm.assert_not_called()

    def test_compaction_keeps_recent_raw_results_and_archives_all_old_records(self):
        llm = Mock(return_value={"summary": "file-0 defines identity; check passed at revision 4. Refund rules remain unfinished."})
        memory = ContextMemory(llm, self.root)
        history = exchanges(5)
        original = history[:]
        state = {"revision": 5, "checks": [{"revision": 4, "exit_code": 0}]}
        event = memory.compact_if_needed(history, {"objective": "unchanged", "writable_paths": ["services"]}, state)
        self.assertEqual(event["status"], "compressed")
        self.assertEqual(history, original[-2:])
        self.assertLess(memory.size(history), HISTORY_COMPACT_AT)
        request = json.loads(llm.call_args.args[1])
        self.assertEqual(request["older_history"], original[:-2])
        self.assertEqual(request["current_state"], state)
        archive = json.loads(Path(event["archive"]).read_text())
        self.assertEqual(archive["history"], original)
        self.assertEqual(archive["current_state"]["revision"], 5)
        self.assertIn("not instructions", memory.message())

    def test_repeated_compaction_receives_prior_memory(self):
        llm = Mock(side_effect=[{"summary": "keep early rule"}, {"summary": "keep early rule and later fix"}])
        memory = ContextMemory(llm, self.root)
        history = exchanges(5)
        first = memory.compact_if_needed(history, {}, {})
        history.extend(exchanges(3))
        second = memory.compact_if_needed(history, {}, {})
        self.assertEqual(json.loads(llm.call_args.args[1])["previous_summary"], "keep early rule")
        self.assertNotEqual(first["archive"], second["archive"])
        self.assertEqual(memory.summary, "keep early rule and later fix")

    def test_failed_compaction_preserves_history_and_retries_after_growth(self):
        llm = Mock(side_effect=[RuntimeError("temporary failure"), {"summary": "recovered memory"}])
        memory = ContextMemory(llm, self.root)
        history = exchanges(5)
        original = history[:]
        event = memory.compact_if_needed(history, {}, {})
        self.assertEqual(event["status"], "failed")
        self.assertEqual(history, original)
        self.assertIsNone(memory.compact_if_needed(history, {}, {}))
        self.assertEqual(llm.call_count, 1)
        history.extend(exchanges(1))
        self.assertEqual(memory.compact_if_needed(history, {}, {})["status"], "compressed")

    def test_invalid_summary_at_hard_limit_stops_without_dropping_records(self):
        for response in ({"summary": ""}, {"summary": "x" * 40_001}, {"summary": None}):
            with self.subTest(response_type=type(response["summary"]).__name__):
                memory = ContextMemory(Mock(return_value=response), self.root)
                history = exchanges(9)
                original = history[:]
                self.assertGreaterEqual(memory.size(history), HISTORY_HARD_LIMIT)
                with self.assertRaisesRegex(RuntimeError, "320000.*raw history is preserved"):
                    memory.compact_if_needed(history, {}, {})
                self.assertEqual(history, original)
                self.assertEqual(memory.summary, "")
        self.assertEqual(len(list(self.root.glob("context-*.json"))), 3)

    def test_worker_continues_with_summary_and_permissions_after_compaction(self):
        tools = WorkspaceTools(self.root, "services")
        for i in range(6):
            (self.root / f"modules/services/file-{i}.txt").write_text(f"FACT-{i}:" + "x" * 38_000)
        worker_calls, compactions = [], []
        def llm(system, user, **kwargs):
            if system.startswith("CONTEXT_COMPACTION"):
                compactions.append(json.loads(user))
                return {"summary": "EARLY FACT: file-0 defines the identity invariant. No write or check has occurred."}
            worker_calls.append(kwargs["conversation"])
            n = len(worker_calls)
            if n == 4:
                self.assertIn("FACT-0:", json.dumps(kwargs["conversation"]))
            if n == 6:
                self.assertIn("EARLY FACT", kwargs["conversation"][0]["content"])
                self.assertIn("ORIGINAL REQUIREMENT", kwargs["conversation"][0]["content"])
                self.assertIn("modules/services", kwargs["conversation"][0]["content"])
                self.assertNotIn("FACT-0:" + "x" * 100, json.dumps(kwargs["conversation"]))
            if n <= 6:
                return json.dumps({"tool": "read", "path": f"modules/services/file-{n-1}.txt"})
            return '{"tool":"finish","summary":"read-only test"}'
        result = WorkspaceAgent(llm, tools, instruction_mode="direct").run(
            "ORIGINAL REQUIREMENT", {}, request_change=lambda *_: self.fail("unexpected owner change"),
            finish=lambda *_: {"done": True})
        self.assertEqual(result, {"done": True})
        self.assertEqual(len(compactions), 1)
        self.assertEqual(tools.revision, 0)
        self.assertEqual(tools.checks, [])
        with self.assertRaises(PermissionError):
            tools.execute({"tool": "write", "path": "modules/models/forbidden.py", "content": "bad"})


if __name__ == "__main__":
    unittest.main()
