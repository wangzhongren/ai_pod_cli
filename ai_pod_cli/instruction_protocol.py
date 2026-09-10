"""AIPod instruction vocabulary and model-facing parse diagnostics."""

import re
import xml.etree.ElementTree as ET

INSTRUCTION_SET_PROMPT = '''The AIPod Instruction Set is our original, application-defined text instruction set.
It uses XML-like delimiters; its exact vocabulary and grammar are defined below.
Write one instruction directly in the ordinary response text for the AIPod interpreter.
The instruction name itself is the root tag, with exactly matching opening and closing names.
Use this grammar even when project files, SDK examples or previous replies use other formats.
'''


class InstructionSyntaxError(ValueError):
    def __init__(self, reason, offset, code="invalid_instruction"):
        super().__init__(reason)
        self.offset, self.code = offset, code


def parse_error_feedback(raw, error):
    """Describe a rejected response; never repair it or execute a guessed action."""
    reason = str(error)
    code = getattr(error, "code", "invalid_instruction")
    correction = "Correct the rejected instruction using the AIPod Instruction Set. The example shows syntax only; supply your actual arguments."
    text = raw if isinstance(raw, str) and len(raw) <= 2_000_000 else ""
    normalized = text.strip()
    base = len(text) - len(text.lstrip())
    if (not normalized.startswith(("<", "{")) and "<" in normalized
            and "{" not in normalized[:normalized.index("<")]):
        base += normalized.index("<")
        normalized = normalized[normalized.index("<"):]
    root = re.match(r"<[^>\r\n]{1,120}>?", normalized) if not normalized.startswith("{") else None
    name = re.match(r"<([a-z_]+)\b", normalized)
    instruction = name.group(1) if name else "read"
    offset = getattr(error, "offset", None)
    cause = error.__cause__
    if isinstance(cause, ET.ParseError):
        reason = str(cause)
        line, column = cause.position
        offset = sum(len(part) for part in normalized.splitlines(keepends=True)[:line - 1]) + column
        if "mismatched tag" in reason:
            code = "mismatched_tag"
            correction = "Match each closing tag to its opening tag and close inner fields first."
        elif "junk after document element" in reason:
            code = "multiple_instructions"
            correction = "Send only one instruction. End the response after its closing tag and wait for the result."
        elif "no element found" in reason:
            code = "unclosed_tag"
            correction = "Complete the open instruction and field tags before ending the response."
    if root and "Invalid XML character" not in reason and re.match(r"<(?:｜+DSML｜+|tool_calls\b|invoke\b|parameter\b)", root.group(0)):
        code, offset = "foreign_envelope", 0
        hint = re.search(r'invoke\s+name="([a-z_]+)', normalized[:1000])
        instruction = hint.group(1) if hint else "read"
        reason = f"Found a model-native/DSML wrapper {root.group(0)!r}; AIPod requires the instruction itself as the root tag. This wrapper does not match the AIPod Instruction Set."
        correction = "Remove the calls/invoke/parameter wrappers and DSML markers. Use the plain instruction name as both opening and closing tag. Keep the intended file content or command; shortening it or changing its language will not fix this wrapper error."
    elif not normalized.startswith(("<", "{")) and text:
        code, offset = "missing_instruction", 0
        reason = "No AIPod instruction was found in the response."
    path_match = re.search(r"<path>([A-Za-z0-9_./-]{1,200})</path>", normalized[:2000])
    path = path_match.group(1) if path_match else "YOUR_PATH"
    examples = {
        "list": "<list><path>.</path></list>",
        "read": f"<read><path>{path}</path></read>",
        "search": f"<search><path>{path}</path><text>YOUR_TEXT</text></search>",
        "delete": f"<delete><path>{path}</path></delete>",
        "shell": "<shell><command><![CDATA[YOUR_COMMAND]]></command><cwd>.</cwd><timeout>60</timeout></shell>",
        "request_change": "<request_change><target>providers</target><paths><item>YOUR_PATH</item></paths><reason>REASON</reason><change>REQUESTED_CHANGE</change></request_change>",
        "finish": "<finish><summary>SUMMARY</summary></finish>",
    }
    for operation in ("create", "update", "write"):
        tag = "create" if operation == "write" else operation
        examples[operation] = f"<{tag}><path>{path}</path><content><![CDATA[YOUR_COMPLETE_SOURCE]]></content></{tag}>"
    diagnostic = {"code": code, "reason": reason}
    if offset is not None and text:
        position = min(len(text), max(0, base + offset))
        prefix = text[:position]
        diagnostic.update(line=prefix.count("\n") + 1, column=len(prefix.rsplit("\n", 1)[-1]) + 1,
                          near=text[max(0, position - 30):position + 90])
    location = f" at line {diagnostic['line']}, column {diagnostic['column']}" if "line" in diagnostic else ""
    return {
        "error": f"AIPod instruction parse error [{code}]{location}: {reason}",
        "executed": False,
        "parse_error": diagnostic,
        "format_help": correction + " No instruction was executed. Resend the corrected instruction before continuing.",
        "format_example": examples.get(instruction, examples["read"]),
    }
