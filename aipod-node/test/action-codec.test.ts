import assert from 'node:assert/strict';
import {test} from 'node:test';
import {parseAction} from '../src/agent/workspace.js';
import {encodeSourceArtifact} from '../src/agent/source-codec.js';

test('XML-like file and shell actions preserve source and command text',()=>{
  assert.deepEqual(parseAction('<read><path>src/models/game.ts</path><offset>12</offset><limit>40</limit></read>'), {tool:'read',path:'src/models/game.ts',offset:12,limit:40});
  const command='node -e "if(1 < 2) console.log(3)" && printf "a&b"\n';
  assert.deepEqual(parseAction(`<shell><command><![CDATA[${command}]]></command><timeout>60</timeout></shell>`),{tool:'shell',command,timeout:60});
  assert.deepEqual(parseAction('<list/>'),{tool:'list'});
  assert.deepEqual(parseAction('I will inspect the project.\n<list/>'),{tool:'list'});
  assert.deepEqual(parseAction('<read><path>aipod.json</path></｜｜DSML｜｜>'),{tool:'read',path:'aipod.json'});
  const content='const text = "<read> & 中文";\r\n';
  assert.equal(parseAction(encodeSourceArtifact({path:'src/models/game.ts',content})).content,content);
  const update=encodeSourceArtifact({path:'src/models/game.ts',content}).replace(/^<create>/,'<update>').replace(/<\/create>$/,'</update>');
  assert.deepEqual(parseAction(update),{tool:'write',path:'src/models/game.ts',content});
});
test('XML finish metadata and Pod requests support nested objects and arrays',()=>{
  const finish=parseAction('<finish><summary>done</summary><components><item><id>Game</id><file>src/models/game.ts</file><dependencies/><inputs><size><type>number</type><required>false</required></size></inputs><outputs/></item></components><remove/></finish>');
  assert.deepEqual(finish,{tool:'finish',summary:'done',components:[{id:'Game',file:'src/models/game.ts',dependencies:[],inputs:{size:{type:'number',required:false}},outputs:{}}],remove:[]});
  assert.deepEqual(parseAction('<request_change><target>providers</target><paths><item>src/providers/impl/a.ts</item></paths><reason>failure</reason><change>fix</change></request_change>').paths,['src/providers/impl/a.ts']);
});
test('observed native XML-like calls preserve literal parameters',()=>{
  for(const marker of ['', '｜DSML｜', '｜｜DSML｜｜']){
    const raw=`<${marker}tool_calls><${marker}invoke name="shell"><${marker}parameter name="command" string="true">printf "x&y" && test 1 -lt 2</${marker}parameter><${marker}parameter name="timeout" string="false">30</${marker}parameter></${marker}invoke></${marker}tool_calls>`;
    assert.deepEqual(parseAction(raw),{tool:'shell',command:'printf "x&y" && test 1 -lt 2',timeout:30});
  }
});
test('multiple calls, prose, declarations and ambiguous operands are rejected',()=>{
  for(const raw of ['<read><path>a</path><path>b</path></read>','<list/><shell><command>touch bad</command></shell>',
    'I will read now.','<finish bad="x"/>','<finish>done</finish>',
    '<!DOCTYPE list [<!ENTITY x SYSTEM "file:///etc/passwd">]><list><path>&x;</path></list>',
    '<list><tool>shell</tool></list>','<write><path>x</path><content>bad</content></write>']) assert.throws(()=>parseAction(raw));
  const one='<invoke name="list"><parameter name="path" string="true">.</parameter></invoke>';
  assert.throws(()=>parseAction('<tool_calls>'+one+one+'</tool_calls>'));
});
