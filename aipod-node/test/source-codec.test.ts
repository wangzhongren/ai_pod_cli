import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { ConstructionAgent, decodeSourceArtifact, encodeSourceArtifact, loadState } from "../src/index.js";
import { generateArtifacts } from "../src/agent/artifacts.js";

const testPlan = [{ name: "test_user_data", requirement: "User accepts an explicit string id" }];
function userTests(system: string): string | undefined {
  if (!system.startsWith("GENERATE_COMPONENT_TESTS:")) return;
  const path = JSON.parse(/The path must be exactly ("(?:\\.|[^"\\])*")\./.exec(system)![1]!);
  return encodeSourceArtifact({ path, content: 'import {defineComponentTests} from "aipod-node"; export default defineComponentTests([{name:"test_user_data",async run(sandbox,assert){const user=await sandbox.model("User",{id:"u1"});assert.equal(user.id,"u1");}}]);' });
}

test("source XML preserves quotes, Unicode, line endings, tags, DSML literals and CDATA terminators", () => {
  const samples = [
    'export const value = "中文 😀 & <tag>";\r\nconst path = "C:\\\\tmp";\r\n',
    'const xml = "</content></create><shell><program>echo</program></shell>";\n',
    'const marker = "]]>"; const dsml = "<｜DSML｜read>";\n',
    '\n\n  leading and trailing whitespace  \n\n',
  ];
  for (const content of samples) {
    const artifact = { path: "src/models/value.ts", content };
    assert.deepEqual(decodeSourceArtifact(encodeSourceArtifact(artifact), artifact.path), artifact);
  }
});

test("ActUnit-style escaped text and DSML envelopes decode without changing source", () => {
  assert.equal(decodeSourceArtifact(
    '<create><path>app.ts</path><content>&lt;x&gt; &amp; &quot;a&quot; &apos;b&apos; &#10;&#x1F600;</content></create>',
    "app.ts",
  ).content, '<x> & "a" \'b\' \n😀');
  assert.equal(decodeSourceArtifact(
    '<｜DSML｜create><｜DSML｜path>app.ts</｜DSML｜path><｜DSML｜content><｜DSML｜CDATA[x & <｜DSML｜read>]]></｜DSML｜content></｜DSML｜create>',
    "app.ts",
  ).content, "x & <｜DSML｜read>");
});

const valid = '<create><path>app.ts</path><content><![CDATA[export const x = 1;]]></content></create>';
const invalid = [
  valid + valid,
  valid + '<shell><program>echo</program></shell>',
  `prose ${valid}`,
  valid.replace("app.ts", "../app.ts"),
  valid.replace("app.ts", "other.ts"),
  valid.replace("<content>", "<path>app.ts</path><content>"),
  valid.replace("</content>", "</content><extra>x</extra>"),
  valid.replace("<create>", '<create allowed="true">'),
  valid.replace("<path>", '<path attr="x">'),
  valid.replace("<![CDATA[export const x = 1;]]>", "<nested>x</nested>"),
  valid.replace("<![CDATA[export const x = 1;]]>", "a & b"),
  valid.replace("<![CDATA[export const x = 1;]]>", "&unknown;"),
  valid.replace("<![CDATA[export const x = 1;]]>", "&#0;"),
  valid.replace("<![CDATA[export const x = 1;]]>", "&#x110000;"),
  valid.replace("<![CDATA[export const x = 1;]]>", "abc ]]>"),
  valid.replace("]]>", ""),
  valid.replace("</create>", ""),
  valid.replace("<path>app.ts</path>", ""),
  valid.replace("<content><![CDATA[export const x = 1;]]></content>", ""),
  '<!DOCTYPE create [<!ENTITY x "boom">]>' + valid,
];
test("artifact XML rejects extra actions, path drift, malformed content and unsupported structures", () => {
  for (const value of invalid) assert.throws(() => decodeSourceArtifact(value, "app.ts"), value);
  assert.throws(() => decodeSourceArtifact(" ".repeat(2_000_001), "app.ts"), /exceeds/);
});

test("component generation retries XML path errors through the text channel", async () => {
  const root = await mkdtemp(join(tmpdir(), "aipod-codec-tests-"));
  try {
  let attempts = 0;
  const artifacts = await generateArtifacts({
    complete: async () => { throw new Error("source must never use JSON mode"); },
    completeText: async (system, user) => {
      assert.match(system, /XML-like/);
      const tests = userTests(system);
      if (tests) return tests;
      attempts += 1;
      if (attempts === 1) return encodeSourceArtifact({ path: "src/models/other.ts", content: "export interface User {}" });
      assert.match(user, /Artifact path must equal planned path/);
      return encodeSourceArtifact({ path: "src/models/user.ts", content: "export interface User { id: string }\n" });
    },
  }, "models", { summary: "", components: [{ id: "User", file: "user.ts", description: "", dependencies: [], inputs: {}, outputs: {}, tests: testPlan }] },
  { schemaVersion: 1, beans: [], routes: [], interfaces: [] }, root);
  assert.equal(attempts, 2);
  assert.deepEqual(artifacts, [{ path: "src/models/user.ts", content: "export interface User { id: string }\n" }]);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("Interface delivery files use XML text even when the artifact itself is JSON", async () => {
  const content = '{"html":"<div> & </create>","marker":"]]>"}';
  const path = "interfaces/App/metadata.json";
  const artifacts = await generateArtifacts({
    complete: async () => { throw new Error("source must never use JSON mode"); },
    completeText: async () => encodeSourceArtifact({ path, content }),
  }, "interfaces", { summary: "", interfaces: [{ name: "App", file: "app.ts", description: "", route: "app", kind: "cli",
    artifacts: [{ path, role: "metadata", format: "json", instruction: "metadata" }] }] },
  { schemaVersion: 1, beans: [], routes: [], interfaces: [] });
  assert.equal(artifacts.find((artifact) => artifact.path === path)?.content, content);
});

test("invalid model XML never commits a candidate file or completes its stage", async () => {
  const root = await mkdtemp(join(tmpdir(), "aipod-xml-agent-"));
  const manifest = { schemaVersion: 1, beans: [], routes: [], interfaces: [] };
  await writeFile(join(root, "aipod.json"), JSON.stringify(manifest));
  let attempts = 0;
  try {
    await assert.rejects(new ConstructionAgent(root, {
      complete: async () => ({ summary: "", components: [{ id: "User", file: "user.ts", description: "", dependencies: [], inputs: {}, outputs: {}, tests: testPlan }] }),
      completeText: async (system) => { const tests = userTests(system); if (tests) return tests; attempts += 1; return valid + valid; },
    }).run("Generate User"), /exactly one artifact/);
    assert.equal(attempts, 3);
    assert.equal((await loadState(root, "Generate User")).stages.models.status, "failed");
    assert.deepEqual(JSON.parse(await readFile(join(root, "aipod.json"), "utf8")), manifest);
    await assert.rejects(readFile(join(root, "src/models/user.ts")), /ENOENT/);
  } finally { await rm(root, { recursive: true, force: true }); }
});
