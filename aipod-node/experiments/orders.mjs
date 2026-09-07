import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, writeFile, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import {
  ConstructionAgent, OpenAICompatibleClient, PipelineContext,
  loadGlobalEnvironment, loadDotEnv, loadRunner, loadProject,
  newState, saveState, typeCheckProject, encodeSourceArtifact,
} from '../dist/src/index.js';
import { revisionScope } from '../dist/src/agent/revision.js';

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const output = await mkdtemp(join(tmpdir(), 'aipod-orders-eval-'));
const jsonCompatibility = process.argv.includes('--json-compat');
const repetitions = Number(process.argv.find((arg) => arg.startsWith('--repetitions='))?.split('=')[1] ?? 3);
const modes = process.argv.includes('--auto-only') ? ['auto'] : ['auto', 'services'];
const requestTimeoutMs = Number(process.argv.find((arg) => arg.startsWith('--request-timeout-ms='))?.split('=')[1] ?? 90000);
if (!Number.isInteger(requestTimeoutMs) || requestTimeoutMs < 1 || requestTimeoutMs > 300000) throw new Error('Invalid experiment request timeout');
const sourceFormat = process.argv.includes('--source-format=json') ? 'json' : 'xml';
const revision = execFileSync('git', ['rev-parse', '--short', 'HEAD'], { cwd: packageRoot, encoding: 'utf8' }).trim();
const dirty = Boolean(execFileSync('git', ['status', '--porcelain', '--untracked-files=no'], { cwd: packageRoot, encoding: 'utf8' }).trim());
const report = { requestTimeoutMs, sourceFormat, jsonCompatibility, revision, dirty, output, startedAt: new Date().toISOString(), offline: [], live: [] };
const writeReport = () => writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2));
const log = (value) => console.log(JSON.stringify(value));
const hash = (value) => createHash('sha256').update(value).digest('hex');
const definitions = [
  { id: 'PriceOrder', file: 'price-order.ts', description: 'Return totalCents = amountCents. Input is nonnegative integer cents. This is the only pricing Service.', dependencies: [], inputs: { amountCents: { type: 'integer' } }, outputs: { totalCents: { type: 'integer' } } },
  { id: 'ReserveStock', file: 'reserve-stock.ts', description: 'Reserve quantity from StockMemory, return reserved boolean. Insufficient stock does not deduct.', dependencies: ['StockMemory'], inputs: { quantity: { type: 'integer' } }, outputs: { reserved: { type: 'boolean' } } },
  { id: 'NotifyOrder', file: 'notify-order.ts', description: 'Return message exactly "Order " + orderId + " confirmed".', dependencies: [], inputs: { orderId: { type: 'string' } }, outputs: { message: { type: 'string' } } },
];
const routes = definitions.map((bean, index) => ({
  name: ['price', 'reserve', 'notify'][index], description: bean.description,
  services: [bean.id], execution: { mode: 'sequential' }, file: `src/pipelines/${['price', 'reserve', 'notify'][index]}.ts`,
}));
const interfaces = routes.map((route) => ({ name: `${route.name}Cli`, file: `src/interfaces/${route.name}-cli.ts`, description: route.description, route: route.name, kind: 'cli', artifacts: [], lifecycle: {}, permissions: [], verify: [] }));
const objective = 'Update only the pricing rule in PriceOrder: for amountCents >= 10000 subtract exactly 1000 cents once; otherwise keep amountCents unchanged. Preserve every existing ID, file path, input/output contract, route and Interface. ReserveStock, StockMemory and NotifyOrder behavior must remain unchanged. The existing project contains all required components. No new components, dependencies, installers or verification commands. Keep artifacts, lifecycle and verify empty in Interfaces. All three existing routes must remain usable.';

