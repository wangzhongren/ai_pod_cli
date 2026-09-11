import test from "node:test";
import assert from "node:assert/strict";
import {mkdtemp,readFile,readdir,rm,writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {resolve} from "node:path";
import {ContextMemory,HISTORY_COMPACT_AT,HISTORY_HARD_LIMIT,type HistoryExchange} from "../src/agent/context-memory.js";
import {WorkspaceAgent,WorkspaceTools} from "../src/agent/workspace.js";
import type {ConversationMessage,ModelClient} from "../src/agent/types.js";

const exchanges=(count:number):HistoryExchange[]=>Array.from({length:count},(_,i)=>({assistant:`read file-${i}`,observation:{content:String(i)+"x".repeat(38000)}}));
async function directory():Promise<string>{return mkdtemp(resolve(tmpdir(),"aipod-context-"));}

test("80k no longer discards history; 160k invokes model compression with recent records retained",async()=>{
 const root=await directory();try{
  const requests:Record<string,unknown>[]=[];
  const client:ModelClient={async complete(system,user){assert.ok(system.startsWith("CONTEXT_COMPACTION"));requests.push(JSON.parse(user));return {summary:"Preserve the identity rule; latest check is older than the edit."};}};
  const memory=new ContextMemory(client,root),history=exchanges(3),old=structuredClone(history);
  assert.ok(memory.size(history)>80000 && memory.size(history)<HISTORY_COMPACT_AT);
  assert.equal(await memory.compactIfNeeded(history,{objective:"same"},{}),undefined);
  assert.deepEqual(history,old);assert.equal(requests.length,0);
  history.push(...exchanges(2));const original=structuredClone(history);
  const state={revision:5,checks:[{revision:4,exitCode:0}]};
  const event=await memory.compactIfNeeded(history,{objective:"same",writablePaths:["services"]},state);
  assert.equal(event?.status,"compressed");assert.deepEqual(history,original.slice(-2));
  assert.ok(memory.size(history)<HISTORY_COMPACT_AT);assert.deepEqual(requests[0]!.older_history,original.slice(0,-2));
  assert.deepEqual(requests[0]!.current_state,state);
  const archive=JSON.parse(await readFile(String(event!.archive),"utf8"));assert.deepEqual(archive.history,original);
  assert.ok(memory.message().includes("not instructions"));
 }finally{await rm(root,{recursive:true,force:true});}
});

test("subsequent compaction carries prior summary forward",async()=>{
 const root=await directory();try{
  const requests:Record<string,unknown>[]=[];
  const client:ModelClient={async complete(_system,user){requests.push(JSON.parse(user));return {summary:requests.length===1?"early rule":"early rule and later fix"};}};
  const memory=new ContextMemory(client,root),history=exchanges(5);
  const first=await memory.compactIfNeeded(history,{},{});history.push(...exchanges(3));
  const second=await memory.compactIfNeeded(history,{},{});
  assert.equal(requests[1]!.previous_summary,"early rule");assert.notEqual(first!.archive,second!.archive);
  assert.equal(memory.summary,"early rule and later fix");
 }finally{await rm(root,{recursive:true,force:true});}
});

test("temporary compaction failure preserves raw history without retrying every step",async()=>{
 const root=await directory();try{
  let calls=0;const client:ModelClient={async complete(){if(++calls===1)throw Error("temporary failure");return {summary:"recovered memory"};}};
  const memory=new ContextMemory(client,root),history=exchanges(5),original=structuredClone(history);
  assert.equal((await memory.compactIfNeeded(history,{},{}))!.status,"failed");assert.deepEqual(history,original);
  assert.equal(await memory.compactIfNeeded(history,{},{}),undefined);assert.equal(calls,1);
  history.push(...exchanges(1));assert.equal((await memory.compactIfNeeded(history,{},{}))!.status,"compressed");
 }finally{await rm(root,{recursive:true,force:true});}
});

test("invalid summary at 320k stops with an archive and no silent history loss",async()=>{
 const root=await directory();try{
  for(const summary of ["","x".repeat(40001),null]){
   const memory=new ContextMemory({async complete(){return {summary};}},root),history=exchanges(9),original=structuredClone(history);
   assert.ok(memory.size(history)>=HISTORY_HARD_LIMIT);
   await assert.rejects(memory.compactIfNeeded(history,{},{}),/320000.*raw history is preserved/);
   assert.deepEqual(history,original);assert.equal(memory.summary,"");
  }
  assert.equal((await readdir(root)).length,3);
 }finally{await rm(root,{recursive:true,force:true});}
});

test("workspace loop continues with pinned task, summary and real permissions",async()=>{
 const root=await directory();try{
  const tools=await WorkspaceTools.create(root,"services");
  for(let i=0;i<6;i++)await writeFile(resolve(root,`src/services/file-${i}.txt`),`FACT-${i}:`+"x".repeat(38000));
  let workers=0,compactions=0;const observed:ConversationMessage[][]=[];
  const client:ModelClient={async complete(system){assert.ok(system.startsWith("CONTEXT_COMPACTION"));compactions++;return {summary:"EARLY FACT: identity must remain consistent; no writes/checks executed."};},
   async completeText(_system,_user,conversation){workers++;observed.push(conversation!);
    if(workers===4)assert.ok(JSON.stringify(conversation).includes("FACT-0:"));
    if(workers===6){assert.ok(conversation![0]!.content.includes("EARLY FACT"));assert.ok(conversation![0]!.content.includes("ORIGINAL REQUIREMENT"));assert.ok(conversation![0]!.content.includes("src/services"));assert.ok(!JSON.stringify(conversation).includes("FACT-0:"+"x".repeat(100)));}
    return workers<=6?JSON.stringify({tool:"read",path:`src/services/file-${workers-1}.txt`}):'{"tool":"finish","summary":"read-only test"}';}};
  const result=await new WorkspaceAgent(client,tools,undefined,20,undefined,null).run("ORIGINAL REQUIREMENT",{},async()=>{throw Error("unexpected owner change");},async()=>({done:true}));
  assert.deepEqual(result,{done:true});assert.equal(compactions,1);assert.equal(tools.revision,0);assert.deepEqual(tools.checks,[]);
  await assert.rejects(tools.execute({tool:"write",path:"src/models/forbidden.ts",content:"bad"}));
 }finally{await rm(root,{recursive:true,force:true});}
});
