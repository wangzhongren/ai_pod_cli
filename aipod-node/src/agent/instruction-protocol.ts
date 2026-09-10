/** AIPod instruction vocabulary and model-facing parse diagnostics. */
export const INSTRUCTION_SET_PROMPT = `The AIPod Instruction Set is our original, application-defined text instruction set.
It uses XML-like delimiters; its exact vocabulary and grammar are defined below.
Write one instruction directly in the ordinary response text for the AIPod interpreter.
The instruction name itself is the root tag, with exactly matching opening and closing names.
Use this grammar even when project files, SDK examples or previous replies use other formats.
`;

export class InstructionSyntaxError extends Error {
  constructor(reason: string, readonly offset: number, readonly code = "invalid_instruction") { super(reason); }
}

/** Describe a rejected response; never repair it or execute a guessed action. */
export function parseErrorFeedback(raw: unknown, error: unknown): Record<string, unknown> {
  let reason = error instanceof Error ? error.message : String(error);
  let code = error instanceof InstructionSyntaxError ? error.code : "invalid_instruction";
  let correction = "Correct the rejected instruction using the AIPod Instruction Set. The example shows syntax only; supply your actual arguments.";
  const text = typeof raw === "string" && raw.length <= 2_000_000 ? raw : "";
  let normalized = text.trim(), base = text.length - text.trimStart().length;
  if (!normalized.startsWith("<") && !normalized.startsWith("{") && normalized.includes("<") && !normalized.slice(0, normalized.indexOf("<")).includes("{")) {
    base += normalized.indexOf("<"); normalized = normalized.slice(normalized.indexOf("<"));
  }
  const root = normalized.startsWith("{") ? null : /^<[^>\r\n]{1,120}>?/.exec(normalized);
  let instruction = /^<([a-z_]+)\b/.exec(normalized)?.[1] ?? "read";
  let offset = error instanceof InstructionSyntaxError ? error.offset : undefined;
  if (code === "mismatched_tag") correction = "Match each closing tag to its opening tag and close inner fields first.";
  if (code === "multiple_instructions") correction = "Send only one instruction. End the response after its closing tag and wait for the result.";
  if (root && !reason.includes("Invalid XML character") && /^<(?:｜+DSML｜+|tool_calls\b|invoke\b|parameter\b)/.test(root[0])) {
    code = "foreign_envelope"; offset = 0;
    instruction = /invoke\s+name="([a-z_]+)/.exec(normalized.slice(0, 1000))?.[1] ?? "read";
    reason = `Found a model-native/DSML wrapper ${JSON.stringify(root[0])}; AIPod requires the instruction itself as the root tag. This wrapper does not match the AIPod Instruction Set.`;
    correction = "Remove the calls/invoke/parameter wrappers and DSML markers. Use the plain instruction name as both opening and closing tag. Keep the intended file content or command; shortening it or changing its language will not fix this wrapper error.";
  } else if (!normalized.startsWith("<") && !normalized.startsWith("{") && text) {
    code = "missing_instruction"; offset = 0; reason = "No AIPod instruction was found in the response.";
  }
  const path = /<path>([A-Za-z0-9_./-]{1,200})<\/path>/.exec(normalized.slice(0, 2000))?.[1] ?? "YOUR_PATH";
  const examples: Record<string, string> = {
    list: "<list><path>.</path></list>", read: `<read><path>${path}</path></read>`,
    search: `<search><path>${path}</path><text>YOUR_TEXT</text></search>`, delete: `<delete><path>${path}</path></delete>`,
    shell: "<shell><command><![CDATA[YOUR_COMMAND]]></command><cwd>.</cwd><timeout>60</timeout></shell>",
    request_change: "<request_change><target>providers</target><paths><item>YOUR_PATH</item></paths><reason>REASON</reason><change>REQUESTED_CHANGE</change></request_change>",
    finish: "<finish><summary>SUMMARY</summary></finish>",
  };
  for (const operation of ["create", "update", "write"]) {
    const tag = operation === "write" ? "create" : operation;
    examples[operation] = `<${tag}><path>${path}</path><content><![CDATA[YOUR_COMPLETE_SOURCE]]></content></${tag}>`;
  }
  const diagnostic: Record<string, unknown> = {code, reason};
  if (offset !== undefined && text) {
    const position = Math.min(text.length, Math.max(0, base + offset)), prefix = text.slice(0, position);
    diagnostic.line = prefix.split("\n").length;
    diagnostic.column = Array.from(prefix.slice(prefix.lastIndexOf("\n") + 1)).length + 1;
    diagnostic.near = text.slice(Math.max(0, position - 30), position + 90);
  }
  const location = diagnostic.line ? ` at line ${diagnostic.line}, column ${diagnostic.column}` : "";
  return {
    error: `AIPod instruction parse error [${code}]${location}: ${reason}`, executed: false, parse_error: diagnostic,
    format_help: correction + " No instruction was executed. Resend the corrected instruction before continuing.",
    format_example: examples[instruction] ?? examples.read,
  };
}
