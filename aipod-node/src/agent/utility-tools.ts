import { listUtilities, readUtility, utilityPath, writeUtility, type UtilityCase } from "../utilities.js";
import { decodeSourceArtifact, sourceArtifactInstruction } from "./source-codec.js";
import type { ModelClient } from "./types.js";

const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);

/** The existing generator can operate its utility tools; this adds no Agent or application layer. */
export async function callWithUtilityTools(
  client: ModelClient, root: string, system: string, user: string,
): Promise<Record<string, unknown>> {
  const protocol = `
You may operate these shared utility tools before returning the original final metadata. A tool request is exactly {"tool":"list_utilities|read_utility|write_utility","arguments":{...}}; never mix it with final metadata.
list_utilities arguments: optional {"query":"search terms"}. read_utility arguments: {"id":"ClassName"}. Read a candidate's verified source, methods and existing callers before reuse when you need implementation details.
write_utility arguments: {"id":"ClassName","description":"one reusable pure computation","instruction":"what to implement","cases":[{"method":"methodName","args":[],"expected":0}]}. A raises case uses {"raises":"RangeError"} instead of expected. Supply real cases for every public method. Do not put source in JSON; the write tool separately asks you for one XML source artifact. write_utility only creates a new utility; existing IDs and files are frozen.
Utilities are globally discoverable ordinary static helper classes imported by normal relative imports. They are not Beans, Providers, Services, Agents or a new layer. Reuse them from suitable Models, Providers, Services, Pipelines or Interfaces. Prefer extracting reusable pure transformations/calculations to reduce component duplication and length. Do not move Service orchestration, Context access, IO, model calls or mutable runtime state into a utility. Existing layer visibility restrictions still apply. Maximum eight utility actions; then return the original final metadata format.`;
  let history = user;
  for (let actions = 0; actions <= 8; actions += 1) {
    const response = await client.complete(`${system}\n${protocol}`, history);
    if (!object(response)) {
      if (actions === 8) throw new Error("Utility tool action limit exceeded; metadata must be an object");
      history += '\nUtility result: {"status":"error","error":"Response must be a metadata object or an exact tool request"}';
      continue;
    }
    if (!Object.hasOwn(response, "tool")) return response;
    if (actions === 8) throw new Error("Utility tool action limit exceeded; no final metadata was returned");
    let result: Record<string, unknown>;
    try {
      if (Object.keys(response).sort().join(",") !== "arguments,tool" || typeof response.tool !== "string" || !object(response.arguments)) {
        throw new Error("Malformed utility tool request; expected exactly tool and arguments");
      }
      const args = response.arguments;
      if (response.tool === "list_utilities") {
        if (Object.keys(args).some((key) => key !== "query") || (args.query !== undefined && typeof args.query !== "string")) throw new Error("list_utilities accepts only an optional query string");
        result = { status: "success", utilities: await listUtilities(root, args.query as string | undefined) };
      } else if (response.tool === "read_utility") {
        if (Object.keys(args).join(",") !== "id" || typeof args.id !== "string") throw new Error("read_utility requires only id");
        result = { status: "success", utility: await readUtility(root, args.id) };
      } else if (response.tool === "write_utility") {
        if (Object.keys(args).sort().join(",") !== "cases,description,id,instruction" || typeof args.id !== "string"
          || !/^[A-Za-z_$][\w$]*$/.test(args.id) || typeof args.description !== "string" || !args.description.trim()
          || typeof args.instruction !== "string" || !args.instruction.trim() || !Array.isArray(args.cases)) {
          throw new Error("write_utility requires only id, description, instruction and explicit cases");
        }
        const directory = await listUtilities(root);
        if (directory.some((entry) => entry.id === args.id)) throw new Error(`Utility '${args.id}' already exists; model tools cannot overwrite it`);
        if (!client.completeText) throw new Error("ModelClient.completeText is required to generate utility source");
        const path = utilityPath(args.id);
        const xml = await client.completeText(
          `GENERATE_UTILITY:${args.id}\nGenerate exactly one ordinary TypeScript static helper class named ${args.id}. No constructor, fields, async methods, runtime Context, DI, Services/Providers, IO, model calls, dynamic evaluation, nondeterminism or top-level execution. Every method needs explicit parameter and return types. Private static helpers are allowed. Do not use any or unsafe casts. Only import registered utilities by relative paths or these pure standard functions: node:path basename/dirname/extname/format/isAbsolute/join/normalize/parse/sep/delimiter; node:util isDeepStrictEqual.\nRegistered directory:\n${JSON.stringify(directory)}\n${sourceArtifactInstruction(path)}`,
          `${args.description}\n${args.instruction}\nRequired behavior cases:\n${JSON.stringify(args.cases)}`,
        );
        const artifact = decodeSourceArtifact(xml, path);
        const entry = await writeUtility(root, { id: args.id, description: args.description, source: artifact.content, cases: args.cases as UtilityCase[] });
        result = { status: "success", utility: entry };
      } else throw new Error(`Unknown utility tool '${response.tool}'`);
    } catch (error) {
      result = { status: "error", error: error instanceof Error ? error.message : String(error) };
    }
    history += `\nUtility request:\n${JSON.stringify(response)}\nUtility result:\n${JSON.stringify(result)}\nUse the evidence above. Continue with an allowed tool or return the original final metadata.`;
  }
  throw new Error("Utility tool loop did not return metadata");
}
