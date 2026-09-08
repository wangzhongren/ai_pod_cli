"""Cross-file real-project experiment using AIPod generation and bounded repair."""
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from unittest.mock import patch

from ai_pod_cli.client import call_llm, get_client
from ai_pod_cli.source_generation import generate_source
from ai_pod_cli.repair import apply_file_patches, file_patch_prompt

repo=Path(sys.argv[1]).resolve()
python=Path(sys.argv[2]).absolute()
output=Path(tempfile.mkdtemp(prefix='aipod-tinydb-large-'))
work=output/'project';work.mkdir()
files=[x for x in subprocess.check_output(['git','ls-files','-z'],cwd=repo).decode().split('\0') if x]
for file in files:
    dest=work/file;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(repo/file,dest)
acceptance=Path(__file__).with_name('acceptance_cases.py').resolve()
hash_file=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
baseline={file:hash_file(work/file) for file in files}
acceptance_hash=hash_file(acceptance)
env={**os.environ,'PYTHONPATH':str(work)}
source_tokens=int(os.environ.get('TINYDB_EVAL_SOURCE_TOKENS','32768'))
source_timeout=float(os.environ.get('TINYDB_EVAL_SOURCE_TIMEOUT','300'))
retries=int(os.environ.get('TINYDB_EVAL_RETRIES','3'))
stream_enabled=os.environ.get('TINYDB_EVAL_STREAM','0')=='1'
metadata_tokens=int(os.environ.get('TINYDB_EVAL_METADATA_TOKENS','8192'))
resume_path=os.environ.get('TINYDB_EVAL_RESUME')
resume_report=json.loads((Path(resume_path)/'report.json').read_text()) if resume_path else None
report={'project':'msiemens/tinydb','revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo).decode().strip(),
        'output':str(output),'allowed_files':['tinydb/table.py','tinydb/database.py'],
        'acceptance_hash':acceptance_hash,'calls':[],'stages':[],
        'source_max_tokens':source_tokens,'source_timeout_seconds':source_timeout,'max_retries':retries,'stream':stream_enabled,'metadata_max_tokens':metadata_tokens,'resume_from':resume_path}
report['size']={name:sum(len((work/file).read_text().splitlines()) for file in files if file.startswith(name+'/') and file.endswith('.py')) for name in ['tinydb','tests']}
original_sources={file:(work/file).read_text() for file in report['allowed_files']}
if resume_report:
    assert resume_report['revision']==report['revision']
    assert resume_report['acceptance_hash']==acceptance_hash
