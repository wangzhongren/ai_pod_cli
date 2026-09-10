import assert from "node:assert/strict";
import test from "node:test";
import { OpenAICompatibleClient } from "../src/agent/client.js";
import type { ConversationMessage } from "../src/agent/types.js";

test("workspace conversation preserves executed actions and observations as successive turns", async (context) => {
  const conversation: ConversationMessage[] = [{role:"user",content:"Build"}, {role:"assistant",content:"<read><path>missing</path></read>"}, {role:"user",content:"File does not exist. Create it."}];
  context.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body));
    assert.deepEqual(body.messages, [{role:"system",content:"XML actions"}, ...conversation]);
    return Response.json({choices:[{message:{content:"<list/>"},finish_reason:"stop"}]});
  });
  const client = new OpenAICompatibleClient({apiKey:"test",model:"test"});
  assert.equal(await client.completeText("XML actions", "fallback", conversation), "<list/>");
});

test("empty model responses retry the same request before entering tool history", async (context) => {
  let attempts = 0;
  context.mock.method(globalThis, "fetch", async () => Response.json({choices:[{message:{content:++attempts === 1 ? null : "<list/>"},finish_reason:"stop"}]}));
  const client = new OpenAICompatibleClient({apiKey:"test",model:"test"});
  assert.equal(await client.completeText("XML actions", "List files"), "<list/>");
  assert.equal(attempts, 2);
});

test("text generation sends no JSON mode and preserves the complete XML response", async (context) => {
  const system = "Generate one XML-like create action with CDATA source.";
  const content = '\n<create><path>app.ts</path><content><![CDATA[const x = "a & b";\n]]></content></create>\n';
  context.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body));
    assert.equal(Object.hasOwn(body, "response_format"), false);
    assert.equal(body.max_tokens, 65536);
    assert.equal(body.messages[0].content, system);
    return Response.json({ choices: [{ message: { content }, finish_reason: "stop" }] });
  });
  const client = new OpenAICompatibleClient({ apiKey: "test-key", model: "test-model" });
  assert.equal(await client.completeText(system, "Write app.ts"), content);
});

test("truncated responses cannot be accepted as complete source or plans", async (context) => {
  context.mock.method(globalThis, "fetch", async () => Response.json({
    choices: [{ message: { content: '{"stage":"services"}' }, finish_reason: "length" }],
  }));
  const client = new OpenAICompatibleClient({ apiKey: "test-key", model: "test-model" });
  await assert.rejects(client.completeText("Generate source", ""), /truncated/);
  await assert.rejects(client.complete("Choose stage", ""), /truncated/);
});

test("JSON-mode client makes schema-only classification prompts acceptable to strict endpoints", async (context) => {
  const system = 'CLASSIFY_REVISION_STAGE\nChoose the earliest affected stage. Return {"stage":"services","targets":["PriceOrder"]}.';
  const user = "Add a discount to PriceOrder.";
  context.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    assert.equal(url, "https://model.example/v1/chat/completions");
    const body = JSON.parse(String(init.body));
    assert.deepEqual(body.response_format, { type: "json_object" });
    assert.equal(body.max_tokens, 32768);
    // Reproduce the configured endpoint's pre-generation JSON-mode validation.
    if (!body.messages.some((message: { content: string }) => /json/i.test(message.content))) {
      return new Response('Prompt must contain the word json', { status: 400 });
    }
    assert.ok(body.messages[0].content.startsWith(system));
    assert.match(body.messages[0].content, /Return a strict JSON object\./);
    assert.deepEqual(body.messages[1], { role: "user", content: user });
    return Response.json({ choices: [{ message: { content: '{"stage":"services","targets":["PriceOrder"]}' } }] });
  });
  const client = new OpenAICompatibleClient({ apiKey: "test-key", model: "test-model", baseUrl: "https://model.example/v1/" });
  assert.deepEqual(await client.complete(system, user), { stage: "services", targets: ["PriceOrder"] });
});

test("model defaults use a ten-minute deadline and preserve explicit limits", async (context) => {
  const delays: Array<number | undefined> = [];
  const timer = globalThis.setTimeout;
  context.mock.method(globalThis, "setTimeout", (callback: () => void, delay?: number) => {
    delays.push(delay);
    return timer(callback, delay);
  });
  const budgets: number[] = [];
  context.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body));
    budgets.push(body.max_tokens);
    return Response.json({ choices: [{ message: { content: body.response_format ? '{}' : 'source' } }] });
  });
  await new OpenAICompatibleClient({ apiKey: "test", model: "test" }).complete("plan", "");
  const custom = new OpenAICompatibleClient({ apiKey: "test", model: "test",
    jsonMaxTokens: 512, sourceMaxTokens: 1024, timeoutMs: 5000 });
  await custom.complete("plan", "");
  await custom.completeText("source", "");
  assert.deepEqual(budgets, [32768, 512, 1024]);
  assert.deepEqual(delays, [600000, 5000, 5000]);
});

test("JSON-mode instruction preserves existing generation format requirements and source content", async (context) => {
  const source = 'export const message = "hello";\n';
  const system = 'Generate TypeScript. Return strict JSON {"content":"complete source"}.';
  context.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body));
    assert.ok(body.messages[0].content.startsWith(system));
    return Response.json({ choices: [{ message: { content: JSON.stringify({ content: source }) } }] });
  });
  const client = new OpenAICompatibleClient({ apiKey: "test-key", model: "test-model" });
  assert.deepEqual(await client.complete(system, "Generate the file."), { content: source });
});
