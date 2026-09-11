import assert from 'node:assert/strict';
import {test} from 'node:test';
import {mkdtemp,mkdir,writeFile,readFile,rm,symlink,link} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {WorkspaceTools,parseAction,type Owner} from '../src/agent/workspace.js';
import {ConstructionAgent} from '../src/agent/agent.js';
import {encodeSourceArtifact} from '../src/agent/source-codec.js';
import {loadState} from '../src/agent/state.js';
import type {ModelClient} from '../src/agent/types.js';
const q=(s:string)=>`'${s.replaceAll("'","'\\''")}'`;
const js=(s:string)=>({tool:'shell',command:`${q(process.execPath)} --input-type=module -e ${q(s)}`});
const done={tool:'finish',summary:'No work needed in this layer'};
async function project(){const root=await mkdtemp(resolve(tmpdir(),'aipod-workspace-'));await writeFile(resolve(root,'package.json'),'{"type":"module"}');await writeFile(resolve(root,'aipod.json'),JSON.stringify({schemaVersion:1,beans:[],routes:[],interfaces:[]}));await writeFile(resolve(root,'config.json'),'{}');return root;}
async function protectedTools(root:string,owner:Owner,t:{skip(s:string):void}){
  const tools=await WorkspaceTools.create(root,owner);
  try{const check=await tools.shell('printf backend-ready');if(check.output.includes('sandbox_apply: Operation not permitted')){t.skip('Outer sandbox prevents nesting; rerun with local shell permission');return;}assert.equal(check.exitCode,0,check.output);tools.revision=0;tools.checks.length=0;return tools;}
  catch(error){if(String(error).includes('requires macOS')){t.skip(String(error));return;}throw error;}
}
class ScriptedClient implements ModelClient{
  calls:string[]=[];
  constructor(readonly actions:Partial<Record<Owner,unknown[]>>,readonly approve=true){}
  async complete(system:string){assert.match(system,/POD_APPROVE_CHANGE/);this.calls.push('approval');return {approved:this.approve,summary:'Original objective considered'};}
  async completeText(system:string){const owner=system.split('\n')[0]!.split(':')[1] as Owner;this.calls.push(owner);const action=this.actions[owner]?.shift()??(owner==='pod'?undefined:done);assert.notEqual(action,undefined,`Unexpected ${owner} call`);return typeof action==='string'?action:JSON.stringify(action);}
}
const model={id:'Number',file:'src/models/number.ts',description:'numeric value',dependencies:[],inputs:{},outputs:{}};
const source=(type:string)=>encodeSourceArtifact({path:model.file,content:`export interface Number { value: ${type} }\n`});
const checkModel=js("import{readFileSync}from'node:fs';import assert from'node:assert/strict';assert.match(readFileSync('src/models/number.ts','utf8'),/value: number/)");
const finishModel={tool:'finish',summary:'Model checked',components:[model]};

test('all owners share CRUD and cannot write upstream or Pod state',async()=>{
 const root=await project();try{for(const owner of ['models','providers','services','pipelines','interfaces','pod'] as Owner[]){const tools=await WorkspaceTools.create(root,owner);const path=owner==='pod'?'README.md':`src/${owner}/item.txt`;await tools.execute(parseAction(encodeSourceArtifact({path,content:'first\r\n'})));await tools.execute(parseAction(encodeSourceArtifact({path,content:'second\r\n'})));assert.equal(await readFile(resolve(root,path),'utf8'),'second\r\n');const page=await tools.execute({tool:'read',path,limit:3});const tail=await tools.execute({tool:'read',path,offset:page.next_offset});assert.equal(String(page.content)+String(tail.content),'second\r\n');assert.equal(tail.next_offset,null);assert.ok((await tools.execute({tool:'search',text:'second'})).matches);await tools.execute({tool:'delete',path});}
 const tools=await WorkspaceTools.create(root,'services');for(const path of ['src/models/x.ts','aipod.json','.aipod/plan.json','.git/config','../escape.ts','.env'])await assert.rejects(tools.execute({tool:'write',path,content:'bad'}));assert.throws(()=>parseAction('{"tool":"write","content":"code"}'),/XML/);
 }finally{await rm(root,{recursive:true,force:true});}
});

test('shell blocks direct, renamed, symbolic-link and hard-link writes to upstream',async t=>{
 const root=await project();try{const tools=await protectedTools(root,'services',t);if(!tools)return;await mkdir(resolve(root,'src/models'));await writeFile(resolve(root,'src/models/locked.txt'),'original');await symlink(resolve(root,'src/models'),resolve(root,'src/services/escape'));
 for(const cmd of ['printf bad > src/models/locked.txt','rm src/models/locked.txt','mv src/models/locked.txt src/services/taken','printf bad > src/services/escape/locked.txt','ln src/models/locked.txt src/services/alias']){assert.notEqual((await tools.shell(cmd)).exitCode,0,cmd);assert.equal(await readFile(resolve(root,'src/models/locked.txt'),'utf8'),'original');}
 assert.equal((await tools.shell('printf own > src/services/own.txt')).exitCode,0);await link(resolve(root,'src/models/locked.txt'),resolve(root,'src/services/hard.txt'));await assert.rejects(tools.shell('printf bad > src/services/hard.txt'),/hard links/);
 }finally{await rm(root,{recursive:true,force:true});}
});

