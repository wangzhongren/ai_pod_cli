"""Local three-arm trial. Native framework loops; only isolation and measurement hooks."""
import argparse
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
TRIAL = Path('/tmp/aipod-flaskbb-trial').resolve()
PYTHON = Path('/tmp/aipod-comparison-venv/bin/python')
ORIGINAL = Path('/tmp/aipod-comparison-flaskbb').resolve()
GOVERNED = Path('/tmp/aipod-comparison-flaskbb-governed').resolve()
ARMS = ('mini-original', 'mini-governed', 'aipod-governed')
BUDGET = Path('/tmp/aipod-flaskbb-budget.json')
IGNORE = {'.git', '.aipod', '__pycache__', '.pytest_cache', '.coverage', '.cache', '.venv', 'venv', '.benchmark-work'}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and not any(part in IGNORE for part in p.relative_to(root).parts)}


def hashes(root):
    return {k: hashlib.sha256(v).hexdigest() for k, v in files(root).items()}


def diff(before, after, dest):
    changes, patches = [], []
    for name in sorted(before.keys() | after.keys()):
        a, b = before.get(name, b''), after.get(name, b'')
        if name in before and name in after and a == b:
            continue
        try:
            patch = list(difflib.unified_diff(a.decode().splitlines(True), b.decode().splitlines(True), fromfile='a/'+name, tofile='b/'+name))
            patches.extend(patch)
            added = sum(s.startswith('+') and not s.startswith('+++') for s in patch)
            deleted = sum(s.startswith('-') and not s.startswith('---') for s in patch)
        except UnicodeError:
            added = deleted = None
        changes.append(dict(path=name, added=added, deleted=deleted))
    dest.write_text(''.join(patches))
    return changes


def evaluate(root, output, number):
    output.mkdir(parents=True, exist_ok=True)
    results = {}
    for kind, args in [('regression', ['tests']), ('acceptance', ['-p', 'tests.conftest', str(HERE/'test_acceptance.py')])]:
        xml = output/f'{kind}.xml'
        command = [str(PYTHON), '-m', 'pytest', '-o', 'addopts=', *args, '-q', '--junitxml='+str(xml)]
        start = time.monotonic()
        env = os.environ | {'PYTHONPATH': str(root), 'BENCHMARK_ROUND': str(number), 'PYTHONDONTWRITEBYTECODE': '1'}
        with (output/f'{kind}.log').open('w') as log:
            proc = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=180)
        cases = []
        if xml.exists():
            for c in ET.parse(xml).getroot().iter('testcase'):
                cases.append(dict(name=c.attrib['name'], classname=c.attrib.get('classname'),
                                  status='failed' if c.find('failure') is not None else 'error' if c.find('error') is not None else 'skipped' if c.find('skipped') is not None else 'passed'))
        results[kind] = dict(exit_code=proc.returncode, seconds=round(time.monotonic()-start, 3),
                             counts={s:sum(c['status']==s for c in cases) for s in ['passed','failed','error','skipped']}, cases=cases)
    if (root/'beans_config.json').exists():
        check = subprocess.run([str(PYTHON), '-c', 'import json; from pathlib import Path; from ai_pod_cli.component_layout import validate_layout; print(json.dumps(validate_layout(Path.cwd(), json.load(open("beans_config.json"))["beans"])))'], cwd=root, env=os.environ | {'PYTHONPATH':str(root)}, capture_output=True, text=True, timeout=30)
        results['layout'] = dict(exit_code=check.returncode, output=check.stdout, stderr=check.stderr)
    dump(output/'evaluation.json', results)
    return results


def prepare():
    if TRIAL.exists():
        raise RuntimeError('Trial already exists; never overwrite trial evidence')
    TRIAL.mkdir()
    manifest = dict(created=time.time(), python=sys.version, migration=diff(files(ORIGINAL), files(GOVERNED), TRIAL/'migration.patch'),
                    source_commits={}, acceptance_sha256=hashlib.sha256((HERE/'test_acceptance.py').read_bytes()).hexdigest(),
                    task_sha256=hashlib.sha256((HERE/'tasks.json').read_bytes()).hexdigest(), arms={})
    for name, root in [('flaskbb', ORIGINAL), ('mini-swe-agent', Path('/tmp/aipod-comparison-mini-swe')), ('aipod', HERE.parents[1])]:
        manifest['source_commits'][name] = subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    for arm in ARMS:
        root = TRIAL/arm/'project'
        shutil.copytree(ORIGINAL if arm=='mini-original' else GOVERNED, root, ignore=shutil.ignore_patterns(*IGNORE))
        manifest['arms'][arm] = dict(root=str(root), initial_files=hashes(root))
    dump(TRIAL/'manifest.json', manifest)
    shutil.copy(HERE/'tasks.json', TRIAL/'tasks.json')
    (TRIAL/'requirements-freeze.txt').write_text(subprocess.check_output([str(PYTHON),'-m','pip','freeze'], text=True))
    print(json.dumps({'trial':str(TRIAL),'migration_files':len(manifest['migration'])}), flush=True)


