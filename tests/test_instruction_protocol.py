"""Regression cases from malformed workspace replies, without external model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ai_pod_cli.instruction_protocol import parse_error_feedback, INSTRUCTION_EXAMPLES
from ai_pod_cli.source_codec import encode_source_artifact
from ai_pod_cli.workspace import WorkspaceTools, parse_action
from ai_pod_cli.workspace_agent import WorkspaceAgent


class InstructionProtocolTests(unittest.TestCase):
    def test_each_instruction_has_a_complete_parseable_example(self):
        self.assertEqual(list(INSTRUCTION_EXAMPLES), ['list', 'read', 'search', 'create', 'update', 'delete', 'shell', 'request_change', 'finish'])
        for name, example in INSTRUCTION_EXAMPLES.items():
            self.assertEqual(parse_action(example)['tool'], 'write' if name in {'create', 'update'} else name)

    def test_rejected_syntax_is_not_replayed_but_valid_source_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            tools = WorkspaceTools(root, 'models')
            source = 'label = "<｜｜DSML｜｜ calls>"\n'
            valid = encode_source_artifact('modules/models/value.py', source)
            broken_summary = '<finish><summary>Check A && B</summary></finish>'
            calls = []
            def llm(system, user, **options):
                conversation = options['conversation']
                calls.append(conversation)
                if len(calls) <= 2:
                    if len(calls) == 2:
                        self.assertNotIn('DSML', json.dumps(conversation))
                        self.assertNotIn('DSML', user)
                    return '<｜｜DSML｜｜ calls><read><path>modules/models/value.py</path></read>'
                if len(calls) == 3:
                    self.assertNotIn('DSML', json.dumps(conversation))
                    return valid
                self.assertTrue(any(m['role'] == 'assistant' and m['content'] == valid for m in conversation))
                if len(calls) == 4:
                    return broken_summary
                self.assertFalse(any(m['content'] == broken_summary for m in conversation))
                self.assertIn('CDATA', conversation[-1]['content'])
                return '<finish><summary><![CDATA[Check A && B]]></summary></finish>'
            result = WorkspaceAgent(llm, tools, max_steps=5, instruction_mode="direct").run('Write a label', {}, request_change=Mock(), finish=lambda action, _: action)
            self.assertEqual(result['summary'], 'Check A && B')
            self.assertEqual(Path(root, 'modules/models/value.py').read_text(), source)

    def feedback(self, raw):
        try:
            parse_action(raw)
        except ValueError as error:
            return parse_error_feedback(raw, error)
        self.fail("Malformed instruction was accepted")

    def test_observed_dsml_hybrid_identifies_the_wrapper_not_file_content(self):
        raw = 'Checking the file.\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="read"><path>app.py</path></read>'
        result = self.feedback(raw)
        self.assertFalse(result['executed'])
        self.assertEqual(result['parse_error']['code'], 'foreign_envelope')
        self.assertEqual((result['parse_error']['line'], result['parse_error']['column']), (2, 1))
        self.assertIn('<｜｜DSML｜｜ calls>', result['parse_error']['reason'])
        self.assertEqual(result['format_example'], '<read><path>app.py</path></read>')
        self.assertIn('shortening', result['format_help'])

    def test_syntax_diagnostics_include_the_actual_reason_and_position(self):
        cases = [
            ('<read>\n<path>app.py</path>\n</shell>', 'mismatched_tag', 3, '</shell>'),
            ('<shell><command><![CDATA[echo hello</command></shell>', 'missing_cdata_end', 1, ']]>'),
            ('<create><path>app.py</path><content><![CDATA[x = 1</content></create>', 'missing_cdata_end', 1, ']]>'),
            ('<read><path>app.py</path></read>\n<list/>', 'multiple_instructions', 2, 'one instruction'),
            ('<read><path>app.py</path>', 'unclosed_tag', 1, 'instruction'),
            ('<!DOCTYPE read><read/>', 'unsupported_markup', 1, 'declarations'),
        ]
        for raw, code, line, detail in cases:
            with self.subTest(raw=raw):
                result = self.feedback(raw)
                self.assertEqual(result['parse_error']['code'], code)
                self.assertEqual(result['parse_error']['line'], line)
                self.assertGreaterEqual(result['parse_error']['column'], 1)
                self.assertIn(detail, json.dumps(result))

    def test_duplicate_and_unknown_operands_name_the_problem(self):
        self.assertIn('<path>', self.feedback('<read><path>a</path><path>b</path></read>')['parse_error']['reason'])
        self.assertIn('extra', self.feedback('<read><path>a</path><extra>1</extra></read>')['parse_error']['reason'])
        self.assertIn('<fly>', self.feedback('<fly/>')['parse_error']['reason'])

    def test_dsml_literal_in_source_is_not_diagnosed_as_a_wrapper(self):
        source = 'label = "<｜｜DSML｜｜ calls>"\n'
        valid = encode_source_artifact('modules/models/value.py', source)
        self.assertEqual(parse_action(valid)['content'], source)
        broken = valid[:-len('</create>')] + '</wrong>'
        self.assertNotEqual(self.feedback(broken)['parse_error']['code'], 'foreign_envelope')

    def test_agent_receives_diagnostics_then_can_correct_without_partial_execution(self):
        with tempfile.TemporaryDirectory() as root:
            tools = WorkspaceTools(root, 'models')
            path = Path(root, 'modules/models/value.py')
            raw = '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="create"><path>modules/models/value.py</path><content><![CDATA[VALUE = 42\n]]></content></create>'
            calls = []
            def llm(system, user, **options):
                calls.append(options['conversation'])
                self.assertIn('original, application-defined text instruction set', system)
                if len(calls) == 1:
                    return raw
                if len(calls) == 2:
                    self.assertFalse(path.exists())
                    feedback = calls[-1][-1]['content']
                    self.assertIn('foreign_envelope', feedback)
                    self.assertIn('nothing was executed', feedback)
                    self.assertIn('format_example', feedback)
                    return encode_source_artifact('modules/models/value.py', 'VALUE = 42\n')
                return '<finish><summary>done</summary></finish>'
            changed = Mock()
            result = WorkspaceAgent(llm, tools, max_steps=3, instruction_mode="direct").run('Create a value', {}, request_change=changed, finish=lambda action, _: action)
            self.assertEqual(result['summary'], 'done')
            self.assertEqual(path.read_text(), 'VALUE = 42\n')
            changed.assert_not_called()

    def test_execution_errors_are_not_reported_as_parse_errors(self):
        with tempfile.TemporaryDirectory() as root:
            tools = WorkspaceTools(root, 'models')
            calls = []
            def llm(system, user, **options):
                calls.append(options['conversation'])
                return '<read><path>missing.py</path></read>' if len(calls) == 1 else '<finish><summary>observed</summary></finish>'
            WorkspaceAgent(llm, tools, max_steps=2, instruction_mode="direct").run('Inspect', {}, request_change=Mock(), finish=lambda action, _: action)
            self.assertIn('FileNotFoundError', calls[1][-1]['content'])
            self.assertNotIn('parse_error', calls[1][-1]['content'])