test('shell timeouts, private runtime data and hidden credentials',async t=>{
 const root=await project();try{const tools=await protectedTools(root,'services',t);if(!tools)return;await writeFile(resolve(root,'.env'),'OPENAI_API_KEY=private');assert.notEqual((await tools.shell('cat .env')).exitCode,0);
 const check=await tools.execute(js("import assert from'node:assert/strict';const {ModelRepository}=await import(process.env.AIPOD_NODE_MODULE);assert.equal(process.env.OPENAI_API_KEY,undefined);const r=new ModelRepository(process.cwd());await r.save('fixtures',{id:'one'});assert.equal((await r.list('fixtures')).length,1)"));assert.equal(check.exitCode,0,String(check.output));await assert.rejects(readFile(resolve(root,'.aipod/data.json')));assert.equal((await tools.shell('sleep 10','.',1)).timedOut,true);
 }finally{await rm(root,{recursive:true,force:true});}
});

test('common tool loop builds and final Pod checks without a generated-test registry',async t=>{
 const root=await project();try{if(!await protectedTools(root,'models',t))return;const client=new ScriptedClient({models:[source('number'),checkModel,finishModel],pod:[js("const {typeCheckProject}=await import(process.env.AIPOD_NODE_MODULE);const e=await typeCheckProject(process.cwd());if(e.length)throw Error(JSON.stringify(e))"),done]});const state=await new ConstructionAgent(root,client, undefined, undefined, {instructionMode: "direct"}).run('Define numeric Number');assert.equal(state.status,'complete');assert.equal(state.verification.status,'passed');assert.equal(client.calls.filter(s=>s==='models').length,3);assert.ok(!client.calls.includes('approval'));await assert.rejects(readFile(resolve(root,'.aipod/component-tests.json')));
 }finally{await rm(root,{recursive:true,force:true});}
});

test('Pod approval dispatches upstream owner and revalidates intermediate layers',async t=>{
 const root=await project();try{if(!await protectedTools(root,'models',t))return;await writeFile(resolve(root,model.file),'export interface Number {value:string}');const client=new ScriptedClient({services:[{tool:'request_change',target:'models',paths:[model.file],reason:'Objective requires a numeric value',change:'Use number for value'},checkModel,done],models:[source('number'),checkModel,finishModel]});await new ConstructionAgent(root,client, undefined, undefined, {instructionMode: "direct"}).runStage('services','Use numeric Number');const state=await loadState(root,'Use numeric Number') as unknown as {changeRequests:{status:string}[],stages:{pipelines:{status:string}}};assert.equal(state.changeRequests[0]!.status,'applied');assert.equal(state.stages.pipelines.status,'pending');assert.ok(client.calls.indexOf('approval')<client.calls.indexOf('models'));assert.ok(client.calls.includes('providers'));await assert.rejects((await WorkspaceTools.create(root,'services')).execute({tool:'write',path:model.file,content:'bad'}));
 }finally{await rm(root,{recursive:true,force:true});}
});

test('Pod denial leaves upstream untouched',async t=>{
 const root=await project();try{if(!await protectedTools(root,'services',t))return;const client=new ScriptedClient({services:[{tool:'request_change',target:'models',paths:[model.file],reason:'Convenience',change:'Invent a new model'},done]},false);await new ConstructionAgent(root,client, undefined, undefined, {instructionMode: "direct"}).runStage('services','Reuse current contracts');assert.deepEqual(client.calls,['services','approval','services']);await assert.rejects(readFile(resolve(root,model.file)));
 }finally{await rm(root,{recursive:true,force:true});}
});

test('cancellation after model response prevents pending file write',async()=>{
 const root=await project();try{let cancelled=false;const client:ModelClient={async complete(){throw Error('unused');},async completeText(){cancelled=true;return source('number');}};await assert.rejects(new ConstructionAgent(root,client,()=>undefined,()=>cancelled, {instructionMode: "direct"}).run('Cancel safely'),/cancelled/);await assert.rejects(readFile(resolve(root,model.file)));
 }finally{await rm(root,{recursive:true,force:true});}
});

test('final Pod writes an ordinary acceptance test and sends a failed requirement back to the owner',async t=>{
 const root=await project();try{if(!await protectedTools(root,'models',t))return;
 const testFile='tests/pod/number.ts';const testSource='import type {Number} from "../../src/models/number.js";const value:Number={value:2};\n';
 const check=js("const {typeCheckProject}=await import(process.env.AIPOD_NODE_MODULE);const errors=await typeCheckProject(process.cwd(),['tests/pod/number.ts']);if(errors.length)throw Error(JSON.stringify(errors));");
 const client=new ScriptedClient({models:[source('string'),js("import{readFileSync}from'node:fs';if(!readFileSync('src/models/number.ts','utf8').includes('value: string'))throw Error('invalid fixture');"),finishModel,source('number'),checkModel,finishModel],
 pod:[encodeSourceArtifact({path:testFile,content:testSource}),check,{tool:'request_change',target:'models',paths:[model.file],reason:'Numeric assignment fails the original requirement',change:'Use number for Number.value'},check,{tool:'finish',summary:'Acceptance now passes'}]});
 const state=await new ConstructionAgent(root,client, undefined, undefined, {instructionMode: "direct"}).run('Number.value is numeric') as unknown as {status:string,changeRequests:{requester:string,status:string}[]};
 assert.equal(state.status,'complete');assert.equal(state.changeRequests[0]!.requester,'pod');assert.equal(state.changeRequests[0]!.status,'applied');assert.equal(await readFile(resolve(root,testFile),'utf8'),testSource);
 }finally{await rm(root,{recursive:true,force:true});}
});