async function put(root, file, text) { await mkdir(dirname(join(root, file)), { recursive: true }); await writeFile(join(root, file), text); }
async function fixture(name) {
  const root = join(output, name);
  await mkdir(root, { recursive: true });
  await put(root, 'package.json', '{"type":"module"}');
  await mkdir(join(root, 'node_modules'), { recursive: true });
  await symlink(packageRoot, join(root, 'node_modules', 'aipod-node'));
  const project = { schemaVersion: 1, beans: [
    { id: 'Order', category: 'model', file: 'src/models/order.ts', description: 'Order amount in integer cents', dependencies: [], inputs: {}, outputs: {} },
    { id: 'StockMemory', category: 'provider', file: 'src/providers/stock-memory.ts', description: 'In-memory stock starts at 10; reserve(q) returns boolean and deducts only when enough stock exists.', dependencies: [], inputs: {}, outputs: {} },
    ...definitions.map((bean) => ({ ...bean, file: `src/services/${bean.file}`, category: 'service' })),
  ], routes, interfaces };
  await put(root, 'aipod.json', JSON.stringify(project, null, 2));
  await put(root, 'src/models/order.ts', 'export interface Order { amountCents: number }\n');
  await put(root, 'src/providers/stock-memory.ts', 'export class StockMemory { private stock = 10; reserve(quantity: number): boolean { if (quantity < 0 || quantity > this.stock) return false; this.stock -= quantity; return true; } }\n');
  await put(root, 'src/services/price-order.ts', `import type { PipelineContext } from 'aipod-node';\nimport type { Order } from '../models/order.js';\nexport class PriceOrder { execute(context: PipelineContext) { const ctx = context.typed(${JSON.stringify(definitions[0].inputs)}, ${JSON.stringify(definitions[0].outputs)}); const order: Order = { amountCents: ctx.get('amountCents') }; return ctx.output({ totalCents: order.amountCents }); } }\n`);
  await put(root, 'src/services/reserve-stock.ts', `import type { PipelineContext } from 'aipod-node';\nimport type { StockMemory } from '../providers/stock-memory.js';\nexport class ReserveStock { constructor(private readonly deps: { StockMemory: StockMemory }) {} execute(context: PipelineContext) { const ctx = context.typed(${JSON.stringify(definitions[1].inputs)}, ${JSON.stringify(definitions[1].outputs)}); return ctx.output({ reserved: this.deps.StockMemory.reserve(ctx.get('quantity')) }); } }\n`);
  await put(root, 'src/services/notify-order.ts', `import type { PipelineContext } from 'aipod-node';\nexport class NotifyOrder { execute(context: PipelineContext) { const ctx = context.typed(${JSON.stringify(definitions[2].inputs)}, ${JSON.stringify(definitions[2].outputs)}); return ctx.output({ message: 'Order ' + ctx.get('orderId') + ' confirmed' }); } }\n`);
  for (const route of routes) await put(root, route.file, `export const routeName = '${route.name}';\n`);
  for (const item of interfaces) await put(root, item.file, `import type { PipelineRunner } from 'aipod-node';\nexport class ${item.name}Adapter { constructor(private readonly runner: PipelineRunner) {} requiredRoutes() { return ['${item.route}']; } start(payload = {}) { return this.runner.run('${item.route}', payload); } }\n`);
  const state = newState('Baseline orders fixture'); state.status = 'complete'; state.verification.status = 'passed';
  for (const stage of Object.keys(state.stages)) {
    state.stages[stage].status = 'complete';
    state.stages[stage].artifacts = stage === 'pipelines' ? routes.map((item) => item.file) : stage === 'interfaces' ? interfaces.map((item) => item.file) : project.beans.filter((bean) => `${bean.category}s` === stage).map((bean) => bean.file);
  }
  await saveState(root, state);
  return root;
}
async function acceptance(root, discounted) {
  const runner = await loadRunner(root);
  const cases = [];
  async function check(name, route, params, expected) {
    const { result } = await runner.run(route, params);
    cases.push({ name, passed: result.status === 'success' && JSON.stringify(result.output) === JSON.stringify(expected), result });
  }
  for (const amountCents of [0, 9999, 10000, 10001, 20000]) await check(`price-${amountCents}`, 'price', { amountCents }, { totalCents: discounted && amountCents >= 10000 ? amountCents - 1000 : amountCents });
  await check('stock-reserve-3', 'reserve', { quantity: 3 }, { reserved: true });
  await check('stock-insufficient-8', 'reserve', { quantity: 8 }, { reserved: false });
  await check('stock-remaining-7', 'reserve', { quantity: 7 }, { reserved: true });
  await check('stock-empty', 'reserve', { quantity: 1 }, { reserved: false });
  await check('notification', 'notify', { orderId: 'synthetic-001' }, { message: 'Order synthetic-001 confirmed' });
  return cases;
}
async function record(name, fn) {
  try { const evidence = await fn(); report.offline.push({ name, passed: true, evidence }); }
  catch (error) { report.offline.push({ name, passed: false, error: error.message }); }
  await writeReport(); log(report.offline.at(-1));
}
const baseline = await fixture('baseline');
await record('baseline-independent-business-acceptance', async () => { const cases = await acceptance(baseline, false); assert.ok(cases.every((c) => c.passed)); return cases; });
await record('compile-rejects-wrong-type-typo-and-missing-output', async () => {
  const errors = [];
  for (const [name, expression] of [['wrong-type', 'ctx.set("totalCents", "100")'], ['typo', 'ctx.get("ammountCents")'], ['missing-output', 'ctx.output({})']]) {
    const root = await fixture(`type-${name}`);
    await put(root, 'src/probe.ts', `import { PipelineContext } from 'aipod-node'; const ctx = new PipelineContext({amountCents: 100}).typed(${JSON.stringify(definitions[0].inputs)}, ${JSON.stringify(definitions[0].outputs)}); ${expression};`);
    const diagnostics = await typeCheckProject(root); assert.ok(diagnostics.length > 0, name); errors.push({ name, diagnostics });
  }
  return errors;
});
await record('runtime-rejects-untyped-invalid-input-and-output', () => {
  assert.throws(() => new PipelineContext({amountCents:'100'}).typed(definitions[0].inputs, definitions[0].outputs), /expected integer/);
  const ctx = new PipelineContext({amountCents:100}).typed(definitions[0].inputs, definitions[0].outputs);
  assert.throws(() => ctx.set('totalCents', '100'), /expected integer/);
  assert.throws(() => ctx.output({}), /required field/);
  return 'Invalid values rejected at runtime, including JavaScript callers.';
});
await record('model-change-reaches-price-only', async () => {
  const scope = await revisionScope(baseline, await loadProject(baseline), 'models', ['Order']);
  assert.deepEqual(scope, { models:['Order'], providers:[], services:['PriceOrder'], pipelines:['price'], interfaces:['priceCli'] }); return scope;
});
await record('dynamic-dependency-and-new-target-fall-back', async () => {
  assert.equal(await revisionScope(baseline, await loadProject(baseline), 'services', ['NewService']), undefined);
  const root = await fixture('dynamic'); await put(root, 'src/services/notify-order.ts', 'const name = "./unknown.js"; void import(name);');
  assert.equal(await revisionScope(root, await loadProject(root), 'services', ['PriceOrder']), undefined); return 'Both fall back.';
});

