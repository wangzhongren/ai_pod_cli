"""Generation tools discover/create pure utilities without putting source in JSON."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_pod_cli.source_codec import encode_source_artifact
from ai_pod_cli.source_generation import generate_source
from ai_pod_cli.utility_tools import call_with_utility_tools


UTILITY_SOURCE = '''class NumberUtils:
    """Reusable integer arithmetic."""

    @staticmethod
    def add(a: int, b: int) -> int:
        """Return the sum of two integers."""
        return a + b
'''
CASES = [{"method": "add", "args": [2, 3], "expected": 5}]
WRITE = {"tool": "write_utility", "arguments": {
    "id": "NumberUtils", "description": "Reusable arithmetic", "instruction": "Implement add(a, b)",
    "cases": CASES,
}}


class UtilityToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.entries = {}
        self.sources = {}
        def listing(project_root=".", query=""):
            return [dict(item) for item in self.entries.values() if query.lower() in item["id"].lower()]
        def reading(identifier, project_root="."):
            if identifier not in self.entries:
                raise ValueError(f"Utility not registered: {identifier}")
            return {**self.entries[identifier], "source": self.sources[identifier], "callers": []}
        def writing(identifier, *, source, description, cases, project_root=".", allow_update=False):
            self.assertFalse(allow_update)
            self.assertEqual(Path(project_root), self.root)
            self.add_utility(identifier, source)
            return {**self.entries[identifier], "operation": "created"}
        self.registry = SimpleNamespace(list_utilities=Mock(side_effect=listing),
                                        read_utility=Mock(side_effect=reading),
                                        write_utility=Mock(side_effect=writing))
        replacement = patch("ai_pod_cli.utility_tools._registry", return_value=self.registry)
        replacement.start()
        self.addCleanup(replacement.stop)

    def add_utility(self, identifier="NumberUtils", source=UTILITY_SOURCE):
        self.entries[identifier] = {
            "id": identifier, "path": f"modules/utils/{identifier.lower()}.py", "symbol": identifier,
            "description": "Pure arithmetic", "methods": [{"name": "add", "signature": "(a: int, b: int) -> int"}],
        }
        self.sources[identifier] = source

    def test_no_tools_preserves_single_metadata_call_and_transport_options(self):
        metadata = {"inputs": {}, "outputs": {"value": "int"}}
        llm = Mock(return_value=metadata)
        result = call_with_utility_tools(llm, "rules", "task", role="interface", project_root=self.root,
                                         max_tokens=512, timeout_seconds=12, max_retries=1)
        self.assertIs(result, metadata)
        llm.assert_called_once()
        self.assertEqual(llm.call_args.kwargs, {
            "max_tokens": 512, "timeout_seconds": 12, "max_retries": 1, "json_mode": True,
        })
        self.assertIn("SHARED PURE UTILITY TOOLS", llm.call_args.args[0])
        self.assertNotIn("role", llm.call_args.kwargs)

    def test_source_generation_without_tools_keeps_metadata_then_xml(self):
        llm = Mock(side_effect=[{}, encode_source_artifact("app.py", "print(1)")])
        result = generate_source(llm, "rules", "task", "app.py", project_root=self.root,
                                 max_tokens=512, source_max_tokens=8192, source_timeout_seconds=120)
        self.assertEqual(result["code"], "print(1)")
        self.assertEqual([call.kwargs["json_mode"] for call in llm.call_args_list], [True, False])
        self.assertEqual(llm.call_args_list[0].kwargs, {"json_mode": True, "max_tokens": 512})
        self.assertEqual(llm.call_args_list[1].kwargs, {"json_mode": False, "max_tokens": 8192, "timeout_seconds": 120})

    def test_list_and_bounded_read_return_observations_not_metadata(self):
        self.add_utility(source="".join(f"# source line {number}\n" for number in range(1, 401)))
        llm = Mock(side_effect=[
            {"tool": "list_utilities", "arguments": {"query": "Number"}},
            {"tool": "read_utility", "arguments": {"id": "NumberUtils", "start_line": 201, "max_lines": 3}},
            {"inputs": {}, "outputs": {}},
        ])
        observed = []
        result = call_with_utility_tools(llm, "rules", "task", project_root=self.root, on_tool_result=observed.append)
        self.assertEqual(result, {"inputs": {}, "outputs": {}})
        self.assertEqual(observed[-1]["result"]["source"], "# source line 201\n# source line 202\n# source line 203\n")
        self.assertTrue(observed[-1]["result"]["source_range"]["has_more"])
        self.assertNotIn("source line 204", llm.call_args.args[1])

    def test_every_layer_has_the_same_pure_tools(self):
        for role in ("model", "provider", "service", "pipeline", "interface"):
            with self.subTest(role=role):
                llm = Mock(side_effect=[{"tool": "list_utilities", "arguments": {}}, {}])
                self.assertEqual(call_with_utility_tools(llm, "rules", "task", role=role, project_root=self.root), {})
                self.assertEqual(llm.call_count, 2)

    def test_write_requests_xml_and_updates_later_component_source_context(self):
        component_source = "from modules.utils.numberutils import NumberUtils\nprint(NumberUtils.add(2, 3))\n"
        llm = Mock(side_effect=[
            deepcopy(WRITE), encode_source_artifact("modules/utils/numberutils.py", UTILITY_SOURCE),
            {"inputs": {}, "outputs": {}}, encode_source_artifact("app.py", component_source),
        ])
        result = generate_source(llm, "No private modules.* imports", "task", "app.py", project_root=self.root)
        self.assertEqual(result["code"], component_source)
        self.assertEqual([call.kwargs["json_mode"] for call in llm.call_args_list], [True, False, True, False])
        self.assertEqual(self.registry.write_utility.call_args.kwargs["source"], UTILITY_SOURCE)
        self.assertEqual(self.registry.write_utility.call_args.kwargs["cases"], CASES)
        final_system = llm.call_args.args[0]
        self.assertIn("NumberUtils", final_system)
        self.assertIn("EXACT IMPORT EXCEPTION", final_system)
        self.assertIn("add", final_system)
        self.assertNotIn("tool", result)

    def test_successful_read_context_reaches_xml_without_metadata_fields(self):
        self.add_utility()
        llm = Mock(side_effect=[
            {"tool": "read_utility", "arguments": {"id": "NumberUtils"}},
            {}, encode_source_artifact("app.py", "print(1)"),
        ])
        result = generate_source(llm, "rules", "task", "app.py", project_root=self.root)
        self.assertEqual(result, {"path": "app.py", "code": "print(1)"})
        self.assertIn("Return the sum of two integers", llm.call_args.args[0])

    def test_frozen_metadata_skips_tools_and_still_sees_registered_signatures(self):
        self.add_utility()
        llm = Mock(return_value=encode_source_artifact("app.py", "print(1)"))
        generate_source(llm, "rules", "task", "app.py", frozen_metadata={}, project_root=self.root)
        llm.assert_called_once()
        self.assertFalse(llm.call_args.kwargs["json_mode"])
        self.assertIn("NumberUtils", llm.call_args.args[0])

    def test_existing_id_cannot_be_rewritten_or_regenerated(self):
        self.add_utility()
        llm = Mock(side_effect=[deepcopy(WRITE), {}])
        call_with_utility_tools(llm, "rules", "task", project_root=self.root)
        self.assertEqual([call.kwargs["json_mode"] for call in llm.call_args_list], [True, True])
        self.registry.write_utility.assert_not_called()
        self.assertIn("already registered", llm.call_args.args[1])
        self.assertEqual(self.sources["NumberUtils"], UTILITY_SOURCE)

    def test_unknown_tools_malformed_arguments_and_source_in_json_are_rejected(self):
        invalid = [
            {"tool": "shell", "arguments": {"command": "write anywhere"}},
            {"tool": "read_utility", "arguments": {"path": "../secret"}},
            {"tool": "read_utility", "arguments": {"id": "../secret"}},
            {"tool": "list_utilities", "arguments": []},
            {"tool_result": {"ok": True}},
            {"tool": "list_utilities", "arguments": {}, "inputs": {}},
            {"tool": "write_utility", "arguments": {**WRITE["arguments"], "source": "untrusted source"}},
            {"tool": "write_utility", "arguments": {**WRITE["arguments"], "id": "numberUtils"}},
        ]
        for action in invalid:
            with self.subTest(action=action):
                llm = Mock(side_effect=[action, {"inputs": {}}])
                self.assertEqual(call_with_utility_tools(llm, "rules", "task", project_root=self.root), {"inputs": {}})
                self.assertIn('"ok": false', llm.call_args.args[1])
        self.registry.read_utility.assert_not_called()
        self.registry.write_utility.assert_not_called()

    def test_xml_cannot_recursively_select_more_tools(self):
        llm = Mock(side_effect=[deepcopy(WRITE), deepcopy(WRITE), deepcopy(WRITE), deepcopy(WRITE), {}])
        call_with_utility_tools(llm, "rules", "task", project_root=self.root)
        self.assertEqual([call.kwargs["json_mode"] for call in llm.call_args_list], [True, False, False, False, True])
        self.registry.write_utility.assert_not_called()
        self.assertIn('"ok": false', llm.call_args.args[1])

    def test_failed_registration_is_feedback_and_never_success(self):
        self.registry.write_utility.side_effect = ValueError("Utility case add failed")
        llm = Mock(side_effect=[deepcopy(WRITE), encode_source_artifact("modules/utils/numberutils.py", UTILITY_SOURCE), {}])
        events = []
        result = call_with_utility_tools(llm, "rules", "task", project_root=self.root, on_tool_result=events.append)
        self.assertEqual(result, {})
        self.assertFalse(events[0]["ok"])
        self.assertNotIn("result", events[0])
        self.assertIn("case add failed", llm.call_args.args[1])

    def test_registry_failure_payload_is_not_treated_as_creation_success(self):
        self.registry.write_utility.side_effect = None
        self.registry.write_utility.return_value = {"ok": False, "error": "case failed"}
        llm = Mock(side_effect=[deepcopy(WRITE), encode_source_artifact("modules/utils/numberutils.py", UTILITY_SOURCE), {}])
        events = []
        call_with_utility_tools(llm, "rules", "task", project_root=self.root, on_tool_result=events.append)
        self.assertFalse(events[0]["ok"])
        self.assertIn("did not confirm", events[0]["error"])

    def test_tool_budget_exhaustion_prevents_component_xml_and_excess_write(self):
        llm = Mock(return_value={"tool": "shell", "arguments": {}})
        with self.assertRaisesRegex(ValueError, "budget exhausted after 8"):
            generate_source(llm, "rules", "task", "app.py", project_root=self.root)
        self.assertEqual(llm.call_count, 9)
        self.assertTrue(all(call.kwargs["json_mode"] for call in llm.call_args_list))
        self.registry.write_utility.assert_not_called()
        llm = Mock(side_effect=[{"tool": "list_utilities", "arguments": {}}, deepcopy(WRITE)])
        with self.assertRaisesRegex(ValueError, "budget exhausted after 1"):
            call_with_utility_tools(llm, "rules", "task", project_root=self.root, max_tool_calls=1)
        self.registry.write_utility.assert_not_called()

    def test_last_allowed_action_can_be_followed_by_metadata(self):
        llm = Mock(side_effect=[{"tool": "list_utilities", "arguments": {}}, {"ready": True}])
        self.assertEqual(call_with_utility_tools(llm, "rules", "task", project_root=self.root, max_tool_calls=1), {"ready": True})

    def test_read_payload_is_bounded_and_reports_truncation(self):
        self.add_utility(source=("# " + "x" * 500 + "\n") * 200)
        llm = Mock(side_effect=[{"tool": "read_utility", "arguments": {"id": "NumberUtils", "max_lines": 200}}, {}])
        call_with_utility_tools(llm, "rules", "task", project_root=self.root)
        self.assertLess(len(llm.call_args.args[1]), 14_000)
        self.assertIn('"truncated": true', llm.call_args.args[1])

    def test_control_options_never_reach_the_raw_model(self):
        llm = Mock(return_value={})
        call_with_utility_tools(llm, "rules", "task", role="model", project_root=self.root,
                                max_tool_calls=0, on_tool_result=lambda event: None,
                                source_max_tokens=8192, source_timeout_seconds=120)
        self.assertEqual(llm.call_args.kwargs, {"json_mode": True})


class UtilityRegistryIntegrationTests(unittest.TestCase):
    def test_xml_tool_creation_registers_only_a_verified_utility(self):
        from ai_pod_cli.utilities import read_utility
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            llm = Mock(side_effect=[deepcopy(WRITE), encode_source_artifact("modules/utils/numberutils.py", UTILITY_SOURCE), {}])
            self.assertEqual(call_with_utility_tools(llm, "rules", "task", project_root=root), {})
            utility = read_utility("NumberUtils", project_root=root)
            self.assertEqual(utility["source"], UTILITY_SOURCE)
            self.assertEqual(utility["cases"][0]["expected"], 5)
            self.assertEqual((root / "modules/utils/numberutils.py").read_text(), UTILITY_SOURCE)
            self.assertTrue((root / "utility_registry.json").is_file())

    def test_failed_real_cases_leave_no_registered_source(self):
        from ai_pod_cli.utilities import list_utilities
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = deepcopy(WRITE)
            request["arguments"]["cases"][0]["expected"] = 999
            llm = Mock(side_effect=[request, encode_source_artifact("modules/utils/numberutils.py", UTILITY_SOURCE), {}])
            events = []
            self.assertEqual(call_with_utility_tools(llm, "rules", "task", project_root=root, on_tool_result=events.append), {})
            self.assertFalse(events[0]["ok"])
            self.assertEqual(list_utilities(root), [])
            self.assertFalse((root / "modules/utils/numberutils.py").exists())

    def test_hash_drift_cannot_be_hidden_by_a_model_recreation_request(self):
        from ai_pod_cli.utilities import write_utility
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_utility("NumberUtils", UTILITY_SOURCE, "Arithmetic", deepcopy(CASES), project_root=root)
            source = root / "modules/utils/numberutils.py"
            source.write_text(UTILITY_SOURCE + "\n# changed without registration\n")
            llm = Mock(return_value=deepcopy(WRITE))
            with self.assertRaises(ValueError):
                call_with_utility_tools(llm, "rules", "task", project_root=root)
            llm.assert_not_called()
            self.assertIn("changed without registration", source.read_text())


if __name__ == "__main__":
    unittest.main()
