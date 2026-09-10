"""One XML-like action per response, including file, shell and handoff actions."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from ai_pod_cli.source_codec import decode_source_artifact, _valid_characters

FIELDS = {
    "list": {"path"}, "read": {"path", "offset", "limit"},
    "search": {"path", "text"}, "delete": {"path"},
    "shell": {"command", "cwd", "timeout"},
    "request_change": {"target", "paths", "reason", "change"},
    "finish": {"summary", "components", "pipelines", "routes", "interfaces", "remove"},
    "write": {"path", "content"},
}
LISTS = {"paths", "components", "pipelines", "routes", "interfaces", "remove", "dependencies", "services", "artifacts", "tests"}
OBJECTS = {"inputs", "outputs", "methods", "execution", "adapter", "lifecycle"}
TEXT = {"path", "file", "id", "name", "class_path", "command", "cwd", "text", "content", "target", "reason", "change", "summary", "description"}


def scalar(name, value):
    if name in TEXT:
        return value if name in {"command", "content", "text"} else value.strip()
    value = value.strip()
    if not value:
        return [] if name in LISTS else {} if name in OBJECTS else ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def checked(tool, arguments):
    if tool not in FIELDS or not isinstance(arguments, dict):
        raise ValueError("Unknown XML action or invalid operands")
    if set(arguments) - FIELDS[tool]:
        raise ValueError(f"Unknown operands for {tool}: {sorted(set(arguments) - FIELDS[tool])}")
    return {"tool": tool, **arguments}


def _native_call(raw):
    # Some models emit their native XML-like envelope. Parameters are literal text,
    # so shell operators such as && must not be interpreted as XML entities.
    marker = r"(?:｜+DSML｜+)?"
    outer = re.fullmatch(rf"<{marker}tool_calls>\s*<{marker}invoke\s+name=\"([a-z_]+)\">([\s\S]*)</{marker}invoke>\s*</{marker}tool_calls>", raw)
    if not outer:
        raise ValueError("Return exactly one XML-like action, without prose or additional calls")
    tool, body = outer.groups()
    arguments, position = {}, 0
    pattern = re.compile(rf'\s*<{marker}parameter\s+name="([a-z_]+)"\s+string="(true|false)">([\s\S]*?)</{marker}parameter>')
    while body[position:].strip():
        match = pattern.match(body, position)
        if not match:
            raise ValueError("Invalid or multiple native XML-like tool calls")
        name, string, value = match.groups()
        if name in arguments:
            raise ValueError("Duplicate action operand")
        arguments[name] = value if string == "true" else json.loads(value)
        position = match.end()
    return checked("write" if tool in {"create", "update", "write"} else tool, arguments)


def decode_action(raw: str):
    if not isinstance(raw, str) or len(raw) > 2_000_000:
        raise ValueError("Action must be text under 2,000,000 characters")
    raw = raw.strip()
    _valid_characters(raw)
    # A model may preface its single action with a sentence. The actual action is
    # still parsed in full, so extra actions/trailing material are never discarded.
    if not raw.startswith(("<", "{")) and "<" in raw and "{" not in raw[:raw.index("<")]:
        raw = raw[raw.index("<"):]
    root_tag = re.match(r"<([a-z_]+)\s*>", raw)
    anonymous_end = re.search(r"</｜+DSML｜+>$", raw)
    if root_tag and anonymous_end:
        raw = raw[:anonymous_end.start()] + f"</{root_tag.group(1)}>"
    # Keep old clients and saved scripted runs readable. Agents are prompted in XML.
    if raw.startswith("{"):
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("tool"), str):
            raise ValueError("Return exactly one tool action")
        if value["tool"] == "write":
            raise ValueError("File source must use XML/CDATA, not JSON")
        return value
    if re.match(r"<(?:｜+DSML｜+)?tool_calls>", raw):
        return _native_call(raw)
    update = re.match(r"<(?:｜DSML｜)?update\s*>", raw)
    update_end = re.search(r"</(?:｜DSML｜)?update\s*>$", raw)
    if update and update_end:
        return {"tool": "write", **decode_source_artifact("<create>" + raw[update.end():update_end.start()] + "</create>")}
    if re.match(r"<(?:｜DSML｜)?create\s*>", raw):
        return {"tool": "write", **decode_source_artifact(raw)}
    without_cdata = re.sub(r"<!\[CDATA\[[\s\S]*?\]\]>", "", raw)
    if re.search(r"<!|<\?", without_cdata):
        raise ValueError("XML declarations, entities and comments are not action operands")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError("Return exactly one XML-like action, without prose or trailing actions") from error

    def value(node):
        if node.attrib or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", node.tag):
            raise ValueError("Use plain XML operand tags without attributes")
        children = list(node)
        if not children:
            return scalar(node.tag, node.text or "")
        if (node.text or "").strip() or any((child.tail or "").strip() for child in children):
            raise ValueError("Do not mix text and nested XML operands")
        if all(child.tag == "item" for child in children):
            return [value(child) for child in children]
        result = {}
        for child in children:
            if child.tag in result:
                raise ValueError("Duplicate XML operand; use <item> for arrays")
            result[child.tag] = value(child)
        return result

    if root.tag == "write":
        raise ValueError("Source writes use <create> with content in CDATA")
    arguments = value(root)
    if not list(root):
        if (root.text or "").strip():
            raise ValueError("Action arguments must use named XML operands")
        arguments = {}
    return checked(root.tag, arguments)
