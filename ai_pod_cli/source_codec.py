"""Strict ActUnit-compatible create/path/content subset; decoding never writes files."""

import re
from ai_pod_cli.instruction_protocol import InstructionSyntaxError


def _valid_characters(value: str) -> None:
    for char in value:
        code = ord(char)
        if not (code in (9, 10, 13) or 0x20 <= code <= 0xD7FF
                or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF):
            raise ValueError("Invalid XML character in source artifact")


def _entities(value: str) -> str:
    if "]]>" in value:
        raise ValueError("CDATA terminator requires split CDATA sections")
    named = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}

    def replace(match):
        entity = match.group(1)
        if entity in named:
            return named[entity]
        if entity and re.fullmatch(r"(?:#[0-9]+|#x[0-9a-fA-F]+)", entity):
            try:
                code = int(entity[2:], 16) if entity.startswith("#x") else int(entity[1:])
                result = chr(code)
                _valid_characters(result)
                return result
            except (ValueError, OverflowError):
                pass
        raise ValueError("Invalid XML entity")

    return re.sub(r"&([^;]*);|&", replace, value)


def encode_source_artifact(path: str, content: str) -> str:
    _valid_characters(path)
    _valid_characters(content)
    path = path.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    content = content.replace("]]>", "]]]]><![CDATA[>")
    return f"<create><path>{path}</path><content><![CDATA[{content}]]></content></create>"


def decode_source_artifact(text: str, expected_path: str | None = None) -> dict[str, str]:
    if not isinstance(text, str):
        raise ValueError("Source generation must return XML text")
    if len(text) > 2_000_000:
        raise ValueError("Source artifact response exceeds 2,000,000 characters")
    _valid_characters(text)
    position = 0

    def fail(reason, offset=None, code="invalid_instruction"):
        raise InstructionSyntaxError(reason, position if offset is None else offset, code)

    def whitespace():
        nonlocal position
        while position < len(text) and text[position].isspace():
            position += 1

    def tag():
        nonlocal position
        match = re.match(r"<(\/?)(?:｜DSML｜)?([A-Za-z_][A-Za-z0-9_.-]*)\s*>", text[position:])
        if match is None:
            fail("Expected plain XML tag; attributes and declarations are not allowed")
        position += match.end()
        return match[1] + match[2]

    def scalar(name):
        nonlocal position
        chunks = []
        while position < len(text):
            marker = next((item for item in ("<![CDATA[", "<｜DSML｜CDATA[")
                           if text.startswith(item, position)), None)
            if marker:
                start = position + len(marker)
                end = text.find("]]>", start)
                if end < 0:
                    fail(f"Incomplete CDATA section: close with ]]> (not ]]]), then </{name}>", code="missing_cdata_end")
                chunks.append(text[start:end])
                position = end + 3
            elif text[position] == "<":
                start = position
                found = tag()
                if found != f"/{name}":
                    fail(f"Expected </{name}>, found <{found}>. Nested XML operands are not allowed; use CDATA for source.", start, "mismatched_tag")
                return "".join(chunks)
            else:
                end = text.find("<", position)
                if end < 0:
                    fail(f"Incomplete XML operand <{name}>: missing </{name}>", code="unclosed_tag")
                chunks.append(_entities(text[position:end]))
                position = end
        fail(f"Incomplete XML operand <{name}>: missing </{name}>", code="unclosed_tag")

    whitespace()
    if tag() != "create":
        fail("Expected exactly one create artifact action", 0)
    fields = {}
    while True:
        whitespace()
        start = position
        name = tag()
        if name == "/create":
            break
        if name not in ("path", "content") or name in fields:
            fail(f"Unknown or duplicate artifact operand <{name}>; supply <path> and <content> exactly once", start)
        fields[name] = scalar(name)
    whitespace()
    if position != len(text):
        fail("Expected exactly one artifact; trailing actions or text are not allowed", code="multiple_instructions")
    if set(fields) != {"path", "content"}:
        fail("Artifact requires path and content")
    if expected_path is not None and fields["path"] != expected_path:
        raise ValueError(f"Artifact path must equal planned path '{expected_path}'")
    return fields
