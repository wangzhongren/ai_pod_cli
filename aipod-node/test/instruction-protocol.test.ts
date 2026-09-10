import assert from 'node:assert/strict';
import {test} from 'node:test';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {parseErrorFeedback} from '../src/agent/instruction-protocol.js';
import {parseAction,WorkspaceAgent,WorkspaceTools} from '../src/agent/workspace.js';
import {encodeSourceArtifact} from '../src/agent/source-codec.js';
import type {ConversationMessage} from '../src/agent/types.js';

function feedback(raw:string) {
  try {parseAction(raw);} catch(error) {return parseErrorFeedback(raw,error) as {executed:boolean;parse_error:{code:string;reason:string;line:number;column:number};format_help:string;format_example:string};}
  throw new Error('Malformed instruction was accepted');
}
test('observed DSML hybrid identifies the wrapper rather than file size or language',()=>{
  const result=feedback('Checking the file.\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="read"><path>app.py</path></read>');
  assert.equal(result.executed,false);
  assert.equal(result.parse_error.code,'foreign_envelope');
  assert.deepEqual([result.parse_error.line,result.parse_error.column],[2,1]);
  assert.match(result.parse_error.reason,/<｜｜DSML｜｜ calls>/);
  assert.equal(result.format_example,'<read><path>app.py</path></read>');
  assert.match(result.format_help,/shortening/);
});
test('syntax diagnostics include the actual reason and position',()=>{
  const cases:[string,string,number,string][]=[
    ['<read>\n<path>app.py</path>\n</shell>','mismatched_tag',3,'</shell>'],
    ['<shell><command><![CDATA[echo hello</command></shell>','missing_cdata_end',1,']]>'],
    ['<create><path>app.py</path><content><![CDATA[x = 1</content></create>','missing_cdata_end',1,']]>'],
    ['<read><path>app.py</path></read>\n<list/>','multiple_instructions',2,'one instruction'],
    ['<read><path>app.py</path>','unclosed_tag',1,'instruction'],
    ['<!DOCTYPE read><read/>','unsupported_markup',1,'declarations'],
  ];
  for(const [raw,code,line,detail] of cases){
    const result=feedback(raw);
    assert.equal(result.parse_error.code,code);
    assert.equal(result.parse_error.line,line);
    assert.ok(result.parse_error.column>=1);
    assert.ok(JSON.stringify(result).includes(detail));
  }
});
test('duplicate and unknown operands name the problem',()=>{
  assert.match(feedback('<read><path>a</path><path>b</path></read>').parse_error.reason,/<path>/);
  assert.match(feedback('<read><path>a</path><extra>1</extra></read>').parse_error.reason,/extra/);
  assert.match(feedback('<fly/>').parse_error.reason,/<fly>/);
});
test('DSML inside source remains data, including when a later closing tag is wrong',()=>{
  const content='const label = "<｜｜DSML｜｜ calls>";\n';
  const valid=encodeSourceArtifact({path:'src/models/value.ts',content});
  assert.equal(parseAction(valid).content,content);
  assert.notEqual(feedback(valid.replace(/<\/create>$/,'</wrong>')).parse_error.code,'foreign_envelope');
});
test('Agent receives diagnostics then corrects its response without partial execution',async()=>{
  const root=await mkdtemp(join(tmpdir(),'aipod-instruction-feedback-'));
  try{
    const tools=await WorkspaceTools.create(root,'models'), path=join(root,'src/models/value.ts');
    const calls:ConversationMessage[][]=[];
    const result=await new WorkspaceAgent({complete:async()=>{throw new Error('unexpected JSON call');},completeText:async(system,_user,conversation)=>{
      assert.match(system,/original, application-defined text instruction set/);
      calls.push(conversation!);
      if(calls.length===1)return '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="create"><path>src/models/value.ts</path><content><![CDATA[export const value = 42;\n]]></content></create>';
      if(calls.length===2){
        await assert.rejects(readFile(path),/ENOENT/);
        assert.match(conversation!.at(-1)!.content,/foreign_envelope/);
        assert.match(conversation!.at(-1)!.content,/nothing was executed/);
        assert.match(conversation!.at(-1)!.content,/format_example/);
        return encodeSourceArtifact({path:'src/models/value.ts',content:'export const value = 42;\n'});
      }
      return '<finish><summary>done</summary></finish>';
    }},tools,()=>false,3).run('Create a value',{},async()=>{throw new Error('unexpected owner request');},async action=>action);
    assert.equal(result.summary,'done');
    assert.equal(await readFile(path,'utf8'),'export const value = 42;\n');
  }finally{await rm(root,{recursive:true,force:true});}
});
test('execution errors are not reported as parsing errors',async()=>{
  const root=await mkdtemp(join(tmpdir(),'aipod-execution-feedback-'));
  try{
    const tools=await WorkspaceTools.create(root,'models'),calls:ConversationMessage[][]=[];
    await new WorkspaceAgent({complete:async()=>({}),completeText:async(_system,_user,conversation)=>{
      calls.push(conversation!);
      return calls.length===1?'<read><path>missing.ts</path></read>':'<finish><summary>observed</summary></finish>';
    }},tools,()=>false,2).run('Inspect',{},async()=>({}),async action=>action);
    assert.match(calls[1]!.at(-1)!.content,/ENOENT/);
    assert.doesNotMatch(calls[1]!.at(-1)!.content,/parse_error/);
  }finally{await rm(root,{recursive:true,force:true});}
});
