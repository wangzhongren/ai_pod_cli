"""Run the remaining preregistered rounds after each prior round's external evaluation."""
import json
from pathlib import Path
import subprocess
import sys
import time

arm=sys.argv[1]
assert arm in ('mini-original','mini-governed','aipod-governed')
here=Path(__file__).resolve().parent
first = int(sys.argv[2]) if len(sys.argv)>2 else 2
root = Path(sys.argv[3]).resolve() if len(sys.argv)>3 else Path('/tmp/aipod-flaskbb-trial')
assert first in (1,2)
for number in range(first,4):
    previous=root/arm/f'round-{number-1}'/'report.json'
    deadline=time.monotonic()+7500
    while number > 1:
        try:
            report=json.loads(previous.read_text())
            if report.get('evaluation') and report['status'] != 'running':
                if not report['calls']:
                    raise RuntimeError('Prior round had a harness preflight failure; stop before inference')
                break
        except (FileNotFoundError,json.JSONDecodeError):
            pass
        if time.monotonic()>deadline:
            raise TimeoutError('Prior round did not finish')
        time.sleep(2)
    print(json.dumps({'arm':arm,'starting_round':number,'previous_status':report['status'] if number>1 else None}),flush=True)
    with (root/arm/f'round-{number}.log').open('w') as log:
        result=subprocess.run([sys.executable,str(here/'run.py'),'--arm',arm,'--round',str(number),'--live','--trial-root',str(root)],stdout=log,stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f'Round {number} runner exited with {result.returncode}')
