import { randomUUID } from "node:crypto";
import { readFile, realpath } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { resolve, relative } from "node:path";

import { AgentCancelledError, ConstructionAgent } from "./agent/agent.js";
import { OpenAICompatibleClient } from "./agent/client.js";
import type { AgentEvent } from "./agent/types.js";
import {
  loadInterface, runInterfaceLifecycle, smokeInterface, verifyInterface,
} from "./interface.js";
import { runRoute } from "./loader.js";
import { inspectProject } from "./project-model.js";

interface StudioTask {
  id: string;
  status: "running" | "cancelling" | "cancelled" | "complete" | "failed";
  events: AgentEvent[];
  error?: string;
  cancelRequested: boolean;
}

const html = String.raw`<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIPod Node Studio</title><style>
:root{color-scheme:dark;font:14px Inter,system-ui,sans-serif;background:#080c14;color:#dce6f5}*{box-sizing:border-box}
body{margin:0;display:grid;grid-template-columns:270px 1fr;min-height:100vh}.side{border-right:1px solid #263247;background:#0b111d;padding:18px;overflow:auto}
h1{font-size:18px;margin:0 0 4px}.muted{color:#7f8da4;font-size:12px}.main{padding:22px;overflow:auto}.card{background:#101827;border:1px solid #263247;border-radius:10px;padding:15px;margin-bottom:14px}
.stats{display:grid;grid-template-columns:repeat(5,minmax(90px,1fr));gap:10px}.stat b{font-size:22px;display:block}.stat span{color:#8f9db2;font-size:11px;text-transform:uppercase}
button,input,textarea,select{background:#0c1421;border:1px solid #34445f;color:#dce6f5;border-radius:6px;padding:8px}button{cursor:pointer;background:#1769aa}textarea{width:100%;min-height:90px}
.item{padding:9px;border-bottom:1px solid #202b3d;cursor:pointer}.item:hover{background:#152137}.badge{font-size:10px;padding:2px 6px;border-radius:10px;background:#28364d}.bad{color:#ff8796}.ok{color:#72d8a3}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#070b12;padding:12px;border-radius:7px;max-height:420px;overflow:auto}.row{display:flex;gap:8px;align-items:center}.grow{flex:1}
</style></head><body><aside class="side"><h1>AIPod Node Studio</h1><div class="muted" id="root"></div><h3>Components</h3><div id="beans"></div><h3>Routes</h3><div id="routes"></div><h3>Interfaces</h3><div id="interfaces"></div></aside>
<main class="main"><div class="stats" id="stats"></div><section class="card"><h2>Project validation</h2><div id="validation"></div></section>
<section class="card"><h2>Run Route</h2><div class="row"><select id="route"></select><input class="grow" id="params" value="{}"><button id="run">Run</button></div><pre id="output">Ready.</pre></section>
<section class="card"><h2>Build or modify with AI</h2><textarea id="objective" placeholder="Describe the application or change"></textarea><button id="pod">Start Pod Agent</button> <button id="cancelPod">Cancel</button><pre id="progress">Idle.</pre></section>
<section class="card"><h2>Source</h2><div id="sourceName" class="muted">Select a component</div><pre id="source"></pre></section></main>
<script>
const token=new URLSearchParams(location.search).get('token');const api=async(path,options={})=>{const response=await fetch(path,{...options,headers:{'content-type':'application/json','x-aipod-token':token,...options.headers}});const value=await response.json();if(!response.ok)throw new Error(value.error||response.statusText);return value};
let project;const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){project=await api('/api/project');root.textContent=project.projectRoot;const s=project.summary;stats.innerHTML=Object.entries(s).map(([k,v])=>'<div class="card stat"><b>'+v+'</b><span>'+k+'</span></div>').join('');validation.innerHTML=project.validation.valid?'<span class="ok">✓ Valid</span>':'<span class="bad">'+project.validation.issues.map(x=>esc(x.message)).join('<br>')+'</span>';beans.innerHTML=project.beans.map(x=>'<div class="item" data-source="'+esc(x.file)+'"><span class="badge">'+esc(x.category)+'</span> '+esc(x.id)+'</div>').join('');routes.innerHTML=project.routes.map(x=>'<div class="item">'+esc(x.name)+' <span class="badge">'+esc(x.execution.mode)+'</span></div>').join('');interfaces.innerHTML=project.interfaces.map(x=>'<div class="item"><span>'+esc(x.name)+'</span> <button data-smoke="'+esc(x.name)+'">Smoke</button> <button data-verify="'+esc(x.name)+'">Verify</button> <button data-install="'+esc(x.name)+'">Install</button> <button data-interface="'+esc(x.name)+'">Run</button></div>').join('');route.innerHTML=project.routes.map(x=>'<option>'+esc(x.name)+'</option>').join('');document.querySelectorAll('[data-source]').forEach(x=>x.onclick=async()=>{sourceName.textContent=x.dataset.source;source.textContent=(await api('/api/source?path='+encodeURIComponent(x.dataset.source))).source});document.querySelectorAll('[data-smoke]').forEach(x=>x.onclick=async()=>{output.textContent=JSON.stringify(await api('/api/interface/smoke',{method:'POST',body:JSON.stringify({name:x.dataset.smoke})}),null,2)});document.querySelectorAll('[data-verify]').forEach(x=>x.onclick=async()=>{output.textContent=JSON.stringify(await api('/api/interface/verify',{method:'POST',body:JSON.stringify({name:x.dataset.verify})}),null,2)});document.querySelectorAll('[data-install]').forEach(x=>x.onclick=async()=>{output.textContent=JSON.stringify(await api('/api/interface/install',{method:'POST',body:JSON.stringify({name:x.dataset.install})}),null,2)});document.querySelectorAll('[data-interface]').forEach(x=>x.onclick=async()=>{output.textContent=JSON.stringify(await api('/api/interface/run',{method:'POST',body:JSON.stringify({name:x.dataset.interface,payload:JSON.parse(params.value)})}),null,2)})}
run.onclick=async()=>{output.textContent='Running…';try{output.textContent=JSON.stringify(await api('/api/run',{method:'POST',body:JSON.stringify({route:route.value,params:JSON.parse(params.value)})}),null,2)}catch(e){output.textContent=e.message}};
let activeTask='';pod.onclick=async()=>{progress.textContent='Starting…';try{const task=await api('/api/pod',{method:'POST',body:JSON.stringify({objective:objective.value})});activeTask=task.id;const poll=async()=>{const value=await api('/api/pod/'+task.id);progress.textContent=value.events.map(x=>'['+x.stage+'] '+x.action+': '+x.message).join('\n')+(value.error?'\n'+value.error:'');if(value.status==='running'||value.status==='cancelling')setTimeout(poll,500);else{activeTask='';load()}};poll()}catch(e){progress.textContent=e.message}};cancelPod.onclick=async()=>{if(activeTask)await api('/api/pod/'+activeTask+'/cancel',{method:'POST',body:'{}'})};load();
</script></body></html>`;

