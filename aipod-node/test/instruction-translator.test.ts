import assert from 'node:assert/strict';
import {test} from 'node:test';
import {mkdtemp,readFile,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {WorkspaceAgent,WorkspaceTools} from '../src/agent/workspace.js';
import {InstructionTranslator,parseNeed,encodeNeed} from '../src/agent/instruction-translator.js';
import {decodeTranslatedAction} from '../src/agent/action-codec.js';
import {ConstructionAgent} from '../src/agent/agent.js';
import type {ModelClient} from '../src/agent/types.js';

test('request wrapper preserves plain text and CDATA source without accepting extra requests',()=>{
  assert.equal(parseNeed('<need_function_tool>Read a & b.</need_function_tool>'),'Read a & b.');
  assert.equal(parseNeed('<need_function_tool><![CDATA[const x="</need_function_tool>";\n]]></need_function_tool>'),'const x="</need_function_tool>";\n');
  assert.equal(parseNeed('<need_function_tool><![CDATA[a]]]]><![CDATA[>b]]></need_function_tool>'),'a]]>b');
  for(const raw of ['<need_function_tool/>','<need_function_tool> </need_function_tool>','prose <need_function_tool>Read</need_function_tool>','<need_function_tool>Read</need_function_tool><need_function_tool>Write</need_function_tool>','<need_function_tool><![CDATA[a]]></need_function_tool><need_function_tool><![CDATA[b]]></need_function_tool>'])assert.throws(()=>parseNeed(raw));
});

test('translated operands reject unknown functions and incorrect shapes',()=>{
  for(const value of [{tool:'fetch',url:'https://example.com'},{tool:'read',path:'x',delete:true},{tool:'write',path:'x',content:12},{tool:'shell',command:'test',timeout:NaN},{tool:'finish',summary:'done',components:{}},{tool:'request_change',target:'models',paths:'x',reason:'r',change:'c'}])assert.throws(()=>decodeTranslatedAction(value));
});

test('translator cannot generate source not literally supplied by the worker',async()=>{
  const converter=new InstructionTranslator({complete:async()=>({tool:'write',path:'src/models/value.ts',content:'invented source'})});
  await assert.rejects(converter.translate('Write the missing model',{}),/complete exact source/);
  const content='const x = "a & b";\r\n';
  const exact=new InstructionTranslator({complete:async()=>({tool:'write',path:'src/models/value.ts',content})});
  assert.equal((await exact.translate('Write this source:\n'+content,{})).content,content);
});

test('worker uses request text, converter supplies operands, and normal ownership still rejects upstream writes',async()=>{
  const root=await mkdtemp(resolve(tmpdir(),'aipod-converter-'));
  try{
    const tools=await WorkspaceTools.create(root,'services'),observations:Record<string,unknown>[]=[];
    const own='src/services/impl/value.ts',upstream='src/models/value.ts',content='export const value=1;\n';
    const requests=[`Write ${upstream}:\n${content}`,`Write ${own}:\n${content}`,'Read the file just written','Finish with summary done'];
    const replies=[{tool:'write',path:upstream,content},{tool:'write',path:own,content},{tool:'read',path:own},{tool:'finish',summary:'done'}];
    let worker=0,converter=0;
    const client={complete:async(system:string,user:string)=>{
      assert.match(system,/TRANSLATE_LOCAL_OPERATION/);const input=JSON.parse(user);assert.equal(input.context.owner,'services');
      assert.equal(input.request,requests[converter]);return replies[converter++]!;
    },completeText:async(system:string,_user:string,conversation?:{role:string;content:string}[])=>{
      assert.match(system,/<need_function_tool>/);assert.doesNotMatch(system,/<read><path>/);
      if(worker===1)assert.match(conversation!.at(-1)!.content,/cannot modify/);
      return '<need_function_tool>'+requests[worker++]+'</need_function_tool>';
    }};
    const result=await new WorkspaceAgent(client,tools,()=>false,4,(_a,r)=>{observations.push(r);}).run('Write then read',{},async()=>({}),async action=>action);
    assert.equal(worker,4);assert.equal(converter,4);assert.equal(result.summary,'done');
    await assert.rejects(readFile(resolve(root,upstream)),/ENOENT/);assert.equal(await readFile(resolve(root,own),'utf8'),content);
    assert.equal(observations[2]!.content,content);
  }finally{await rm(root,{recursive:true,force:true});}
});

test('large translated writes keep request syntax when history is shortened',async()=>{
  const root=await mkdtemp(resolve(tmpdir(),'aipod-large-request-'));
  try{
    const content='//'+ 'x'.repeat(41000)+'\n';let calls=0;
    const client:ModelClient={complete:async(_s,u)=>JSON.parse(u).request.startsWith('Write')?{tool:'write',path:'src/models/value.ts',content}:{tool:'finish',summary:'done'},completeText:async(_s,_u,conversation)=>{
      for(const m of conversation??[])if(m.role==='assistant')assert.match(parseNeed(m.content),/source omitted/);
      return encodeNeed(++calls===1?'Write src/models/value.ts:\n'+content:'Finish');
    }};
    await new WorkspaceAgent(client,await WorkspaceTools.create(root,'models'),()=>false,2).run('Write',{},async()=>({}),async a=>a);
    assert.equal(await readFile(resolve(root,'src/models/value.ts'),'utf8'),content);
  }finally{await rm(root,{recursive:true,force:true});}
});

test('default ConstructionAgent translates every layer and final Pod review',async t=>{
  const root=await mkdtemp(resolve(tmpdir(),'aipod-default-translated-'));
  try{
    await writeFile(resolve(root,'package.json'),'{"type":"module"}');
    await writeFile(resolve(root,'aipod.json'),'{"schemaVersion":1,"beans":[],"routes":[],"interfaces":[]}');
    const probe=await (await WorkspaceTools.create(root,'models')).shell('node --version');
    if(probe.output.includes('sandbox_apply: Operation not permitted')){t.skip('Nested sandbox unavailable');return;}
    assert.equal(probe.exitCode,0,probe.output);
    const content='export class Value { count = 1; }\n';
    const program='const s=await import(process.env.AIPOD_NODE_MODULE);const e=await s.compileProjectSources(process.cwd());if(e.length)throw Error(JSON.stringify(e));const{pathToFileURL}=await import("node:url");const{Value}=await import(pathToFileURL(s.compiledSourcePath(process.cwd(),"src/models/value.ts")).href);if(new Value().count!==1)throw Error("wrong value");';
    const command='node --input-type=module -e '+"'"+program+"'";
    const corrected=content.replace('= 1','= 2'),correctedCommand=command.replace('count!==1','count!==2');
    const metadata={id:'Value',file:'src/models/value.ts',description:'value',dependencies:[],inputs:{},outputs:{}};
    const actions:Record<string,Record<string,unknown>[]>={models:[{tool:'write',path:'src/models/value.ts',content},{tool:'shell',command},{tool:'finish',summary:'model checked',components:[metadata]},
      {tool:'write',path:'src/models/value.ts',content:corrected},{tool:'shell',command:correctedCommand},{tool:'finish',summary:'model corrected',components:[metadata]}],
      services:[{tool:'request_change',target:'models',paths:['src/models/value.ts'],reason:'Objective requires count 2',change:'Set count to 2'},{tool:'shell',command:correctedCommand},{tool:'finish',summary:'correction checked'}],
      pod:[{tool:'shell',command:correctedCommand},{tool:'finish',summary:'delivery checked'}]};
    const pending:Record<string,Record<string,unknown>>={},seen:string[]=[];
    const client:ModelClient={complete:async(s,u)=>{if(s.startsWith('POD_APPROVE_CHANGE'))return {approved:true,summary:'Required for count 2'};const owner=JSON.parse(u).context.owner;seen.push(owner);const action=pending[owner]!;delete pending[owner];return action;},completeText:async(system)=>{
      const owner=system.split('\n')[0]!.split(':')[1]!;assert.match(system,/<need_function_tool>/);
      const queue=actions[owner]??=[];pending[owner]=queue.shift()??{tool:'finish',summary:'empty layer'};
      return encodeNeed(pending[owner]!.tool==='write'?'Write exact source:\n'+pending[owner]!.content:'Perform '+pending[owner]!.tool);
    }};
    const result=await new ConstructionAgent(root,client).run('Create a Value model with count 2');
    assert.equal(result.status,'complete');assert.equal(result.verification.status,'passed');
    assert.deepEqual(new Set(seen),new Set(['models','providers','services','pipelines','interfaces','pod']));
    assert.equal(await readFile(resolve(root,'src/models/value.ts'),'utf8'),corrected);
    assert.equal((result as typeof result & {changeRequests:{status:string}[]}).changeRequests[0]!.status,'applied');
  }finally{await rm(root,{recursive:true,force:true});}
});

test('cancellation after translation prevents local execution',async()=>{
  const root=await mkdtemp(resolve(tmpdir(),'aipod-converter-cancel-'));
  try{
    const tools=await WorkspaceTools.create(root,'models');let cancelled=false;
    const content='export const x=1;';
    const client={completeText:async()=>`<need_function_tool>Write src/models/x.ts: ${content}</need_function_tool>`,complete:async()=>{cancelled=true;return {tool:'write',path:'src/models/x.ts',content};}};
    await assert.rejects(new WorkspaceAgent(client,tools,()=>cancelled,2,()=>undefined,new InstructionTranslator(client)).run('Write',{},async()=>({}),async a=>a),/cancelled/);
    await assert.rejects(readFile(resolve(root,'src/models/x.ts')),/ENOENT/);
  }finally{await rm(root,{recursive:true,force:true});}
});
