import { decodeSourceArtifact, decodeEntities, validCharacters } from "./source-codec.js";

const fields: Record<string, string[]> = {
  list: ["path"], read: ["path", "offset", "limit"], search: ["path", "text"], delete: ["path"],
  shell: ["command", "cwd", "timeout"], request_change: ["target", "paths", "reason", "change"],
  finish: ["summary", "components", "pipelines", "routes", "interfaces", "remove"], write: ["path", "content"],
};
const lists = new Set(["paths", "components", "pipelines", "routes", "interfaces", "remove", "dependencies", "services", "artifacts", "tests"]);
const objects = new Set(["inputs", "outputs", "methods", "execution", "adapter", "lifecycle"]);
const textFields = new Set(["path", "file", "id", "name", "class_path", "command", "cwd", "text", "content", "target", "reason", "change", "summary", "description"]);
type Action = Record<string, unknown> & {tool: string};

function scalar(name: string, value: string): unknown {
  if (textFields.has(name)) return ["command", "content", "text"].includes(name) ? value : value.trim();
  value = value.trim();
  if (!value) return lists.has(name) ? [] : objects.has(name) ? {} : "";
  try { return JSON.parse(value); } catch { return value; }
}
function checked(tool: string, arguments_: unknown): Action {
  if (!Object.hasOwn(fields, tool) || !arguments_ || typeof arguments_ !== "object" || Array.isArray(arguments_)) throw new Error("Unknown XML action or invalid operands");
  if (Object.keys(arguments_).some((key) => !fields[tool]!.includes(key))) throw new Error(`Unknown operands for ${tool}`);
  return { tool, ...arguments_ };
}
function nativeCall(raw: string): Action {
  const marker = "(?:｜+DSML｜+)?";
  const outer = new RegExp(`^<${marker}tool_calls>\\s*<${marker}invoke\\s+name="([a-z_]+)">([\\s\\S]*)</${marker}invoke>\\s*</${marker}tool_calls>$`).exec(raw);
  if (!outer) throw new Error("Return exactly one XML-like action without prose or additional calls");
  const arguments_: Record<string, unknown> = Object.create(null), body = outer[2]!;
  let position = 0;
  const parameter = new RegExp(`^\\s*<${marker}parameter\\s+name="([a-z_]+)"\\s+string="(true|false)">([\\s\\S]*?)</${marker}parameter>`);
  while (body.slice(position).trim()) {
    const match = parameter.exec(body.slice(position));
    if (!match) throw new Error("Invalid or multiple native XML-like tool calls");
    const [, name, string, value] = match;
    if (Object.hasOwn(arguments_, name!)) throw new Error("Duplicate action operand");
    arguments_[name!] = string === "true" ? value : JSON.parse(value!);
    position += match[0].length;
  }
  return checked(["create", "update", "write"].includes(outer[1]!) ? "write" : outer[1]!, arguments_);
}

/** All generated instructions use XML-like actions. Saved JSON clients still work. */
export function decodeAction(raw: string): Action {
  if (typeof raw !== "string" || raw.length > 2_000_000) throw new Error("Action must be text under 2,000,000 characters");
  raw = raw.trim();
  validCharacters(raw);
  // Ignore a prefatory sentence, but parse the entire action and reject extra calls.
  if (!raw.startsWith("<") && !raw.startsWith("{") && raw.includes("<") && !raw.slice(0, raw.indexOf("<")).includes("{")) raw = raw.slice(raw.indexOf("<"));
  const rootTag = /^<([a-z_]+)\s*>/.exec(raw), anonymousEnd = /<\/｜+DSML｜+>$/.exec(raw);
  if (rootTag && anonymousEnd) raw = raw.slice(0, anonymousEnd.index) + `</${rootTag[1]}>`;
  if (raw.startsWith("{")) {
    const value = JSON.parse(raw) as Action;
    if (!value || typeof value !== "object" || typeof value.tool !== "string") throw new Error("Return one tool action");
    if (value.tool === "write") throw new Error("Source must use XML/CDATA, not JSON");
    return value;
  }
  if (/^<(?:｜+DSML｜+)?tool_calls>/.test(raw)) return nativeCall(raw);
  const update = /^<(?:｜DSML｜)?update\s*>/.exec(raw), updateEnd = /<\/(?:｜DSML｜)?update\s*>$/.exec(raw);
  if (update && updateEnd) return {tool: "write", ...decodeSourceArtifact("<create>" + raw.slice(update[0].length, updateEnd.index) + "</create>")};
  if (/^<(?:｜DSML｜)?create\s*>/.test(raw)) return {tool: "write", ...decodeSourceArtifact(raw)};
  interface Element {name: string; text: string; children: Element[]}
  let position = 0;
  const element = (): Element => {
    const tag = /^<([A-Za-z_][A-Za-z0-9_.-]*)\s*(\/?)>/.exec(raw.slice(position));
    if (!tag) throw new Error("Use plain XML action/operand tags without prose, attributes or declarations");
    position += tag[0].length;
    const node: Element = {name: tag[1]!, text: "", children: []};
    if (tag[2]) return node;
    while (position < raw.length) {
      if (raw.startsWith("<![CDATA[", position)) {
        const end = raw.indexOf("]]>", position + 9);
        if (end < 0) throw new Error("Incomplete CDATA operand: close with ]]> (not ]]]), then the closing operand tag");
        node.text += raw.slice(position + 9, end); position = end + 3;
      } else if (raw.startsWith("</", position)) {
        const close = /^<\/([A-Za-z_][A-Za-z0-9_.-]*)\s*>/.exec(raw.slice(position));
        if (!close || close[1] !== node.name) throw new Error("Mismatched XML operand");
        position += close[0].length; return node;
      } else if (raw[position] === "<") node.children.push(element());
      else {
        const end = raw.indexOf("<", position);
        if (end < 0) throw new Error("Incomplete XML action");
        node.text += decodeEntities(raw.slice(position, end)); position = end;
      }
    }
    throw new Error("Incomplete XML action");
  };
  const value = (node: Element): unknown => {
    if (!node.children.length) return scalar(node.name, node.text);
    if (node.text.trim()) throw new Error("Do not mix text and nested XML operands");
    if (node.children.every((child) => child.name === "item")) return node.children.map(value);
    const entries: [string, unknown][] = [], names = new Set<string>();
    for (const child of node.children) {
      if (names.has(child.name)) throw new Error("Duplicate XML operand; use <item> for arrays");
      names.add(child.name); entries.push([child.name, value(child)]);
    }
    return Object.fromEntries(entries);
  };
  const root = element();
  if (raw.slice(position).trim()) throw new Error("Return exactly one XML-like action without trailing actions");
  if (root.name === "write") throw new Error("Source writes use <create> with content in CDATA");
  if (!root.children.length && root.text.trim()) throw new Error("Action arguments must use named XML operands");
  return checked(root.name, root.children.length ? value(root) : {});
}
