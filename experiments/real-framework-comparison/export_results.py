"""Export compact evidence; raw provider messages remain in the temporary trial directories."""
from collections import Counter
import json
from pathlib import Path
import shutil

HERE=Path(__file__).resolve().parent
PRIMARY=Path('/tmp/aipod-flaskbb-trial')
UPDATED=Path('/tmp/aipod-flaskbb-context-policy')
DEST=HERE/'results'
DEST.mkdir(exist_ok=True)
original={(c['classname'],c['name']) for c in json.loads((PRIMARY/'original-cases.json').read_text())}

def read_cell(root,arm,number):
    folder=root/arm/f'round-{number}'
    report=json.loads((folder/'report.json').read_text())
    if 'evaluation' not in report:
        raise RuntimeError(f'Incomplete external evaluation: {folder}')
    calls=report['calls']
    usages=[c['usage'] for c in calls if c.get('usage')]
    cases={(c['classname'],c['name']):c['status'] for c in report['evaluation']['regression']['cases']}
    layout=report['evaluation'].get('layout')
    errors=json.loads(layout['output']) if layout and layout['exit_code']==0 else None
    roles=Counter('classifier' if arm=='aipod-governed' and c['role']=='baseline' else c['role'] for c in calls)
    row=dict(arm=arm,round=number,status=report['status'],requests=len(calls),seconds=report['seconds'],roles=dict(roles),
             acceptance=report['evaluation']['acceptance']['counts'],
             original_regression={s:sum(cases.get(c,'missing')==s for c in original) for s in ['passed','failed','error','skipped','missing']},
             all_collected_regression=report['evaluation']['regression']['counts'],
             original_tests_changed=report['original_tests_changed'],layout_errors=errors,
             layout_valid=None if layout is None else layout['exit_code']==0 and errors==[],
             input_tokens=sum(u.get('prompt_tokens',0) for u in usages),
             cached_input_tokens=sum(u.get('prompt_tokens_details',{}).get('cached_tokens',0) for u in usages),
             output_tokens=sum(u.get('completion_tokens',0) for u in usages),
             reasoning_tokens=sum(u.get('completion_tokens_details',{}).get('reasoning_tokens',0) for u in usages),
             source_changes=[c for c in report['changes'] if c['path'].endswith(('.py','.ts')) and not c['path'].startswith('tests/')],
             changes=report['changes'],manual_business_code_repairs=report['manual_code_repairs'])
    if report.get('error'):row['error']=report['error']
    if report.get('history_policy'):row['history_policy']=report['history_policy']
    if report.get('pod_state'):
        row['layer_statuses']={k:v['status'] for k,v in report['pod_state']['stages'].items()}
        row['pod_status']=report['pod_state']['agent']['status']
    target=DEST/arm/f'round-{number}'
    target.mkdir(parents=True,exist_ok=True)
    for name in ['changes.patch','objective.txt']:
        shutil.copy2(folder/name,target/name)
    for name in ['evaluation.json','acceptance.xml','regression.xml','acceptance.log']:
        shutil.copy2(folder/'external'/name,target/name)
    (target/'calls.json').write_text(json.dumps(calls,ensure_ascii=False,indent=2)+'\n')
    if report.get('aipod_runtime_hashes'):
        (target/'runtime-hashes.json').write_text(json.dumps(report['aipod_runtime_hashes'],indent=2)+'\n')
    return row

rows=[]
for arm,root in [('mini-original',PRIMARY),('mini-governed',PRIMARY),('aipod-governed',UPDATED)]:
    rows.extend(read_cell(root,arm,n) for n in (1,2,3))
totals={}
for arm in ['mini-original','mini-governed','aipod-governed']:
    selected=[r for r in rows if r['arm']==arm]
    totals[arm]={k:sum(r[k] for r in selected) for k in ['requests','seconds','input_tokens','cached_input_tokens','output_tokens','reasoning_tokens']}
    totals[arm].update(completed_deliveries=sum(r['status']=='complete' for r in selected),
                       strict_acceptance_rounds_passed=sum(r['acceptance']['failed']==0 and r['acceptance']['error']==0 for r in selected),
                       final_acceptance=selected[-1]['acceptance'],
                       all_original_tests_preserved=all(not r['original_tests_changed'] and r['original_regression']['passed']==454 for r in selected),
                       layout_errors=selected[-1]['layout_errors'],
                       source_files_touched=sorted({c['path'] for r in selected for c in r['source_changes']}))
result=dict(rows=rows,totals=totals,budget=json.loads(Path('/tmp/aipod-flaskbb-budget.json').read_text()),
            invalidated_attempt=json.loads(Path('/tmp/aipod-flaskbb-trial-invalid-1/INVALIDATED.json').read_text()),
            old_policy_stop=json.loads((PRIMARY/'aipod-governed/round-1/USER-STOP.json').read_text()),
            compression_smoke=json.loads((UPDATED/'compression-smoke/report.json').read_text()))
for name in ['manifest.json','requirements-freeze.txt','migration-source.json','migration-source.patch','isolation-denial-probes.json','mini-isolation-preflight.json']:
    shutil.copy2(PRIMARY/name,DEST/name)
for name in ['framework-tracked.patch','framework-source-hashes.json','supplemental-criterion.json','test_acceptance_values.py']:
    shutil.copy2(UPDATED/name,DEST/name)
shutil.copytree(UPDATED/'framework-source',DEST/'framework-source',dirs_exist_ok=True)
supplemental=UPDATED/'supplemental-values-result.json'
if supplemental.exists():result['supplemental_values']=json.loads(supplemental.read_text())
(HERE/'RESULTS.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(totals,ensure_ascii=False,indent=2))
