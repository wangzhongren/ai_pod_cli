"""Compact running/result summaries without exposing model deliberations or credentials."""
from collections import Counter
import json
from pathlib import Path
import time

ROOT = Path('/tmp/aipod-flaskbb-trial')
results=[]
case_path=ROOT/'original-cases.json'
original_cases={(c['classname'],c['name']) for c in json.loads(case_path.read_text())} if case_path.exists() else set()
for path in sorted(ROOT.glob('*/round-*/report.json')):
    report=json.loads(path.read_text())
    calls=report['calls']
    usages=[c['usage'] for c in calls if c.get('usage')]
    row={k:report[k] for k in ['arm','round','status']}
    row.update(calls=len(calls),completed_http=sum('seconds' in c for c in calls),
               roles=dict(Counter(c['role'] for c in calls)),
               prompt_tokens=sum(u.get('prompt_tokens',0) for u in usages),
               completion_tokens=sum(u.get('completion_tokens',0) for u in usages),
               reasoning_tokens=sum(u.get('completion_tokens_details',{}).get('reasoning_tokens',0) for u in usages),
               cached_prompt_tokens=sum(u.get('prompt_tokens_details',{}).get('cached_tokens',u.get('prompt_cache_hit_tokens',0)) for u in usages),
               usage_missing=sum(c.get('status_code')==200 and not c.get('usage') for c in calls),
               seconds=report.get('seconds'),error=report.get('error'))
    if calls:
        row['last_call_age_seconds']=round(time.time()-calls[-1]['started'],1)
    for kind,evaluation in report.get('evaluation',{}).items():
        if 'counts' in evaluation:
            row[kind]=evaluation['counts']
            if kind=='regression' and original_cases:
                actual={(c['classname'],c['name']):c['status'] for c in evaluation['cases']}
                row['original_regression']={status:sum(actual.get(c,'missing')==status for c in original_cases) for status in ['passed','failed','error','skipped','missing']}
        elif kind=='layout':
            row[kind]=evaluation
            try:
                row['layout_errors']=json.loads(evaluation['output'])
                row['layout_valid']=evaluation['exit_code']==0 and row['layout_errors']==[]
            except (json.JSONDecodeError,KeyError):
                row['layout_valid']=False
    row['changed_files']=report.get('changes')
    row['original_tests_changed']=report.get('original_tests_changed')
    results.append(row)
print(json.dumps(results,ensure_ascii=False,indent=2))