const json = (response: ServerResponse, status: number, value: unknown) => {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(value));
};

async function body(request: IncomingMessage): Promise<Record<string, unknown>> {
  let content = "";
  for await (const chunk of request) {
    content += String(chunk);
    if (content.length > 1_000_000) throw new Error("Request body is too large");
  }
  return content ? JSON.parse(content) as Record<string, unknown> : {};
}

export async function startStudio(
  projectRoot: string,
  options: { port?: number; host?: string } = {},
): Promise<{ url: string; close(): Promise<void> }> {
  const root = resolve(projectRoot);
  const realRoot = await realpath(root);
  const token = randomUUID();
  const tasks = new Map<string, StudioTask>();
  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url ?? "/", "http://localhost");
      if (url.pathname === "/") {
        response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
        response.end(html);
        return;
      }
      if (request.headers["x-aipod-token"] !== token && url.searchParams.get("token") !== token) {
        json(response, 403, { error: "Forbidden" });
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/project") {
        json(response, 200, await inspectProject(root));
        return;
      }
      if (request.method === "GET" && url.pathname === "/api/source") {
        const path = await realpath(resolve(root, url.searchParams.get("path") ?? ""));
        if (relative(realRoot, path).startsWith("..")) throw new Error("Source path is outside project");
        json(response, 200, { source: await readFile(path, "utf8") });
        return;
      }
      if (request.method === "POST" && url.pathname === "/api/run") {
        const payload = await body(request);
        json(response, 200, await runRoute(
          root, String(payload.route ?? ""),
          (payload.params ?? {}) as Record<string, unknown>,
        ));
        return;
      }
      if (request.method === "POST" && url.pathname === "/api/interface/smoke") {
        const payload = await body(request);
        json(response, 200, await smokeInterface(root, String(payload.name ?? "")));
        return;
      }
      if (request.method === "POST" && url.pathname === "/api/interface/verify") {
        const payload = await body(request);
        json(response, 200, await verifyInterface(root, String(payload.name ?? "")));
        return;
      }
      if (request.method === "POST" && (
        url.pathname === "/api/interface/install" || url.pathname === "/api/interface/uninstall"
      )) {
        const payload = await body(request);
        const action = url.pathname.endsWith("install") && !url.pathname.endsWith("uninstall")
          ? "install" : "uninstall";
        json(response, 200, await runInterfaceLifecycle(root, String(payload.name ?? ""), action));
        return;
      }
      if (request.method === "POST" && url.pathname === "/api/interface/run") {
        const payload = await body(request);
        const { adapter } = await loadInterface(root, String(payload.name ?? ""));
        json(response, 200, await adapter.start((payload.payload ?? {}) as Record<string, unknown>));
        return;
      }
      if (request.method === "POST" && url.pathname === "/api/pod") {
        const payload = await body(request);
        const apiKey = process.env.OPENAI_API_KEY;
        const model = process.env.OPENAI_MODEL;
        if (!apiKey || !model) throw new Error("OPENAI_API_KEY and OPENAI_MODEL are required");
        const task: StudioTask = {
          id: randomUUID(), status: "running", events: [], cancelRequested: false,
        };
        tasks.set(task.id, task);
        const client = new OpenAICompatibleClient({
          apiKey, model,
          ...(process.env.OPENAI_BASE_URL ? { baseUrl: process.env.OPENAI_BASE_URL } : {}),
        });
        void new ConstructionAgent(
          root, client, (event) => task.events.push(event), () => task.cancelRequested,
        )
          .run(String(payload.objective ?? ""))
          .then(() => { task.status = "complete"; })
          .catch((error) => {
            task.status = error instanceof AgentCancelledError ? "cancelled" : "failed";
            task.error = error instanceof Error ? error.message : String(error);
          });
        json(response, 202, { id: task.id });
        return;
      }
      const taskMatch = url.pathname.match(/^\/api\/pod\/([A-Za-z0-9-]+)$/);
      if (request.method === "GET" && taskMatch) {
        const task = tasks.get(taskMatch[1] ?? "");
        if (!task) { json(response, 404, { error: "Task not found" }); return; }
        json(response, 200, task);
        return;
      }
      const cancelMatch = url.pathname.match(/^\/api\/pod\/([A-Za-z0-9-]+)\/cancel$/);
      if (request.method === "POST" && cancelMatch) {
        const task = tasks.get(cancelMatch[1] ?? "");
        if (!task) { json(response, 404, { error: "Task not found" }); return; }
        if (task.status === "running") {
          task.cancelRequested = true;
          task.status = "cancelling";
        }
        json(response, 200, task);
        return;
      }
      json(response, 404, { error: "Not found" });
    } catch (error) {
      json(response, 400, { error: error instanceof Error ? error.message : String(error) });
    }
  });
  await new Promise<void>((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(options.port ?? 0, options.host ?? "127.0.0.1", resolveListen);
  });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Cannot resolve Studio address");
  const url = `http://${address.address}:${address.port}/?token=${token}`;
  return {
    url,
    close: () => new Promise<void>((resolveClose, reject) =>
      server.close((error) => error ? reject(error) : resolveClose())
    ),
  };
}
