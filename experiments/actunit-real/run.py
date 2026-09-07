"""Exercise AIPod's production Python source-generation helper on a real repository."""
import ast
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

repo=Path(sys.argv[1]).resolve()
test_python=Path(sys.argv[2]).absolute()  # Preserve the virtualenv executable symlink.
run_root=Path(tempfile.mkdtemp(prefix='aipod-actunit-real-'))
work=run_root/'project'
work.mkdir()
files=subprocess.check_output(['git','ls-files','-z'],cwd=repo).decode().split('\0')
files=[file for file in files if file]
for file in files:
    target=work/file
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(repo/file,target)
acceptance=Path(__file__).with_name('acceptance_cases.py').resolve()
acceptance_hash=hashlib.sha256(acceptance.read_bytes()).hexdigest()
env={**os.environ,'PYTHONPATH':str(work/'src')}
max_tokens=int(os.environ.get('ACTUNIT_EVAL_MAX_TOKENS','32768'))
if not 1 <= max_tokens <= 32768: raise ValueError('ACTUNIT_EVAL_MAX_TOKENS must be between 1 and 32768')
max_retries=int(os.environ.get('ACTUNIT_EVAL_RETRIES','3'))
max_attempts=int(os.environ.get('ACTUNIT_EVAL_ATTEMPTS','1'))
report={'max_retries':max_retries,'max_attempts':max_attempts,'initial_max_tokens':max_tokens,'project':'wangzhongren/ActUnit','revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo).decode().strip(),
        'root':str(run_root),'source_target':'src/actunit/runtime.py','acceptance_hash':acceptance_hash,'calls':[],'attempts':[]}
