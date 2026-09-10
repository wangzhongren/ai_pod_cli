import { INSTRUCTION_SET_PROMPT, InstructionSyntaxError } from "./instruction-protocol.js";

/** Strict ActUnit-compatible create/path/content subset, not a general XML parser.
 * Exactly one candidate artifact is decoded; no action is executed here.
 */
export interface SourceArtifact {
  path: string;
  content: string;
}

export function validCharacters(value: string): void {
  for (const character of value) {
    const code = character.codePointAt(0)!;
    if (!(code === 9 || code === 10 || code === 13 ||
      (code >= 0x20 && code <= 0xd7ff) || (code >= 0xe000 && code <= 0xfffd) || code >= 0x10000)) {
      throw new Error("Invalid XML character in source artifact");
    }
  }
}

export function decodeEntities(value: string): string {
  if (value.includes("]]>")) throw new Error("CDATA terminator must be inside split CDATA sections");
  return value.replace(/&([^;]*);|&/g, (match, entity: string | undefined) => {
    const named: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };
    if (entity && Object.hasOwn(named, entity)) return named[entity]!;
    if (entity && /^(?:#[0-9]+|#x[0-9a-fA-F]+)$/.test(entity)) {
      const code = entity.startsWith("#x") ? Number.parseInt(entity.slice(2), 16) : Number(entity.slice(1));
      if (code >= 0 && code <= 0x10ffff) {
        const decoded = String.fromCodePoint(code);
        validCharacters(decoded);
        return decoded;
      }
    }
    throw new Error(`Invalid XML entity '${match}'`);
  });
}

export function encodeSourceArtifact(artifact: SourceArtifact): string {
  validCharacters(artifact.path);
  validCharacters(artifact.content);
  const path = artifact.path.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
  const content = artifact.content.replaceAll("]]>", "]]]]><![CDATA[>");
  return `<create><path>${path}</path><content><![CDATA[${content}]]></content></create>`;
}

export function decodeSourceArtifact(text: string, expectedPath?: string): SourceArtifact {
  if (text.length > 2_000_000) throw new Error("Source artifact response exceeds 2,000,000 characters");
  validCharacters(text);
  let position = 0;
  const fail = (reason: string, code = "invalid_instruction", at = position): never => { throw new InstructionSyntaxError(reason, at, code); };
  const whitespace = () => { while (/\s/.test(text[position] ?? "") && position < text.length) position += 1; };
  const tag = (): string => {
    const match = /^<(\/?)(?:｜DSML｜)?([A-Za-z_][A-Za-z0-9_.-]*)\s*>/.exec(text.slice(position));
    if (!match) return fail("Expected a plain XML tag; attributes, declarations and nested content are not allowed");
    position += match[0].length;
    return `${match[1]}${match[2]}`;
  };
  const scalar = (name: string): string => {
    let value = "";
    while (position < text.length) {
      const marker = text.startsWith("<![CDATA[", position) ? "<![CDATA["
        : text.startsWith("<｜DSML｜CDATA[", position) ? "<｜DSML｜CDATA[" : undefined;
      if (marker) {
        const start = position + marker.length;
        const end = text.indexOf("]]>", start);
        if (end < 0) return fail(`Incomplete CDATA section: close with ]]> (not ]]]), then </${name}>`, "missing_cdata_end");
        value += text.slice(start, end);
        position = end + 3;
      } else if (text[position] === "<") {
        const start = position, found = tag();
        if (found !== `/${name}`) return fail(`Expected </${name}>, found <${found}>. Nested XML is not allowed inside '${name}'; use CDATA for source`, "mismatched_tag", start);
        return value;
      } else {
        const end = text.indexOf("<", position);
        if (end < 0) return fail(`Incomplete XML operand <${name}>: missing </${name}>`, "unclosed_tag");
        value += decodeEntities(text.slice(position, end));
        position = end;
      }
    }
    return fail(`Incomplete XML operand <${name}>: missing </${name}>`, "unclosed_tag");
  };
  whitespace();
  if (tag() !== "create") fail("Expected exactly one create artifact action", "invalid_instruction", 0);
  const fields = new Map<string, string>();
  while (true) {
    whitespace();
    const start = position;
    const name = tag();
    if (name === "/create") break;
    if (name !== "path" && name !== "content") fail(`Unknown artifact operand '${name}'; supply path and content exactly once`, "invalid_instruction", start);
    if (fields.has(name)) fail(`Duplicate artifact operand '${name}'`, "invalid_instruction", start);
    fields.set(name, scalar(name));
  }
  whitespace();
  if (position !== text.length) fail("Expected exactly one artifact; trailing actions or text are not allowed", "multiple_instructions");
  if (!fields.has("path") || !fields.has("content")) fail("Artifact requires path and content");
  const path = fields.get("path")!;
  if (expectedPath !== undefined && path !== expectedPath) throw new Error(`Artifact path must equal planned path '${expectedPath}'`);
  return { path, content: fields.get("content")! };
}

export function sourceArtifactInstruction(path: string): string {
  return INSTRUCTION_SET_PROMPT + `Return exactly one XML-like <create> instruction with <path> and <content> operands. The path must be exactly ${JSON.stringify(path)}. Put the complete file text inside CDATA. No prose, Markdown fences, extra operands or additional instructions. To include the literal CDATA terminator ]]> in source, split it as ]]]]><![CDATA[>. Example envelope:\n${encodeSourceArtifact({ path, content: "complete file text" })}`;
}
