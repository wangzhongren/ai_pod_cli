/** Strict ActUnit-compatible create/path/content subset, not a general XML parser.
 * Exactly one candidate artifact is decoded; no action is executed here.
 */
export interface SourceArtifact {
  path: string;
  content: string;
}

function validCharacters(value: string): void {
  for (const character of value) {
    const code = character.codePointAt(0)!;
    if (!(code === 9 || code === 10 || code === 13 ||
      (code >= 0x20 && code <= 0xd7ff) || (code >= 0xe000 && code <= 0xfffd) || code >= 0x10000)) {
      throw new Error("Invalid XML character in source artifact");
    }
  }
}

function decodeEntities(value: string): string {
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

export function decodeSourceArtifact(text: string, expectedPath: string): SourceArtifact {
  if (text.length > 2_000_000) throw new Error("Source artifact response exceeds 2,000,000 characters");
  validCharacters(text);
  let position = 0;
  const whitespace = () => { while (/\s/.test(text[position] ?? "") && position < text.length) position += 1; };
  const tag = (): string => {
    const match = /^<(\/?)(?:｜DSML｜)?([A-Za-z_][A-Za-z0-9_.-]*)\s*>/.exec(text.slice(position));
    if (!match) throw new Error("Expected a plain XML tag; attributes, declarations and nested content are not allowed");
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
        if (end < 0) throw new Error("Incomplete CDATA section");
        value += text.slice(start, end);
        position = end + 3;
      } else if (text[position] === "<") {
        if (tag() !== `/${name}`) throw new Error(`Nested XML is not allowed inside '${name}'; use CDATA for source`);
        return value;
      } else {
        const end = text.indexOf("<", position);
        if (end < 0) throw new Error(`Incomplete XML operand '${name}'`);
        value += decodeEntities(text.slice(position, end));
        position = end;
      }
    }
    throw new Error(`Incomplete XML operand '${name}'`);
  };
  whitespace();
  if (tag() !== "create") throw new Error("Expected exactly one create artifact action");
  const fields = new Map<string, string>();
  while (true) {
    whitespace();
    const name = tag();
    if (name === "/create") break;
    if (name !== "path" && name !== "content") throw new Error(`Unknown artifact operand '${name}'`);
    if (fields.has(name)) throw new Error(`Duplicate artifact operand '${name}'`);
    fields.set(name, scalar(name));
  }
  whitespace();
  if (position !== text.length) throw new Error("Expected exactly one artifact; trailing actions or text are not allowed");
  if (!fields.has("path") || !fields.has("content")) throw new Error("Artifact requires path and content");
  const path = fields.get("path")!;
  if (path !== expectedPath) throw new Error(`Artifact path must equal planned path '${expectedPath}'`);
  return { path, content: fields.get("content")! };
}

export function sourceArtifactInstruction(path: string): string {
  return `Return exactly one XML-like <create> action with <path> and <content> operands. The path must be exactly ${JSON.stringify(path)}. Put the complete file text inside CDATA. No prose, Markdown fences, extra operands or additional actions. To include the literal CDATA terminator ]]> in source, split it as ]]]]><![CDATA[>. Example envelope:\n${encodeSourceArtifact({ path, content: "complete file text" })}`;
}