def save(): (run_root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
def run_tests(name, targets):
    command=[str(test_python),'-m','pytest','-o','addopts=','-q','--tb=short',*targets]
    result=subprocess.run(command,cwd=work,env=env,text=True,capture_output=True,timeout=120)
    (run_root/f'{name}.log').write_text(result.stdout+result.stderr)
    record={'exit_code':result.returncode,'summary':(result.stdout+result.stderr).strip().splitlines()[-1], 'log':str(run_root/f'{name}.log')}
    print(name,json.dumps(record),flush=True)
    return record
import_path=subprocess.check_output([str(test_python),'-c','import actunit;print(actunit.__file__)'],cwd=work,env=env,text=True).strip()
assert Path(import_path).is_relative_to(work), import_path
report['tested_import']=import_path
report['baseline_original']=run_tests('baseline-original',['tests'])
report['baseline_acceptance']=run_tests('baseline-acceptance',[str(acceptance)])
save()
if report['baseline_original']['exit_code'] or report['baseline_acceptance']['exit_code']==0:
    raise SystemExit('Baseline does not match intended experiment; inspect logs before generation.')
original={file:hashlib.sha256((work/file).read_bytes()).hexdigest() for file in files}
source=(work/report['source_target']).read_text()
requirements='''Repair the existing ActUnit ActionRuntime in src/actunit/runtime.py. Preserve all public classes, methods, signatures, imports that remain necessary, permission ordering and existing error mappings. Handler arguments must include Pydantic validated defaults (including default factories and None), while preserving explicit caller values and normal Pydantic coercion. Ordinary handler exceptions such as RuntimeError, KeyError and TypeError must become EXECUTION_FAILED ActionResult values. Never catch KeyboardInterrupt, SystemExit or other BaseException control flow. Preserve ActionExecutionError codes, APPROVAL_DENIED status and FILE_NOT_FOUND mapping. Reject non-dict handler outputs and invalid status values (including unhashable list/dict values) with INVALID_RESULT instead of throwing. Preserve valid ok/partial/error outputs, call IDs, tool names, duration and existing PROCESS_FAILED behavior. Change only this file. No new dependencies, no weakening of permission or input validation. Do not change contracts.py, xml.py, tests or README. Metadata must be {"summary":"brief changes","extra_deps":[]}.'''
config=Path.home()/'.aipod/config.toml'
for key,value in tomllib.loads(config.read_text()).get('env',{}).items(): os.environ.setdefault(key,str(value))
report['model']=os.environ.get('OPENAI_MODEL')
report['request_timeout_seconds']=float(os.environ.get('OPENAI_TIMEOUT_SECONDS','120'))
def llm(system,user,**options):
    index=len(report['calls'])+1
    call={'channel':'json_metadata' if options['json_mode'] else 'xml_source'}
    report['calls'].append(call)
    start=time.monotonic()
    print('model',index,call['channel'],flush=True)
    try:
        completions=get_client().chat.completions
        original_create=completions.create
        call['requests']=[]
        def observed_create(**kwargs):
            request={'max_tokens':kwargs.get('max_tokens')}
            call['requests'].append(request)
            save()
            started=time.monotonic()
            try:
                response=original_create(**kwargs)
                choice=response.choices[0]
                request['finish_reason']=choice.finish_reason
                request['source_characters']=len(choice.message.content or '')
                usage=getattr(response,'usage',None)
                if usage:
                    request['usage']={key:getattr(usage,key,None) for key in ('prompt_tokens','completion_tokens','total_tokens')}
                    details=getattr(usage,'completion_tokens_details',None)
                    if details: request['reasoning_tokens']=getattr(details,'reasoning_tokens',None)
                return response
            except Exception as error:
                request['error_type']=type(error).__name__
                raise
            finally:
                request['seconds']=round(time.monotonic()-started,3)
                print('request',json.dumps(request),flush=True)
                save()
        with patch.object(completions,'create',side_effect=observed_create):
            result=call_llm(system,user,**options)
        call['seconds']=round(time.monotonic()-start,3)
        (run_root/f'model-{index}.txt').write_text(json.dumps(result,ensure_ascii=False,indent=2) if isinstance(result,dict) else result)
        return result
    except Exception as error:
        call['error']=str(error)
        raise
    finally: save()
feedback=''
for attempt in range(1,max_attempts+1):
    start=time.monotonic()
    item={'attempt':attempt}
    report['attempts'].append(item)
    try:
        result=generate_source(llm,requirements,'Existing source:\n'+source+feedback,report['source_target'],max_retries=max_retries,max_tokens=max_tokens,source_max_tokens=max_tokens,source_timeout_seconds=report['request_timeout_seconds'])
        if result.get('extra_deps')!=[]: raise ValueError('Unexpected dependencies')
        code=result['code']
        ast.parse(code)
        (work/report['source_target']).write_text(code)
        item['original_tests']=run_tests(f'attempt-{attempt}-original',['tests'])
        item['acceptance']=run_tests(f'attempt-{attempt}-acceptance',[str(acceptance)])
        item['changed_files']=[file for file in files if hashlib.sha256((work/file).read_bytes()).hexdigest()!=original[file]]
        assert set(item['changed_files'])<={report['source_target']}
        assert hashlib.sha256(acceptance.read_bytes()).hexdigest()==acceptance_hash
        item['passed']=not item['original_tests']['exit_code'] and not item['acceptance']['exit_code']
        if not item['passed']:
            feedback='\nAcceptance failures (fixed tests must not be modified):\n'+(run_root/f'attempt-{attempt}-acceptance.log').read_text()[-10000:]
    except Exception as error:
        item['passed']=False
        item['error']=str(error)
        feedback='\nPrevious attempt failed: '+str(error)
    item['seconds']=round(time.monotonic()-start,3)
    save()
    if item['passed']: break
before=run_root/'runtime-before.py';before.write_text(source)
diff=subprocess.run(['diff','-u',str(before),str(work/report['source_target'])],text=True,capture_output=True)
(run_root/'runtime.patch').write_text(diff.stdout)
report['success']=report['attempts'][-1]['passed']
save()
print('REPORT',run_root/'report.json',flush=True)
print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
