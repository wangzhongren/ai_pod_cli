"""One real model call to exercise the user's requested compression mechanism."""
import fcntl
import hashlib
import json
from pathlib import Path
import time

from ai_pod_cli.cli import _apply_global_env
from ai_pod_cli import client
from ai_pod_cli.context_memory import ContextMemory

source=Path('/tmp/aipod-flaskbb-trial/aipod-governed/round-1')
output=Path('/tmp/aipod-flaskbb-context-policy/compression-smoke')
output.mkdir(exist_ok=False)
history=[]
task=None
for path in sorted(source.glob('call-*.json')):
    record=json.loads(path.read_text())
    messages=record['request']['messages']
    if '\nLayer: services\n' not in messages[0]['content']:
        continue
    if task is None:
        task=json.JSONDecoder().raw_decode(messages[1]['content'].split('Current layer task and project context:\n',1)[1])[0]
    last=messages[-1]['content']
    marker='Instruction result:\n'
    if marker not in last:
        continue
    observation=json.JSONDecoder().raw_decode(last.rsplit(marker,1)[1])[0]
    if observation.get('translation_error') or observation.get('parse_error'):
        continue
    assistant=next((m['content'] for m in reversed(messages[:-1]) if m['role']=='assistant'),None)
    if assistant is None:
        continue
    history.append({'assistant':assistant,'observation':observation})
    if len(json.dumps(history,ensure_ascii=False))>=175000:
        break
assert len(json.dumps(history,ensure_ascii=False))>=160000
_apply_global_env()
sdk=client.get_client()
original=sdk.chat.completions.create
report={'model':client.get_model(),'scope':'one read-only model compression call; no business edits or agent tools',
        'source_exchanges':len(history),'source_sha256':hashlib.sha256(json.dumps(history,ensure_ascii=False).encode()).hexdigest(),'calls':[]}
def create(**kwargs):
    if report['calls']:
        raise RuntimeError('Compression smoke allows at most one actual API request')
    with Path('/tmp/aipod-flaskbb-budget.json').open('r+') as ledger:
        fcntl.flock(ledger,fcntl.LOCK_EX)
        budget=json.load(ledger)
        if budget['used']>=budget['authorized_total']:
            raise RuntimeError('Authorized aggregate API budget exhausted')
        budget['used']+=1;ledger.seek(0);json.dump(budget,ledger);ledger.truncate();ledger.flush()
    call={'started':time.time()};report['calls'].append(call)
    response=original(**kwargs)
    call.update(seconds=time.time()-call['started'],usage=response.usage.model_dump() if response.usage else None)
    (output/'response.json').write_text(response.model_dump_json())
    return response
sdk.chat.completions.create=create
def llm(system,user,**kwargs):
    kwargs.update(max_retries=1,max_tokens=16384,timeout_seconds=120,progress_callback=None)
    return client.call_llm(system,user,**kwargs)
memory=ContextMemory(llm,output)
report['before_characters']=memory.size(history)
report['event']=memory.compact_if_needed(history,task,{'note':'Historical read-only replay; not a fresh execution or proof of validation.'})
report['after_characters']=memory.size(history)
report['summary']=memory.summary
report['retained_exchanges']=len(history)
(output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k not in ['summary','calls']},ensure_ascii=False))
assert report['event']['status']=='compressed'