class BudgetExhausted(BaseException):
    pass


def trial(arm, number, live):
    if arm not in ARMS or number not in (1,2,3):
        raise ValueError('Unknown trial arm/round')
    root, output = TRIAL/arm/'project', TRIAL/arm/f'round-{number}'
    if output.exists():
        raise RuntimeError('Round already exists; do not overwrite or silently retry')
    output.mkdir()
    manifest = json.loads((TRIAL/'manifest.json').read_text())
    assert hashlib.sha256((HERE/'test_acceptance.py').read_bytes()).hexdigest() == manifest['acceptance_sha256']
    tasks = json.loads((TRIAL/'tasks.json').read_text())
    before = files(root)
    report = dict(arm=arm, round=number, task=tasks[number-1]['id'], status='prepared', calls=[], manual_code_repairs=0, http_budget=180)
    def save():
        dump(output/'report.json', report)
    save()
    if not live:
        report['evaluation'] = evaluate(root, output/'preflight', number)
        save()
        return
    # Read the user's already configured model into memory only. Never put it in child shell environments or logs.
    from ai_pod_cli.cli import _apply_global_env
    _apply_global_env()
    from ai_pod_cli import client
    import ai_pod_cli.context_memory as context_memory
    report['history_policy'] = dict(compact_at=context_memory.HISTORY_COMPACT_AT,hard_limit=context_memory.HISTORY_HARD_LIMIT)
    report['aipod_runtime_hashes'] = {p.relative_to(Path(client.__file__).parent).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in Path(client.__file__).parent.rglob('*.py')}
    api_key = os.environ['OPENAI_API_KEY']
    api_base = os.environ.get('OPENAI_BASE_URL','https://api.openai.com/v1')
    model_name = client.get_model()
    sdk = client.get_client()
    client.DEFAULT_SOURCE_MAX_TOKENS = client.DEFAULT_JSON_MAX_TOKENS = 16384
    report.update(model=model_name, temperature=0.1, max_output_tokens=16384, timeout_seconds=120)
    safe_environment = {'PATH':str(PYTHON.parent)+os.pathsep+os.defpath+':/opt/homebrew/bin',
                        'LANG':'en_US.UTF-8','PYTHONDONTWRITEBYTECODE':'1', 'LITELLM_LOCAL_MODEL_COST_MAP':'True',
                        'MSWEA_SILENT_STARTUP':'1'}
    os.environ.clear()
    os.environ.update(safe_environment)
    os.chdir(root)
    scratch = root/'.benchmark-work'
    scratch.mkdir(exist_ok=True)
    # OS runtime reads remain available, but every other temporary workspace, evaluator log,
    # and user home is denied. The exception list contains only this arm and the installed venv.
    read_grant = '\n'.join(['(allow file-read*)', '(deny file-read* (subpath "/Users"))',
        '(deny file-read-data (require-all (subpath "/private/tmp") '
        + f'(require-not (subpath {json.dumps(str(root))})) '
        + f'(require-not (subpath {json.dumps(str(Path("/tmp/aipod-comparison-venv").resolve()))}))))'])
    # The same read/network containment applies to both products, on top of their native mutation scope.
    denied = [str(Path('/Users/wangzhongren')), str(HERE), str(ORIGINAL), str(GOVERNED)]
    for other in ARMS:
        if other != arm:
            denied.append(str(TRIAL/other))
    denied.extend(str(p) for p in TRIAL.iterdir() if p != TRIAL/arm)
    denied.extend(str(p) for p in (TRIAL/arm).iterdir() if p != root)
    profile = '\n'.join(['(version 1)', '(deny default)', '(allow file-read-metadata)', read_grant, '(allow process-exec)',
                          '(allow process-fork)', '(allow sysctl-read)', '(allow mach-lookup)', '(allow signal (target self))',
                          '(allow network* (local ip "localhost:*"))',
                          f'(allow file-write* (subpath {json.dumps(str(root))}) (literal "/dev/null"))',
                          '(deny file-link)', '(deny file-read* '+ ' '.join(f'(subpath {json.dumps(p)})' for p in denied)+')',
                          '(deny file-read* (regex "/[.](env([.][^/]*)?|npmrc|pypirc)$"))'])
    import httpx
    original_send = httpx.Client.send
    current_role = 'baseline'
    def measured_send(http_client, request, **kwargs):
        if request.url.host != httpx.URL(api_base).host:
            raise RuntimeError('Trial permits only configured model API requests from its parent process')
        if len(report['calls']) >= report['http_budget']:
            raise BudgetExhausted('180 actual HTTP requests exhausted for this round')
        # Count invalidated attempts and retries against the user's one total authorization.
        with BUDGET.open('r+') as ledger:
            fcntl.flock(ledger, fcntl.LOCK_EX)
            totals = json.load(ledger)
            if totals['used'] >= totals['authorized_total']:
                raise BudgetExhausted('Authorized aggregate HTTP request budget exhausted')
            totals['used'] += 1
            ledger.seek(0); json.dump(totals,ledger); ledger.truncate(); ledger.flush()
        index = len(report['calls'])+1
        body = json.loads(request.content)
        # Common measurement annotation; framework messages, tool schemas, parsers and loops remain native.
        notice = f'\nBenchmark resource meter: {181-index} actual HTTP requests remain for this task, including separate instruction translation and retries. Submit when implementation and checks are complete.'
        body['messages'][-1]['content'] = (body['messages'][-1].get('content') or '') + notice
        payload = json.dumps(body).encode()
        request = httpx.Request(request.method, request.url, headers={k:v for k,v in request.headers.items() if k.lower()!='content-length'}, content=payload, extensions=request.extensions)
        call = dict(index=index, role=current_role, started=time.time())
        report['calls'].append(call)
        save()
        start = time.monotonic()
        try:
            response = original_send(http_client, request, **kwargs)
            response.read()
            response_json = response.json()
            call.update(status_code=response.status_code, usage=response_json.get('usage'))
            if response_json.get('choices'):
                call['finish_reason'] = response_json['choices'][0].get('finish_reason')
            dump(output/f'call-{index:03d}.json', dict(request=body,response=response_json))
            return response
        except Exception as error:
            call['error'] = str(error).replace(api_key,'[redacted]')
            raise
        finally:
            call['seconds'] = round(time.monotonic()-start,3)
            save()
            print(json.dumps({'arm':arm,'round':number,**{k:v for k,v in call.items() if k!='usage'}}),flush=True)
    httpx.Client.send = measured_send
    common = '''Work in the current repository only. This is an existing FlaskBB application; keep unrelated features and public imports compatible. Dependencies and translations are already installed/built. Run python -m pytest -o addopts='' tests -q for the original full regression suite. Independent acceptance is withheld and will run after submission. Add your own focused tests if useful. Do not modify existing tests, test fixtures or build configuration to bypass verification. No new dependencies, external services, deployment or network operations are needed. Keep changes focused. Each task has at most 180 actual model HTTP requests including translations/retries. A fresh task conversation is used for each successive requirement while files are retained.\n'''
    common += f'Your repository working directory is {root}. The shell starts there. Read only this repository and installed runtime documentation; evaluator data, other workspaces and host home directories are outside this task.\n'
    if arm != 'mini-original':
        common += '''This project has a migrated AIPod registration slice; read MIGRATION.md and preserve its Models/Providers/Services/Pipeline/Interface responsibilities and public entry. Original FlaskBB host modules and plugins outside that slice remain legacy compatibility code. Registration tests exercise the real host adapter; it requires a Flask app context and has no standalone launch command.\n'''
    task = common + '\nCURRENT CHANGE:\n' + tasks[number-1]['requirement']
    if number > 1:
        task += '\nEARLIER REQUIREMENTS TO PRESERVE:\n'+'\n'.join(t['requirement'] for t in tasks[:number-1])
    (output/'objective.txt').write_text(task)
    start = time.monotonic()
    report['status'] = 'running'
    save()
    try:
        if arm.startswith('mini-'):
            import yaml
            from minisweagent.agents.default import DefaultAgent
            from minisweagent.models.litellm_model import LitellmModel
            from minisweagent.environments.local import LocalEnvironment
            # mini's package import may load its optional user .env; never inherit it into bash.
            os.environ.clear()
            os.environ.update(safe_environment)
            config = yaml.safe_load(Path('/tmp/aipod-comparison-mini-swe/src/minisweagent/config/mini.yaml').read_text())
            class IsolatedLocalEnvironment(LocalEnvironment):
                def execute(self, action, cwd='', *, timeout=None):
                    wrapped = dict(action, command=shlex.join(['/usr/bin/sandbox-exec','-p',profile,'/bin/sh','-c',action.get('command','')]))
                    return super().execute(wrapped,cwd,timeout=timeout)
            class RedactedModel(LitellmModel):
                def serialize(self):
                    return json.loads(json.dumps(super().serialize(),default=str).replace(api_key,'[redacted]'))
            model = RedactedModel(**(config['model'] | dict(model_name='openai/'+model_name, cost_tracking='ignore_errors',model_kwargs=dict(api_key=api_key,api_base=api_base,temperature=0.1,max_tokens=16384,timeout=120,num_retries=0))))
            env = IsolatedLocalEnvironment(cwd=str(root),timeout=120,env=config['environment']['env'] | {'HOME':str(scratch),'TMPDIR':str(scratch),'TMP':str(scratch),'TEMP':str(scratch),'PYTHONPATH':str(root),'PYTEST_ADDOPTS':'-p no:cacheprovider'})
            preflight = env.execute({'command':"python -m pytest -o addopts='' tests/unit/auth/test_registration.py -q"})
            report['shell_preflight'] = preflight
            if preflight['returncode'] and number == 1:
                raise RuntimeError('Native baseline shell preflight failed before model calls')
            settings = {k:v for k,v in config['agent'].items() if k!='mode'}
            settings.update(step_limit=180,cost_limit=0,wall_time_limit_seconds=7200,output_path=output/'trajectory.json')
            agent = DefaultAgent(model,env,**settings)
            result = agent.run(task)
            report['native_result'] = result
            report['status'] = 'complete' if result.get('exit_status')=='Submitted' else 'incomplete'
        else:
            from ai_pod_cli.workspace import WorkspaceTools
            original_shell = WorkspaceTools.shell_command
            def protected_shell(instance, command):
                argv = original_shell(instance,command)
                argv[2] = argv[2].replace('(allow file-read*)','(allow file-read-metadata)\n'+read_grant)
                # macOS refuses nested sandbox-exec. Extend the native profile with containment;
                # retain all native ownership grants and denials exactly as produced by AIPod.
                argv[2] = argv[2].replace('(allow network*)','(allow network* (local ip "localhost:*"))')
                argv[2] += '\n(deny file-read* '+ ' '.join(f'(subpath {json.dumps(p)})' for p in denied)+')'
                return argv
            WorkspaceTools.shell_command = protected_shell
            preflight = WorkspaceTools(root,'services').shell("python -m pytest -o addopts='' tests/unit/auth/test_registration.py -q")
            report['shell_preflight'] = preflight
            if preflight['exit_code'] and number == 1:
                raise RuntimeError('AIPod shell preflight failed before model calls')
            original_llm = client.call_llm
            def measured_llm(system,user,**kwargs):
                nonlocal current_role
                current_role = 'compactor' if system.startswith('CONTEXT_COMPACTION') else 'translator' if system.startswith('TRANSLATE_LOCAL_OPERATION') else 'worker:'+system.split('\nLayer: ')[1].split('\n')[0] if '\nLayer: ' in system else 'coordinator'
                kwargs.update(max_tokens=16384,timeout_seconds=120,progress_callback=None)
                return original_llm(system,user,**kwargs)
            client.call_llm = measured_llm
            # Client initialized above; handle_pod only uses the environment key as a presence check.
            os.environ['OPENAI_API_KEY'] = 'configured-in-parent-client'
            from ai_pod_cli.pod.agent import handle_pod
            from types import SimpleNamespace
            handle_pod(SimpleNamespace(desc=task,file='',stage='auto',yes=True))
            report['status'] = 'complete'
    except (Exception, BudgetExhausted) as error:
        report.update(status='budget_exhausted' if isinstance(error,BudgetExhausted) else 'failed',error=str(error).replace(api_key,'[redacted]'))
    finally:
        httpx.Client.send = original_send
        report['seconds'] = round(time.monotonic()-start,3)
        os.environ.pop('OPENAI_API_KEY',None)
        save()
    if (root/'aipod_plan.json').exists():
        report['pod_state'] = json.loads((root/'aipod_plan.json').read_text())
    after = files(root)
    report['changes'] = diff(before,after,output/'changes.patch')
    report['original_tests_changed'] = [n for n,b in before.items() if n.startswith('tests/') and after.get(n) != b]
    report['evaluation'] = evaluate(root,output/'external',number)
    save()
    print(json.dumps({'arm':arm,'round':number,'status':report['status'],'calls':len(report['calls']),'evaluation':{k:v.get('counts') for k,v in report['evaluation'].items()}}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--arm',choices=ARMS)
    parser.add_argument('--round',type=int,default=1)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--trial-root',type=Path)
    args=parser.parse_args()
    if args.trial_root:
        TRIAL=args.trial_root.resolve()
    if args.prepare: prepare()
    else: trial(args.arm,args.round,args.live)