def save(): (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
def command(label,args):
    start=time.monotonic()
    proc=subprocess.run([str(python),*args],cwd=work,env=env,text=True,capture_output=True,timeout=180)
    text=proc.stdout+proc.stderr;(output/f'{label}.log').write_text(text)
    item={'exit_code':proc.returncode,'summary':text.strip().splitlines()[-1] if text.strip() else '',
          'seconds':round(time.monotonic()-start,3),'log':str(output/f'{label}.log')}
    print(label,json.dumps(item),flush=True)
    return item
report['tested_import']=subprocess.check_output([str(python),'-c','import tinydb;print(tinydb.__file__)'],cwd=work,env=env,text=True).strip()
assert Path(report['tested_import']).resolve().is_relative_to(work.resolve())
report['baseline_tests']=command('baseline-original',['-m','pytest','-o','addopts=','-q','tests'])
report['baseline_types']=command('baseline-types',['-m','mypy','tinydb','tests'])
report['baseline_acceptance']=command('baseline-acceptance',['-m','pytest','-o','addopts=','-q','--tb=short',str(acceptance)])
save()
assert report['baseline_tests']['exit_code']==0 and report['baseline_types']['exit_code']==0
assert report['baseline_acceptance']['exit_code']!=0

config=Path.home()/'.aipod/config.toml'
for key,value in tomllib.loads(config.read_text()).get('env',{}).items(): os.environ.setdefault(key,str(value))
report['model']=os.environ.get('OPENAI_MODEL')

def llm(system,user,**options):
    index=len(report['calls'])+1
    call={'channel':'json' if options['json_mode'] else 'xml','requests':[]}
    report['calls'].append(call);save()
    start=time.monotonic();print('MODEL',index,call['channel'],flush=True)
    completions=get_client().chat.completions;original=completions.create
    def observed(**kwargs):
        streaming=bool(kwargs.get('stream'))
        if streaming: kwargs['stream_options']={'include_usage':True}
        request={'max_tokens':kwargs.get('max_tokens'),'timeout':kwargs.get('timeout'),'stream':streaming}
        call['requests'].append(request);save();began=time.monotonic()
        def finish():
            request['seconds']=round(time.monotonic()-began,3);save();print('REQUEST',json.dumps(request),flush=True)
        def usage_of(response):
            usage=getattr(response,'usage',None)
            if usage:
                request['usage']={key:getattr(usage,key,None) for key in ('prompt_tokens','completion_tokens','total_tokens')}
                details=getattr(usage,'completion_tokens_details',None)
                request['reasoning_tokens']=getattr(details,'reasoning_tokens',None) if details else None
        try:
            response=original(**kwargs)
        except Exception as error:
            request['error_type']=type(error).__name__;finish();raise
        if streaming:
            def chunks():
                last_progress=began
                request.update(response_characters=0,reasoning_characters=0)
                try:
                    for chunk in response:
                        usage_of(chunk)
                        choices=getattr(chunk,'choices',None) or []
                        if choices:
                            choice=choices[0]
                            if choice.finish_reason is not None:request['finish_reason']=choice.finish_reason
                            delta=choice.delta
                            request['response_characters']+=len(getattr(delta,'content',None) or '')
                            request['reasoning_characters']+=len(getattr(delta,'reasoning_content',None) or '')
                        now=time.monotonic()
                        if now-last_progress>=30:
                            request['seconds']=round(now-began,3);save()
                            print('STREAM',json.dumps(request),flush=True);last_progress=now
                        yield chunk
                except Exception as error:
                    request['error_type']=type(error).__name__;raise
                finally:
                    response.close();finish()
            return chunks()
        try:
            choice=response.choices[0]
            request.update(finish_reason=choice.finish_reason,response_characters=len(choice.message.content or ''))
            usage_of(response)
            return response
        finally:finish()
    if stream_enabled:
        options['progress_callback']=lambda event: print('MODEL_OUTPUT',json.dumps(event),flush=True) if event['type']=='llm_completed' else None
    try:
        with patch.object(completions,'create',side_effect=observed): response=call_llm(system,user,**options)
        (output/f'model-{index}.txt').write_text(json.dumps(response,ensure_ascii=False,indent=2) if isinstance(response,dict) else response)
        return response
    except Exception as error:
        call['error']=str(error);raise
    finally:
        call['seconds']=round(time.monotonic()-start,3);save()

spec='''Add opt-in copy_on_read deep read isolation to this existing TinyDB project. This is an additive feature; default False must preserve all existing behavior. No dependencies, no public API removals, no changing storage format or write semantics.
Table.__init__ gains copy_on_read: bool=False without changing the order/meaning of existing positional parameters. With True, all read-returned Documents from search (both cache miss and hit), get(cond), get(doc_id), get(doc_ids), all, and iteration must be deep independent from storage and cached objects, including nested lists/dicts, doc_id and custom document_class metadata. Modifying returned containers or Documents must not change later reads. Query caching must still work (do not disable or recompute cache hits). Writes must still invalidate query caches and persist normally. Read operations must not write to storage. Preserve Document subclasses and metadata. Handle copies at read-output boundaries; do not change _read_table, _update_table or unrelated write methods. Add private helpers if useful. Update the relevant docstrings to describe opt-in behavior.
TinyDB(copy_on_read=True) consumes this keyword before constructing storage and uses it as the default for newly opened tables (including the default table reached through __getattr__ and __iter__). Explicit table(name,copy_on_read=False/True) overrides the database default. Existing cached Table instances retain their original setting and identity. When database option is omitted or False, do not inject copy_on_read=False into custom legacy Table constructors which do not accept the new keyword. Explicit False must also not leak to storage. Existing storage args/kwargs still work.
Do not change tests, docs files, queries.py, storages.py, utils.py, dependencies, exports or unrelated methods. Code conventions: preserve existing style and multiline docstring layout. Metadata must be {"summary":"brief description","extra_deps":[]} with no source code.'''
allowed_methods={'tinydb/table.py':{'Table':{'__init__','all','search','get','__iter__'}},'tinydb/database.py':{'TinyDB':{'__init__','table'}}}
def method_map(source):
    tree=ast.parse(source)
    return {(cls.name,node.name):ast.dump(node,include_attributes=False)
            for cls in tree.body if isinstance(cls,ast.ClassDef)
            for node in cls.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}

def check_scope(path,source,frozen):
    old=method_map(original_sources[path]);new=method_map(source)
    errors=[]
    for (cls,method),value in old.items():
        if (cls,method) not in new:errors.append(f'Removed method {cls}.{method}')
        elif method not in allowed_methods[path].get(cls,set()) and value!=new[(cls,method)]:
            errors.append(f'Frozen method changed: {cls}.{method}')
    for file in files:
        if file not in report['allowed_files'] and hash_file(work/file)!=baseline[file]:errors.append(f'Unrelated file changed: {file}')
    for file,digest in frozen.items():
        if hash_file(work/file)!=digest:errors.append(f'Frozen upstream file changed: {file}')
    if hash_file(acceptance)!=acceptance_hash:errors.append('Acceptance criteria changed')
    return errors

frozen={}
for path in report['allowed_files']:
    stage={'file':path,'attempts':[]};report['stages'].append(stage);save()
    context='Current target source:\n'+(work/path).read_text()
    if path.endswith('table.py'):
        context+='\nFrozen storage interfaces:\n'+(work/'tinydb/storages.py').read_text()
        context+='\nDatabase integration comes in the next stage. Implement only Table read isolation now.'
    else:
        context+='\nFrozen upstream Table implementation (do not modify):\n'+(work/'tinydb/table.py').read_text()
    try:
        if resume_report and path in resume_report.get('frozen_files',{}):
            reused=Path(resume_path)/'project'/path
            assert hash_file(reused)==resume_report['frozen_files'][path], 'Saved upstream hash changed'
            code=reused.read_text()
            stage['reused_from']=str(reused)
            print('REUSE',path,flush=True)
        else:
            generated=generate_source(llm,spec,context,path,max_tokens=metadata_tokens,timeout_seconds=source_timeout,max_retries=retries,source_max_tokens=source_tokens,source_timeout_seconds=source_timeout)
            assert generated.get('extra_deps')==[], 'Unexpected dependencies'
            code=generated['code']
        ast.parse(code,feature_version=(3,10))
        (work/path).write_text(code)
        for check_index in range(3):
            label=path.split('/')[-1].replace('.py','')+f'-{check_index}'
            check={'scope_errors':check_scope(path,code,frozen)}
            check['original_tests']=command(label+'-original',['-m','pytest','-o','addopts=','-q','--tb=short','tests'])
            check['types']=command(label+'-types',['-m','mypy','tinydb','tests'])
            targets=['-m','pytest','-o','addopts=','-q','--tb=short',str(acceptance)]
            if path.endswith('table.py'):targets+=['-k','direct_table_and_explicit_opt_in']
            check['acceptance']=command(label+'-acceptance',targets)
            check['passed']=not check['scope_errors'] and all(check[k]['exit_code']==0 for k in ('original_tests','types','acceptance'))
            stage['attempts'].append(check);save()
            if check['passed']:break
            if check_index==2:raise RuntimeError('Candidate failed after two bounded patch attempts')
            evidence=list(check['scope_errors'])
            for kind in ('original_tests','types','acceptance'):
                if check[kind]['exit_code']:
                    evidence.append((output/f'{label}-{kind if kind!="original_tests" else "original"}.log').read_text()[-14000:])
            raw=llm('Repair only the current file using exact minimal JSON patches. Preserve frozen methods, upstream files and all tests.\n'+spec,
                    file_patch_prompt(code,evidence,path),json_mode=True,max_tokens=source_tokens,timeout_seconds=source_timeout,max_retries=retries)
            code=apply_file_patches(code,raw.get('patches'))
            (work/path).write_text(code)
        stage['status']='complete';frozen[path]=hash_file(work/path)
    except Exception as error:
        stage['status']='failed';stage['error']=str(error);save();break
    save()
report['success']=len(report['stages'])==2 and all(s.get('status')=='complete' for s in report['stages'])
report['changed_files']=[file for file in files if hash_file(work/file)!=baseline[file]]
report['acceptance_unchanged']=hash_file(acceptance)==acceptance_hash
report['frozen_files']=frozen
for path in report['allowed_files']:
    diff=''.join(difflib.unified_diff(original_sources[path].splitlines(keepends=True),(work/path).read_text().splitlines(keepends=True),fromfile='a/'+path,tofile='b/'+path))
    (output/(Path(path).stem+'.patch')).write_text(diff)
save();print('REPORT',output/'report.json',flush=True);print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
