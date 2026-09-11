import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_pod_cli.action_codec import decode_translated_action
from ai_pod_cli.instruction_translator import InstructionTranslator, parse_need, encode_need
from ai_pod_cli.workspace import WorkspaceTools
from ai_pod_cli.workspace_agent import WorkspaceAgent
from ai_pod_cli.pod.coordinator import PodCoordinator
from ai_pod_cli.pod.state import load_and_upgrade_plan


class InstructionTranslatorTests(unittest.TestCase):
    def test_request_preserves_source_and_rejects_extra_requests(self):
        for value in ['Read a & b.', 'x = "</need_function_tool>"\r\n', 'label = "]]>"\n']:
            self.assertEqual(parse_need(encode_need(value)), value)
        self.assertEqual(parse_need('<need_function_tool>Read a & b.</need_function_tool>'), 'Read a & b.')
        for raw in ['', '<need_function_tool/>', '<need_function_tool> </need_function_tool>',
                    'prose <need_function_tool>Read</need_function_tool>',
                    '<need_function_tool>Read</need_function_tool><need_function_tool>Write</need_function_tool>',
                    '<need_function_tool><![CDATA[a]]></need_function_tool><need_function_tool><![CDATA[b]]></need_function_tool>']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_need(raw)

    def test_structured_operands_and_literal_source(self):
        for action in [None, [], {'tool': 'fetch'}, {'tool': 'read', 'path': 'x', 'delete': True},
                       {'tool': 'write', 'path': 'x', 'content': 12},
                       {'tool': 'shell', 'command': 'test', 'timeout': True},
                       {'tool': 'finish', 'summary': 'done', 'components': {}},
                       {'tool': 'request_change', 'target': 'models', 'paths': 'x', 'reason': 'r', 'change': 'c'}]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                decode_translated_action(action)
        llm = Mock(return_value={'tool': 'write', 'path': 'modules/models/x.py', 'content': 'invented source'})
        with self.assertRaisesRegex(ValueError, 'complete exact source'):
            InstructionTranslator(llm).translate('Write a model', {})
        self.assertTrue(llm.call_args.kwargs['json_mode'])
        source = 'x = "a & b"\r\n'
        llm.return_value['content'] = source
        self.assertEqual(InstructionTranslator(llm).translate('Write:\n' + source, {})['content'], source)

    def test_default_worker_converts_and_preserves_owner_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            tools = WorkspaceTools(root, 'services')
            source = 'value = 1\n'
            own, upstream = 'modules/services/impl/value.py', 'modules/models/value.py'
            requests = [f'Write {upstream}:\n{source}', f'Write {own}:\n{source}', 'Read the file', 'Finish with summary done']
            actions = [{'tool': 'write', 'path': upstream, 'content': source}, {'tool': 'write', 'path': own, 'content': source},
                       {'tool': 'read', 'path': own}, {'tool': 'finish', 'summary': 'done'}]
            worker, converter = [], []
            def llm(system, user, **options):
                if options['json_mode']:
                    payload = json.loads(user)
                    self.assertEqual(payload['context']['owner'], 'services')
                    self.assertEqual(payload['request'], requests[len(converter)])
                    converter.append(payload)
                    return actions[len(converter) - 1]
                self.assertIn('<need_function_tool>', system)
                self.assertNotIn('<read><path>', system)
                if worker:
                    self.assertIn('cannot modify', json.dumps(options['conversation']))
                result = encode_need(requests[len(worker)])
                worker.append(result)
                return result
            result = WorkspaceAgent(llm, tools, max_steps=4).run('Write then read', {}, request_change=Mock(), finish=lambda a, _: a)
            self.assertEqual(result['summary'], 'done')
            self.assertEqual(len(worker), 4)
            self.assertEqual(len(converter), 4)
            self.assertFalse(Path(root, upstream).exists())
            self.assertEqual(Path(root, own).read_text(), source)

    def test_large_source_history_keeps_the_request_protocol(self):
        with tempfile.TemporaryDirectory() as root:
            source = '#' + 'x' * 41000 + '\n'
            replies = iter([encode_need('Write modules/models/x.py:\n' + source), encode_need('Finish')])
            def llm(system, user, **options):
                if options['json_mode']:
                    request = json.loads(user)['request']
                    return {'tool': 'write', 'path': 'modules/models/x.py', 'content': source} if request.startswith('Write') else {'tool': 'finish', 'summary': 'done'}
                for message in options['conversation']:
                    if message['role'] == 'assistant':
                        self.assertIn('source omitted', parse_need(message['content']))
                return next(replies)
            WorkspaceAgent(llm, WorkspaceTools(root, 'models'), max_steps=2).run('Write', {}, request_change=Mock(), finish=lambda a, _: a)
            self.assertEqual(Path(root, 'modules/models/x.py').read_text(), source)

    def test_interrupt_during_conversion_never_writes(self):
        with tempfile.TemporaryDirectory() as root:
            def llm(_system, _user, **options):
                if options['json_mode']:
                    raise KeyboardInterrupt()
                return encode_need('Write modules/models/x.py: x=1')
            with self.assertRaises(KeyboardInterrupt):
                WorkspaceAgent(llm, WorkspaceTools(root, 'models')).run('Write', {}, request_change=Mock(), finish=Mock())
            self.assertFalse(Path(root, 'modules/models/x.py').exists())

    def test_cli_defaults_and_direct_override(self):
        from ai_pod_cli import cli
        for command, arguments in [('pod', ['objective']), ('create', ['--category', 'model', '--name', 'Value', '--desc', 'value']), ('compose', ['pipeline'])]:
            for mode in [None, 'direct']:
                argv = ['aipod', command, *arguments, *(['--instruction-mode', mode] if mode else [])]
                with patch.object(sys, 'argv', argv), patch.object(cli, '_apply_global_env'), patch.object(cli, 'init_config_if_not_exists'), patch('dotenv.load_dotenv'), patch.object(cli, 'handle_' + command) as handler:
                    cli.main()
                    self.assertEqual(handler.call_args.args[0].instruction_mode, mode or 'translated')

    def test_default_coordinator_builds_and_checks_all_layers(self):
        with tempfile.TemporaryDirectory() as root:
            previous = Path.cwd()
            os.chdir(root)
            try:
                Path('beans_config.json').write_text('{"beans":[]}')
                probe = WorkspaceTools(root, 'models').shell('printf ready')
                if 'sandbox_apply: Operation not permitted' in probe['output']:
                    self.skipTest('Nested sandbox unavailable')
                self.assertEqual(probe['exit_code'], 0, probe)
                source = 'from ai_pod_cli import Model\nclass Value(Model):\n    count: int = 1\n'
                check = shlex.quote(sys.executable) + ' -c ' + shlex.quote('from modules.models.value import Value; assert Value().count == 1')
                corrected = source.replace('= 1', '= 2')
                check_corrected = check.replace('== 1', '== 2')
                actions = {'models': [{'tool': 'write', 'path': 'modules/models/value.py', 'content': source}, {'tool': 'shell', 'command': check}, {'tool': 'finish', 'summary': 'model checked', 'components': [{'id': 'Value', 'class_path': 'modules.models.value.Value'}]}],
                           'services': [{'tool': 'request_change', 'target': 'models', 'paths': ['modules/models/value.py'], 'reason': 'Objective requires count 2', 'change': 'Set count to 2'}, {'tool': 'shell', 'command': check_corrected}, {'tool': 'finish', 'summary': 'corrected model checked'}],
                           'pod': [{'tool': 'shell', 'command': check_corrected}, {'tool': 'finish', 'summary': 'delivery checked'}]}
                actions['models'] += [{'tool': 'write', 'path': 'modules/models/value.py', 'content': corrected}, {'tool': 'shell', 'command': check_corrected}, {'tool': 'finish', 'summary': 'model corrected', 'components': [{'id': 'Value', 'class_path': 'modules.models.value.Value'}]}]
                seen, pending = [], {}
                def llm(system, user, **options):
                    if options['json_mode']:
                        if not system.startswith('TRANSLATE_LOCAL_OPERATION'):
                            return {'approved': True, 'summary': 'Required for count 2'}
                        owner = json.loads(user)['context']['owner']
                        seen.append(owner)
                        return pending.pop(owner)
                    owner = system.split('\nLayer: ')[1].split('\n')[0]
                    self.assertIn('<need_function_tool>', system)
                    queue = actions.get(owner, [])
                    pending[owner] = queue.pop(0) if queue else {'tool': 'finish', 'summary': 'empty layer'}
                    return encode_need('Write exact source:\n' + pending[owner]['content'] if pending[owner]['tool'] == 'write' else 'Perform ' + pending[owner]['tool'])
                state = load_and_upgrade_plan(None, 'Create a Value model with count 2')
                result = PodCoordinator(root, state, llm, save=lambda _: None).build()
                self.assertEqual(result['agent']['status'], 'complete')
                self.assertEqual(set(seen), {'models', 'providers', 'services', 'pipelines', 'interfaces', 'pod'})
                self.assertTrue(any(b['id'] == 'Value' for b in json.loads(Path('beans_config.json').read_text())['beans']))
                self.assertEqual(result['agent']['change_requests'][0]['status'], 'applied')
                self.assertEqual(Path('modules/models/value.py').read_text(), corrected)
            finally:
                os.chdir(previous)
