"""Bounded Pod authorization and resumable workspace Agent work."""
import tempfile
import io
from contextlib import redirect_stdout
from pathlib import Path
import unittest
from unittest.mock import Mock

from ai_pod_cli.pod.coordinator import PodCoordinator
from ai_pod_cli.pod.state import load_and_upgrade_plan
from ai_pod_cli.workspace import WorkspaceTools
from ai_pod_cli.workspace_agent import WorkspaceAgent
from ai_pod_cli.source_codec import encode_source_artifact


class RepairBudgetTests(unittest.TestCase):
    def test_pod_and_layer_instruction_budgets_are_independent(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'beans_config.json').write_text('{"beans":[]}')
            state = load_and_upgrade_plan(None, 'Check instruction budgets')
            llm = Mock(return_value='<unknown/>')
            coordinator = PodCoordinator(root, state, llm, save=lambda _: None, instruction_mode="direct")
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'pod Agent reached 200 steps'):
                coordinator.run_layer('pod')
            self.assertEqual(llm.call_count, 200)
            llm.reset_mock()
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'models Agent reached 100 steps'):
                coordinator.run_layer('models')
            self.assertEqual(llm.call_count, 100)
            pod = WorkspaceTools(root, 'pod')
            self.assertEqual(WorkspaceAgent(llm, pod, instruction_mode="direct").max_steps, 200)
            self.assertEqual(WorkspaceAgent(llm, pod, max_steps=2, instruction_mode="direct").max_steps, 2)

    def test_ten_change_requests_are_allowed_but_not_an_eleventh_decision(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'beans_config.json').write_text('{"beans":[]}')
            state = load_and_upgrade_plan(None, 'Keep original contracts')
            llm = Mock(return_value={'approved': False, 'summary': 'Not required'})
            co = PodCoordinator(root, state, llm, save=lambda _: None, instruction_mode="direct")
            request = {'target': 'models', 'paths': ['modules/models/item.py'], 'reason': 'proposed change', 'change': 'adjust field'}
            for _ in range(10):
                self.assertFalse(co.request_change('services', request)['approved'])
            with self.assertRaisesRegex(RuntimeError, 'limit'):
                co.request_change('services', request)
            self.assertEqual(llm.call_count, 10)
            self.assertEqual(len(state['agent']['change_requests']), 10)

    def test_step_limit_preserves_partial_owned_files_for_resume(self):
        with tempfile.TemporaryDirectory() as root:
            tools = WorkspaceTools(root, 'models')
            llm = Mock(side_effect=[encode_source_artifact('modules/models/note.txt', 'partial work'), '{"tool":"list","path":"modules/models"}'])
            with self.assertRaisesRegex(RuntimeError, '2 steps'):
                WorkspaceAgent(llm, tools, max_steps=2, instruction_mode="direct").run('Build model', {}, request_change=Mock(), finish=Mock())
            self.assertEqual(Path(root, 'modules/models/note.txt').read_text(), 'partial work')
            self.assertEqual(llm.call_count, 2)

    def test_failed_owner_dispatch_does_not_grant_requester_files(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'beans_config.json').write_text('{"beans":[]}')
            state = load_and_upgrade_plan(None, 'Repair model')
            co = PodCoordinator(root, state, Mock(return_value={'approved': True}), save=lambda _: None, instruction_mode="direct")
            co.run_layer = Mock(side_effect=RuntimeError('owner stopped'))
            with self.assertRaisesRegex(RuntimeError, 'owner stopped'):
                co.request_change('services', {'target': 'models', 'paths': ['modules/models/item.py'], 'reason': 'contract mismatch', 'change': 'correct field'})
            self.assertEqual(state['agent']['change_requests'][0]['status'], 'failed')
            with self.assertRaises(PermissionError):
                WorkspaceTools(root, 'services').execute({'tool': 'write', 'path': 'modules/models/item.py', 'content': 'escape'})
