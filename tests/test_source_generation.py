"""Protocol and channel regression checks for Python source generation."""
import unittest
import io
import json
import os
import re
import tempfile
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from ai_pod_cli.client import call_llm, get_client
from ai_pod_cli.source_codec import decode_source_artifact, encode_source_artifact
from ai_pod_cli.source_generation import generate_source
from ai_pod_cli.commands.create import handle_create
from ai_pod_cli.commands.compose import handle_compose
from ai_pod_cli.entry_generator import generate_entry
from ai_pod_cli.config import init_config_if_not_exists


class SourceGenerationTests(unittest.TestCase):
    def test_progress_distinguishes_metadata_and_source_without_changing_labels(self):
        events, original_events, labels = [], [], []
        label = "Generating component 2/2: UserAccount"
        def llm(_system, _user, **options):
            labels.append(options["progress_label"])
            for event_type in ("llm_started", "llm_delta", "llm_completed"):
                event = {"type": event_type, "label": label,
                         "characters": 665 if options["json_mode"] else 1469}
                original_events.append(event)
                options["progress_callback"](event)
            return {} if options["json_mode"] else encode_source_artifact("account.py", "class UserAccount: pass\n")
        result = generate_source(llm, "rules", "build", "account.py",
                                 progress_label=label, progress_callback=events.append)
        self.assertIn("UserAccount", result["code"])
        self.assertEqual(labels, [label, label])
        self.assertEqual([event["generation_phase"] for event in events], ["metadata"] * 3 + ["source"] * 3)
        self.assertTrue(all(event["generation_attempt"] == 1 for event in events))
        self.assertTrue(all("generation_phase" not in event for event in original_events))

    def test_xml_progress_retry_does_not_repeat_metadata_phase(self):
        events, modes = [], []
        def llm(_system, _user, **options):
            modes.append(options["json_mode"])
            options["progress_callback"]({"type": "llm_started", "label": "Generating component 1/1: Item"})
            if options["json_mode"]:
                return {}
            return "invalid XML" if len(modes) == 2 else encode_source_artifact("item.py", "value = 1\n")
        generate_source(llm, "rules", "build", "item.py", progress_callback=events.append)
        self.assertEqual(modes, [True, False, False])
        self.assertEqual([(event["generation_phase"], event["generation_attempt"]) for event in events],
                         [("metadata", 1), ("source", 1), ("source", 2)])

    def test_frozen_metadata_emits_only_source_progress(self):
        events = []
        def llm(_system, _user, **options):
            self.assertFalse(options["json_mode"])
            options["progress_callback"]({"type": "llm_started"})
            return encode_source_artifact("app.py", "value = 1\n")
        generate_source(llm, "rules", "build", "app.py", frozen_metadata={}, progress_callback=events.append)
        self.assertEqual(events, [{"type": "llm_started", "generation_phase": "source", "generation_attempt": 1}])

    def test_client_default_timeout_and_explicit_environment_override(self):
        for environment, expected in [({}, 600), ({"OPENAI_TIMEOUT_SECONDS": "45"}, 45)]:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test", **environment}, clear=True), patch(
                "ai_pod_cli.client._client", None,
            ), patch("ai_pod_cli.client.OpenAI") as constructor:
                get_client()
                self.assertEqual(constructor.call_args.kwargs["timeout"], expected)

    def test_client_channel_budgets_and_truncation_retry_ceiling(self):
        requests = []
        def create(**kwargs):
            requests.append(dict(kwargs))
            content = '{}' if 'response_format' in kwargs else 'source'
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                finish_reason='length' if len(requests) == 1 else 'stop')], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch("ai_pod_cli.client.get_client", return_value=client), patch("ai_pod_cli.client.get_model", return_value="test"), redirect_stdout(io.StringIO()):
            call_llm("plan", "", json_mode=True, max_retries=2, retry_delay=0)
            call_llm("source", "", max_retries=1)
            call_llm("explicit", "", json_mode=True, max_tokens=512, max_retries=1)
        self.assertEqual([r['max_tokens'] for r in requests], [32768, 65536, 65536, 512])

    def test_pipeline_command_requests_xml_and_keeps_planned_filename(self):
        modes = []
        def llm(*args, **options):
            modes.append(options["json_mode"])
            if options["json_mode"]:
                return {"pipeline_ids": ["Base"], "inputs": {},
                        "verification_cases": [{"name": "default", "params": {}}]}
            return encode_source_artifact("pipelines/demo.py", "def run(ctx):\n    return ctx.summary()\n")
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                with redirect_stdout(io.StringIO()):
                    init_config_if_not_exists()
                    config = json.loads(Path("beans_config.json").read_text())
                    config["beans"].append({"id": "Base", "category": "service", "class_path": "modules.services.base.Base", "inputs": {}, "outputs": {}})
                    Path("beans_config.json").write_text(json.dumps(config))
                    with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch(
                        "ai_pod_cli.commands.compose.call_llm", side_effect=llm,
                    ), patch("ai_pod_cli.commands.compose.verify_pipeline_candidate", return_value=[]):
                        self.assertTrue(handle_compose(SimpleNamespace(name="demo", cmd="Run Base", json=True, list=False)))
                self.assertIn("def run(ctx)", Path("pipelines/demo.py").read_text())
                self.assertEqual(modes, [True, False])
            finally:
                os.chdir(previous)

    def test_legacy_entry_uses_xml_and_rejects_nonlocal_metadata_path(self):
        previous = Path.cwd()
        for filename in ("app.py", "../escape.py"):
            modes = []
            def llm(*args, **options):
                modes.append(options["json_mode"])
                return {"entry_file": filename, "extra_deps": []} if options["json_mode"] else encode_source_artifact(filename, "print('hello')\n")
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    os.chdir(tmp)
                    with redirect_stdout(io.StringIO()), patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch(
                        "ai_pod_cli.entry_generator.call_llm", side_effect=llm,
                    ):
                        result = generate_entry("simple CLI")
                    if filename == "app.py":
                        self.assertEqual(result, ("app.py", []))
                        self.assertEqual(Path("app.py").read_text(), "print('hello')\n")
                        self.assertEqual(modes, [True, False])
                    else:
                        self.assertIsNone(result)
                        self.assertEqual(modes, [True])
                finally:
                    os.chdir(previous)

    def test_create_command_commits_xml_source_with_json_metadata(self):
        source = "from ai_pod_cli import Model\nclass Sample(Model):\n    value: int = 1\n"
        test_source = (
            "import unittest\nfrom ai_pod_cli.testing import Sandbox\n"
            "class ComponentTests(unittest.TestCase):\n"
            "    def test_default_value(self):\n"
            "        with Sandbox() as s:\n"
            "            item = s.model('Sample', {})\n"
            "            self.assertEqual(item.value, 1)\n"
        )
        modes = []
        def llm(*args, **options):
            modes.append(options["json_mode"])
            if options["json_mode"]:
                return {"dependencies": [], "inputs": {}, "outputs": {}, "extra_deps": [],
                        "tests": [{"name": "test_default_value", "requirement": "Default value is 1"}]}
            test_path = re.search(r"tests/components/Sample_[a-f0-9]+\.py", args[0])
            if test_path:
                self.assertFalse(Path("modules/models/sample.py").exists())
                return encode_source_artifact(test_path.group(), test_source)
            return encode_source_artifact("modules/models/sample.py", source)
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                with redirect_stdout(io.StringIO()):
                    init_config_if_not_exists()
                    with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch(
                        "ai_pod_cli.commands.create.call_llm", side_effect=llm,
                    ):
                        handle_create(SimpleNamespace(name="Sample", category="model", desc="Sample data", json=True))
                self.assertEqual(Path("modules/models/sample.py").read_text(), source)
                beans = json.loads(Path("beans_config.json").read_text())["beans"]
                self.assertTrue(any(bean["id"] == "Sample" for bean in beans))
                self.assertEqual(modes, [True, False, False])
                self.assertEqual(len(list(Path("tests/components").glob("Sample_*.py"))), 1)
            finally:
                os.chdir(previous)

    def test_xml_roundtrip_preserves_source(self):
        for source in [
            'print("中文 😀 & <tag>")\r\n',
            'text = "</content></create><shell>"\n',
            'text = "]]> <｜DSML｜read>"\n',
        ]:
            with self.subTest(source=source):
                artifact = decode_source_artifact(encode_source_artifact("app.py", source), "app.py")
                self.assertEqual(artifact, {"path": "app.py", "content": source})

    def test_invalid_xml_cannot_select_another_path_or_action(self):
        valid = encode_source_artifact("app.py", "print(1)\n")
        cases = [valid + valid, "prose" + valid, valid + "<shell></shell>",
                 valid.replace("app.py", "../app.py"), valid.replace("<create>", '<create x="1">'),
                 valid.replace("<content>", "<path>app.py</path><content>"),
                 valid.replace("<![CDATA[print(1)\n]]>", "<nested>x</nested>"),
                 valid.replace("<![CDATA[print(1)\n]]>", "a & b"),
                 valid.replace("<![CDATA[print(1)\n]]>", "&#0;"),
                 valid.replace("]]>", ""), valid.replace("</create>", ""),
                 '<!DOCTYPE create [<!ENTITY x "bad">]>' + valid]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    decode_source_artifact(value, "app.py")

    def test_dsml_and_escaped_xml(self):
        value = '<｜DSML｜create><｜DSML｜path>app.py</｜DSML｜path><｜DSML｜content><｜DSML｜CDATA[x & <x>]]></｜DSML｜content></｜DSML｜create>'
        self.assertEqual(decode_source_artifact(value, "app.py")["content"], "x & <x>")
        value = '<create><path>app.py</path><content>&lt;x&gt; &amp; &#x1F600;</content></create>'
        self.assertEqual(decode_source_artifact(value, "app.py")["content"], '<x> & 😀')

    def test_metadata_is_frozen_while_xml_is_retried(self):
        calls = []
        metadata = {"dependencies": [], "inputs": {"amount": "int"}, "outputs": {"total": "int"}}
        def llm(system, user, **options):
            calls.append(options["json_mode"])
            if options["json_mode"]:
                return metadata
            self.assertIn('"total": "int"', user)
            if len(calls) == 2:
                return encode_source_artifact("other.py", "print(1)")
            self.assertIn("Artifact path must equal", user)
            return encode_source_artifact("app.py", "print(1)\n")
        result = generate_source(llm, "component rules", "build", "app.py")
        self.assertEqual(calls, [True, False, False])
        self.assertEqual(result["code"], "print(1)\n")
        self.assertEqual(result["outputs"], metadata["outputs"])
        self.assertNotIn("code", metadata)

    def test_metadata_source_is_discarded_and_xml_remains_mandatory(self):
        for content_key in ("code", "content"):
            metadata = {"code": "untrusted_old_code", "content": "untrusted_old_content",
                        "inputs": {"content": "str"}, "outputs": {"code": "str"}}
            modes = []
            def llm(system, user, **options):
                modes.append(options["json_mode"])
                if options["json_mode"]:
                    return metadata
                self.assertNotIn("untrusted_old_", user)
                return encode_source_artifact("app.py", "print('XML')")
            result = generate_source(llm, "rules", "build", "app.py", content_key=content_key)
            self.assertEqual(modes, [True, False])
            self.assertEqual(result[content_key], "print('XML')")
            self.assertNotIn("content" if content_key == "code" else "code", result)
            self.assertEqual(result["inputs"], {"content": "str"})
            self.assertEqual(result["outputs"], {"code": "str"})
            self.assertEqual(metadata["code"], "untrusted_old_code")

    def test_discarded_metadata_source_is_never_an_xml_failure_fallback(self):
        def llm(*args, **options):
            return {"code": "print('old')"} if options["json_mode"] else "broken XML"
        with self.assertRaises(ValueError):
            generate_source(llm, "rules", "build", "app.py")

    def test_metadata_path_is_checked_before_requesting_source(self):
        modes = []
        def llm(*args, **options):
            modes.append(options["json_mode"])
            return {"path": "../escape.py", "code": "print('old')"}
        with self.assertRaisesRegex(ValueError, "planned path"):
            generate_source(llm, "rules", "build", "app.py")
        self.assertEqual(modes, [True])

    def test_invalid_xml_retries_are_bounded(self):
        modes = []
        def llm(*args, **options):
            modes.append(options["json_mode"])
            return {} if options["json_mode"] else '<create><path>app.py</path>'
        with self.assertRaises(ValueError):
            generate_source(llm, "rules", "build", "app.py")
        self.assertEqual(modes, [True, False, False, False])

    def test_transport_only_enables_json_mode_for_metadata(self):
        requests = []
        def create(**kwargs):
            requests.append(kwargs)
            raw = '{"outputs":{}}' if "response_format" in kwargs else encode_source_artifact("app.py", "print(1)")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch("ai_pod_cli.client.get_client", return_value=client), patch("ai_pod_cli.client.get_model", return_value="test"):
            result = generate_source(call_llm, "metadata rules", "build", "app.py", max_retries=1)
        self.assertEqual(result["code"], "print(1)")
        self.assertEqual(requests[0]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", requests[1])
        self.assertEqual(requests[1]["max_tokens"], 65536)
        self.assertEqual(requests[1]["timeout"], 600)
        self.assertNotIn("timeout", requests[0])
        self.assertTrue(requests[0]["messages"][0]["content"].endswith("Return a strict JSON object."))
        self.assertFalse(requests[1]["messages"][0]["content"].endswith("Return a strict JSON object."))

    def test_source_budget_does_not_raise_metadata_budget_and_can_be_overridden(self):
        requests = []
        def llm(*args, **options):
            requests.append(options)
            return {} if options["json_mode"] else encode_source_artifact("app.py", "print(1)")
        generate_source(llm, "rules", "build", "app.py", max_tokens=512)
        self.assertEqual(requests[0]["max_tokens"], 512)
        self.assertNotIn("timeout_seconds", requests[0])
        self.assertEqual(requests[1]["max_tokens"], 65536)
        self.assertEqual(requests[1]["timeout_seconds"], 600)
        requests.clear()
        generate_source(llm, "rules", "build", "app.py", max_tokens=512,
                        source_max_tokens=8192, source_timeout_seconds=120)
        self.assertEqual(requests[1]["max_tokens"], 8192)
        self.assertEqual(requests[1]["timeout_seconds"], 120)