if (process.argv.includes('--live')) {
  const config = { ...await loadGlobalEnvironment(), ...await loadDotEnv(join(packageRoot, '..', '.env')), ...process.env };
  if (!config.OPENAI_API_KEY || !config.OPENAI_MODEL) throw new Error('Model configuration missing');
  report.model = config.OPENAI_MODEL;
  const model = new OpenAICompatibleClient({ apiKey: config.OPENAI_API_KEY, model: config.OPENAI_MODEL, baseUrl: config.OPENAI_BASE_URL, timeoutMs: requestTimeoutMs });
  for (let repeat = 1; repeat <= repetitions; repeat++) for (const mode of modes) {
    const root = await fixture(`${mode}-${repeat}`);
    const project = await loadProject(root);
    const files = [...project.beans, ...project.routes, ...project.interfaces].map((item) => item.file);
    const before = Object.fromEntries(await Promise.all(files.map(async (file) => [file, hash(await readFile(join(root, file)))])));
    const run = { mode, repeat, root, calls: [], status: 'running' };
    report.live.push(run); await writeReport();
    const started = performance.now();
    let requestNo = 0;
    const invoke = async (format, system, user) => {
      if (++requestNo > 20) throw new Error('Experiment call budget exceeded');
      const callNumber = requestNo;
      const sentSystem = jsonCompatibility && format === "json" ? `${system}\nReturn a strict JSON object.` : system;
      const call = { format, step: system.split('\n')[0], startedAt: new Date().toISOString() };
      run.calls.push(call); log({ mode, repeat, step: call.step });
      const start = performance.now();
      try {
        const result = format === 'json' ? await model.complete(sentSystem, user) : await model.completeText(sentSystem, user);
        call.responseCharacters = typeof result === 'string' ? result.length : JSON.stringify(result).length;
        call.ms = Math.round(performance.now() - start);
        await put(root, `evidence/call-${callNumber}.json`, JSON.stringify({system: sentSystem, user, result}, null, 2));
        if (call.step.startsWith('CLASSIFY')) run.classification = result;
        // This fixture needs no external commands or platform lifecycle actions.
        if (call.step === 'PLAN_STAGE:interfaces' && (result.interfaces ?? []).some((i) => (i.verify?.length ?? 0) || Object.values(i.lifecycle ?? {}).some((cmd) => cmd?.length))) throw new Error('Experiment forbids external verification/lifecycle commands');
        return result;
      } catch (error) { call.error = error.message; throw error; }
      finally { await writeReport(); }
    };
    const client = {
      complete: (system, user) => invoke('json', system, user),
      completeText: async (system, user) => {
        if (sourceFormat === 'xml') return invoke('text', system, user);
        // Comparator only: emulate the previous JSON content envelope through the
        // same generator, validators, model, fixture and independent acceptance.
        const match = /The path must be exactly ("(?:\\.|[^"\\])*")\./.exec(system);
        if (!match) throw new Error('Missing planned path in generation prompt');
        const path = JSON.parse(match[1]);
        const jsonPrompt = system.slice(0, system.indexOf('Return exactly one XML-like'))
          + 'Return strict JSON {"content":"complete source"}.';
        const result = await invoke('json', jsonPrompt, user);
        if (typeof result.content !== 'string') throw new Error('Missing JSON source content');
        return encodeSourceArtifact({ path, content: result.content });
      },
    };
    try { const state = await new ConstructionAgent(root, client).revise(objective, mode); run.status = state.status; run.scope = state.revisionScope ?? null; }
    catch (error) { run.status = 'failed'; run.error = error.message; }
    run.ms = Math.round(performance.now() - started);
    try { run.acceptance = await acceptance(root, true); } catch (error) { run.acceptanceError = error.message; }
    run.changed = [];
    for (const file of files) if (hash(await readFile(join(root, file))) !== before[file]) run.changed.push(file);
    const protectedFiles = files.filter((file) => !['src/services/price-order.ts','src/pipelines/price.ts','src/interfaces/price-cli.ts'].includes(file));
    run.unrelatedChanged = run.changed.filter((file) => protectedFiles.includes(file));
    run.typedServices = [];
    for (const bean of (await loadProject(root)).beans.filter((bean) => bean.category === 'service')) {
      const source = await readFile(join(root, bean.file), 'utf8');
      run.typedServices.push({ id: bean.id, usesTyped: /\.typed\s*\(/.test(source), anyEscape: /\bany\b/.test(source) });
    }
    await writeReport(); log({ mode, repeat, status:run.status, calls:run.calls.length, ms:run.ms, passed:run.acceptance?.filter((c)=>c.passed).length, unrelatedChanged:run.unrelatedChanged, error:run.error });
    if (run.calls.some((call) => /fetch failed|Model request failed \((401|403|404|429)|aborted/i.test(call.error ?? ''))) {
      report.liveStopped = 'Model transport/authentication/rate limit failure; remaining trials not attempted.';
      await writeReport(); log({ report: join(output, 'report.json'), stopped: report.liveStopped }); process.exit(2);
    }
  }
}
report.finishedAt = new Date().toISOString(); await writeReport(); log({ report: join(output, 'report.json') });
