"""Protocol and channel regression checks for Python source generation."""
import unittest
import io
import json
import os
import tempfile
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from ai_pod_cli.client import call_llm
from ai_pod_cli.source_codec import decode_source_artifact, encode_source_artifact
from ai_pod_cli.source_generation import generate_source
from ai_pod_cli.commands.create import handle_create
from ai_pod_cli.commands.compose import handle_compose
from ai_pod_cli.entry_generator import generate_entry
from ai_pod_cli.config import init_config_if_not_exists


class SourceGenerationTests(unittest.TestCase):
    def test_pipeline_command_requests_xml_and_keeps_planned_filename(self):
        modes = []
        def llm(*args, **options):
            modes.append(options["json_mode"])
            if options["json_mode"]:
                return {"pipeline_ids": ["Base"]}
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
        modes = []
        def llm(*args, **options):
            modes.append(options["json_mode"])
            if options["json_mode"]:
                return {"dependencies": [], "inputs": {}, "outputs": {}, "extra_deps": []}
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
                self.assertEqual(modes, [True, False])
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

    def test_source_cannot_be_smuggled_into_metadata(self):
        with self.assertRaisesRegex(ValueError, "must not contain source"):
            generate_source(lambda *a, **k: {"code": "print(1)"}, "rules", "build", "app.py")

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
        self.assertEqual(requests[1]["max_tokens"], 32768)
        self.assertEqual(requests[1]["timeout"], 300)
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
        self.assertEqual(requests[1]["max_tokens"], 32768)
        self.assertEqual(requests[1]["timeout_seconds"], 300)
        requests.clear()
        generate_source(llm, "rules", "build", "app.py", max_tokens=512,
                        source_max_tokens=8192, source_timeout_seconds=120)
        self.assertEqual(requests[1]["max_tokens"], 8192)
        self.assertEqual(requests[1]["timeout_seconds"], 120)
