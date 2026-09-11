import unittest
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_pod_cli.workspace import parse_action
from ai_pod_cli.source_codec import encode_source_artifact
from ai_pod_cli.workspace import WorkspaceTools
from ai_pod_cli.workspace_agent import WorkspaceAgent
from ai_pod_cli.client import call_llm


class XmlActionTests(unittest.TestCase):
    def test_file_and_shell_actions(self):
        self.assertEqual(parse_action('<read><path>modules/models/game.py</path><offset>12</offset><limit>40</limit></read>'),
                         {'tool': 'read', 'path': 'modules/models/game.py', 'offset': 12, 'limit': 40})
        command = 'python -c "assert 1 < 2" && printf "a&b"\n'
        self.assertEqual(parse_action(f'<shell><command><![CDATA[{command}]]></command><timeout>60</timeout></shell>'),
                         {'tool': 'shell', 'command': command, 'timeout': 60})
        self.assertEqual(parse_action('<list/>'), {'tool': 'list'})
        self.assertEqual(parse_action('I will inspect the project.\n<list/>'), {'tool': 'list'})
        self.assertEqual(parse_action('<read><path>beans_config.json</path></｜｜DSML｜｜>'), {'tool':'read','path':'beans_config.json'})
        source = 'text = "<read> & 中文"\r\n'
        self.assertEqual(parse_action(encode_source_artifact('modules/models/game.py', source))['content'], source)
        update = encode_source_artifact('modules/models/game.py', source).replace('<create>', '<update>', 1)
        update = update[:-len('</create>')] + '</update>'
        self.assertEqual(parse_action(update), {'tool':'write', 'path':'modules/models/game.py', 'content':source})

    def test_nested_finish_and_owner_handoff(self):
        finish = parse_action('''<finish><summary>done</summary><components><item><id>Game</id>
<class_path>modules.models.game.Game</class_path><dependencies/><inputs><size><type>int</type><required>false</required></size></inputs>
<outputs/></item></components><remove/></finish>''')
        self.assertEqual(finish['components'][0]['dependencies'], [])
        self.assertEqual(finish['components'][0]['inputs'], {'size': {'type': 'int', 'required': False}})
        self.assertEqual(finish['components'][0]['outputs'], {})
        self.assertEqual(finish['remove'], [])
        self.assertEqual(parse_action('<request_change><target>providers</target><paths><item>modules/providers/impl/a.py</item></paths><reason>failure</reason><change>fix</change></request_change>')['paths'], ['modules/providers/impl/a.py'])

    def test_observed_native_xml_envelope(self):
        for marker in ('', '｜DSML｜', '｜｜DSML｜｜'):
            action = f'<{marker}tool_calls><{marker}invoke name="shell"><{marker}parameter name="command" string="true">printf "x&y" && test 1 -lt 2</{marker}parameter><{marker}parameter name="timeout" string="false">30</{marker}parameter></{marker}invoke></{marker}tool_calls>'
            self.assertEqual(parse_action(action), {'tool':'shell', 'command':'printf "x&y" && test 1 -lt 2', 'timeout':30})

    def test_rejects_ambiguous_and_unsafe_xml_without_executing(self):
        for raw in ('<read><path>a</path><path>b</path></read>', '<list/><shell><command>touch bad</command></shell>',
                    'I will read now.', '<finish bad="x"/>', '<finish>done</finish>',
                    '<!DOCTYPE list [<!ENTITY x SYSTEM "file:///etc/passwd">]><list><path>&x;</path></list>',
                    '<list><tool>shell</tool></list>', '<write><path>x</path><content>bad</content></write>'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_action(raw)
        one = '<invoke name="list"><parameter name="path" string="true">.</parameter></invoke>'
        with self.assertRaises(ValueError):
            parse_action('<tool_calls>' + one + one + '</tool_calls>')

    def test_executed_result_is_sent_as_the_next_conversation_turn(self):
        with tempfile.TemporaryDirectory() as root:
            tools = WorkspaceTools(root, 'models')
            (Path(root)/'modules/models/value.py').write_text('VALUE = 42\n')
            calls = []
            def llm(system, user, **options):
                calls.append(options['conversation'])
                return '<read><path>modules/models/value.py</path></read>' if len(calls) == 1 else '<finish><summary>observed</summary></finish>'
            result = WorkspaceAgent(llm, tools, instruction_mode="direct").run('Inspect existing value', {}, request_change=lambda *_: {}, finish=lambda action, _: action)
            self.assertEqual(result['summary'], 'observed')
            self.assertEqual([message['role'] for message in calls[1]], ['user', 'assistant', 'user'])
            self.assertIn('VALUE = 42', calls[1][-1]['content'])
            self.assertIn('Instructions remaining', calls[1][-1]['content'])

    def test_client_preserves_conversation_roles_and_results(self):
        transport = Mock()
        transport.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='<list/>'), finish_reason='stop')])
        conversation = [{'role':'user','content':'Build'}, {'role':'assistant','content':'<read><path>missing</path></read>'}, {'role':'user','content':'File does not exist. Create it.'}]
        with patch('ai_pod_cli.client.get_client', return_value=transport):
            self.assertEqual(call_llm('XML actions', 'fallback', conversation=conversation), '<list/>')
        self.assertEqual(transport.chat.completions.create.call_args.kwargs['messages'][1:], conversation)

    def test_empty_model_response_retries_without_poisoning_conversation(self):
        transport = Mock()
        def result(content):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason='stop')])
        transport.chat.completions.create.side_effect = [result(None), result('<list/>')]
        with patch('ai_pod_cli.client.get_client', return_value=transport):
            self.assertEqual(call_llm('XML actions', 'List files', retry_delay=0), '<list/>')
        self.assertEqual(transport.chat.completions.create.call_count, 2)

    def test_agent_remembers_the_source_it_just_wrote(self):
        with tempfile.TemporaryDirectory() as root:
            tools = WorkspaceTools(root, 'models')
            source = encode_source_artifact('modules/models/value.py', 'VALUE = 42\n')
            calls = []
            def llm(system, user, **options):
                calls.append(options['conversation'])
                return source if len(calls) == 1 else '<finish><summary>written</summary></finish>'
            WorkspaceAgent(llm, tools, instruction_mode="direct").run('Write a value', {}, request_change=lambda *_: {}, finish=lambda action, _: action)
            self.assertEqual(calls[1][1], {'role':'assistant','content':source})


if __name__ == '__main__':
    unittest.main()
