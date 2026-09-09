import strictAssert from "node:assert/strict";
import { readFile, rm, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { syncBuiltinESMExports } from "node:module";
import net from "node:net";
import tls from "node:tls";
import http from "node:http";
import https from "node:https";
import dns from "node:dns";
import dgram from "node:dgram";

import { TestSandbox, TestSetupError, type ComponentTestCase, type TestAssertions } from "./test-sandbox.js";
import { sourceFingerprint } from "./component-tests.js";

const deny = () => { throw new Error("External connections are disabled in component tests; supply explicit dependency Provider doubles"); };
net.Socket.prototype.connect = deny as typeof net.Socket.prototype.connect;
net.connect = deny as typeof net.connect; net.createConnection = deny as typeof net.createConnection;
tls.connect = deny as typeof tls.connect;
http.request = deny as typeof http.request; http.get = deny as typeof http.get;
https.request = deny as typeof https.request; https.get = deny as typeof https.get;
dns.lookup = deny as unknown as typeof dns.lookup; dns.resolve = deny as unknown as typeof dns.resolve;
dgram.createSocket = deny as typeof dgram.createSocket;
globalThis.fetch = deny as typeof globalThis.fetch;
syncBuiltinESMExports();

Object.freeze(strictAssert);
const request = JSON.parse(await readFile(process.argv[2]!, "utf8")) as { projectRoot: string; target: string; test: string; scenarios: string[]; token: string; receiptPath: string };
await rm(process.argv[2]!);
const results: { name: string; targetCalls: number; assertions: number }[] = [];
try {
const module = await import(pathToFileURL(request.test).href) as { default: readonly ComponentTestCase[] };
const cases = module.default;
strictAssert.ok(Array.isArray(cases) && cases.length > 0, "No executable tests were registered");
strictAssert.deepEqual([...cases.map((test) => test.name)].sort(), [...request.scenarios].sort(), "Missing or duplicated planned scenarios");
for (const test of cases) {
  strictAssert.ok(test && typeof test.run === "function", `Scenario '${test?.name}' is not executable`);
  strictAssert.ok(Object.keys(test).every((key) => ["name", "config", "providers", "run"].includes(key)), "Unsupported or skipped test declaration");
  let assertions = 0;
  const failedAssertions: string[] = [];
  const pendingAssertions: Promise<unknown>[] = [];
  const tracked = Object.fromEntries(["ok", "equal", "notEqual", "deepEqual", "notDeepEqual", "match", "throws", "rejects"].map((name) => [name, (...args: unknown[]) => {
    assertions += 1;
    try {
      const result = (strictAssert[name as keyof typeof strictAssert] as (...values: unknown[]) => unknown)(...args);
      if (result instanceof Promise) {
        const checked = result.catch((error: unknown) => { failedAssertions.push(String(error)); throw error; });
        checked.catch(() => undefined);
        pendingAssertions.push(checked);
        return checked;
      }
      return result;
    } catch (error) { failedAssertions.push(String(error)); throw error; }
  }])) as unknown as TestAssertions;
  Object.freeze(tracked);
  let sandbox: TestSandbox | undefined;
  try {
    sandbox = await TestSandbox.create(request.projectRoot, request.target, {
      ...(test.config ? { config: test.config } : {}), ...(test.providers ? { providers: test.providers } : {}),
    }, (root) => process.chdir(root));
    const before = JSON.stringify(await sourceFingerprint(sandbox.root));
    await test.run(sandbox, tracked);
    await Promise.allSettled(pendingAssertions);
    if (!sandbox.targetCalls) throw new TestSetupError(`${test.name}: no real tested-component call`);
    if (!assertions) throw new TestSetupError(`${test.name}: no executed assertion`);
    if (sandbox.setupErrors.length) throw new TestSetupError(`${test.name}: invalid fixtures: ${sandbox.setupErrors.join("; ")}`);
    strictAssert.equal(failedAssertions.length, 0, `${test.name}: test swallowed a failed assertion`);
    strictAssert.equal(JSON.stringify(await sourceFingerprint(sandbox.root)), before, `${test.name}: test or implementation modified candidate sources`);
    results.push({ name: test.name, targetCalls: sandbox.targetCalls, assertions });
    process.stdout.write(`PASS ${test.name}\n`);
  } finally { process.chdir(request.projectRoot); await sandbox?.close(); }
}

await writeFile(request.receiptPath, JSON.stringify({version:1,token:request.token,target:request.target,status:"passed",cases:results}));
} catch (error) {
  await writeFile(request.receiptPath, JSON.stringify({version:1,token:request.token,target:request.target,status:"failed",kind:error instanceof TestSetupError ? "TEST_SETUP" : "TEST_FAILURE",error:error instanceof Error ? error.message : String(error),cases:results}));
  process.exitCode=1;
}
